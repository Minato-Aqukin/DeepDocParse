package discovery

import (
	"context"
	"slices"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

// ChildManifest is the frozen reference to a child directory that this scope
// actually observed. The shape is fixed by ddp-scope-coverage/1#ScopeManifest:
// node_id, scope_ref and enumeration_state only.
type ChildManifest struct {
	NodeID           string `json:"node_id"`
	ScopeRef         string `json:"scope_ref"`
	EnumerationState string `json:"enumeration_state"`
}

// RemoteExpansion carries everything the authenticated server-side expansion
// observed. It is never assembled from caller input. Handled suppresses the
// store's local fallback unknown-subtree entry for direct members that the
// expansion already classified; Sources records the direct member through
// which a remote origin was discovered so that later reads can re-check the
// authorization root instead of demanding a local registration for it.
type RemoteExpansion struct {
	Handled          map[string]bool
	Sources          map[string]string
	Targets          []TargetKey
	NodeRoutes       []NodeRoute
	Consumption      DiscoveryConsumption
	Revisions        []DirectoryRevision
	Children         []ChildManifest
	Unknowns         []UnknownSubtree
	TargetBudgetUsed int
	ValidUntil       time.Time
}

type ExpansionInput struct {
	Members []Member
	// RevokedNodeIDs is this center's current membership revocation set, not the
	// frozen snapshot's authorization overlay or a peer's advertised state.
	RevokedNodeIDs map[string]bool
	LocalNodeID    string
	Operation      string
	AllowedNodeIDs []string
	MaxTargets     int
	MaxRequests    int
	MaxNodes       int
	Now            time.Time
	// Path contains upstream callers, root first; none may be contacted.
	Path []string
	// A nil set retains the configured-directory test seam. Production supplies
	// the live local approval set, so endpoint configuration cannot confer trust.
	ApprovedNodeIDs map[string]bool
}

func hasFederationEndpoint(d NodeDescriptor) bool {
	for _, endpoint := range d.ControlledEndpoints {
		if endpoint.Purpose == "federation" && ValidBaseURL(endpoint.URL) {
			return true
		}
	}
	return false
}

type directoryPull struct {
	snapshotID string
	revision   int64
	validUntil time.Time
	items      []PeerMember
	complete   bool
	reason     string
}

// pullMembers follows one peer directory snapshot to its separate terminal
// page. A truncated chain returns the members actually observed plus an honest
// reason; the caller keeps them but must mark the subtree partial.
func (d *PeerDirectory) pullMembers(ctx context.Context, cfg PeerConfig, requests *int, maxRequests int) directoryPull {
	out := directoryPull{}
	cursor := ""
	seen := map[string]bool{}
	first := true
	var firstCreated time.Time
	var firstCursor, firstTerminal string
	for {
		if *requests >= maxRequests {
			out.reason = "budget_exhausted"
			return out
		}
		limit := 0
		if first {
			limit = 100
		}
		// Every follow-up page must name the snapshot the first page opened;
		// both peer endpoints reject a bare cursor with 400.
		page, reason := d.membersPage(ctx, cfg, out.snapshotID, cursor, limit, requests)
		if reason != "" {
			out.reason = reason
			return out
		}
		if first {
			out.snapshotID, out.revision, out.validUntil = page.SnapshotID, page.RegistryRevision, page.ExpiresAt
			firstCreated, firstCursor, firstTerminal = page.CreatedAt, page.FirstCursor, page.TerminalCursor
			first = false
			cursor = page.FirstCursor
		} else {
			if page.SnapshotID != out.snapshotID || page.RegistryRevision != out.revision ||
				!page.CreatedAt.Equal(firstCreated) || !page.ExpiresAt.Equal(out.validUntil) ||
				page.FirstCursor != firstCursor || page.TerminalCursor != firstTerminal || seen[cursor] {
				out.reason = "unknown"
				return out
			}
			seen[cursor] = true
		}
		if cursor == firstTerminal {
			out.complete = page.Complete && page.NextCursor == nil && len(page.Members) == 0
			if !out.complete {
				out.reason = "unknown"
			}
			return out
		}
		if page.Complete || page.NextCursor == nil {
			out.reason = "unknown"
			return out
		}
		out.items = append(out.items, page.Members...)
		cursor = *page.NextCursor
	}
}

type catalogPull struct {
	snapshotID string
	revision   int64
	validUntil time.Time
	total      int
	items      []CollectionRef
	complete   bool
	reason     string
}

func (d *PeerDirectory) pullCatalog(ctx context.Context, cfg PeerConfig, requests *int, maxRequests int) catalogPull {
	out := catalogPull{}
	cursor := ""
	seen := map[string]bool{}
	firstPage := true
	var firstCreated time.Time
	var firstCursor, firstTerminal string
	for {
		if *requests >= maxRequests {
			out.reason = "budget_exhausted"
			return out
		}
		limit := 0
		if firstPage {
			limit = 100
		}
		page, reason := d.collectionsPage(ctx, cfg, out.snapshotID, cursor, limit, requests)
		if reason != "" {
			out.reason = reason
			return out
		}
		if firstPage {
			out.snapshotID, out.revision, out.validUntil = page.SnapshotID, page.RegistryRevision, page.ValidUntil
			out.total = *page.Total
			firstCreated, firstCursor, firstTerminal = page.CreatedAt, page.FirstCursor, page.TerminalCursor
			firstPage = false
			cursor = page.FirstCursor
		} else {
			if page.SnapshotID != out.snapshotID || page.RegistryRevision != out.revision ||
				!page.CreatedAt.Equal(firstCreated) || !page.ValidUntil.Equal(out.validUntil) ||
				*page.Total != out.total || page.FirstCursor != firstCursor || page.TerminalCursor != firstTerminal || seen[cursor] {
				out.reason = "unknown"
				return out
			}
			seen[cursor] = true
		}
		if cursor == firstTerminal {
			out.complete = page.Complete && page.NextCursor == nil && len(page.Collections) == 0 && len(out.items) == out.total
			if !out.complete {
				out.reason = "unknown"
			}
			return out
		}
		if page.Complete || page.NextCursor == nil || (len(page.Collections) == 0 && len(out.items) != out.total) {
			out.reason = "unknown"
			return out
		}
		out.items = append(out.items, page.Collections...)
		cursor = *page.NextCursor
	}
}

// ExpandScope walks the approved direct members and, transitively, the
// directories they publish. It never contacts an unregistered node, never
// follows a cycle twice, keeps every observed target, and reports exhaustion
// as budget_exhausted instead of silently returning a short result.
func ExpandScope(ctx context.Context, dir *PeerDirectory, in ExpansionInput) RemoteExpansion {
	out := RemoteExpansion{Handled: map[string]bool{}, Sources: map[string]string{}}
	if dir == nil || len(in.Members) == 0 {
		return out
	}
	now := in.Now
	if now.IsZero() {
		now = time.Now().UTC()
	}
	var allowedNodes map[string]bool
	if in.AllowedNodeIDs != nil {
		allowedNodes = make(map[string]bool, len(in.AllowedNodeIDs))
		for _, nodeID := range in.AllowedNodeIDs {
			allowedNodes[nodeID] = true
		}
	}
	allowed := func(nodeID string) bool { return allowedNodes == nil || allowedNodes[nodeID] }
	reachable := func(nodeID string) bool {
		_, configured := dir.Configured(nodeID)
		return configured && (in.ApprovedNodeIDs == nil || in.ApprovedNodeIDs[nodeID])
	}
	requests, nodes := 0, 0
	stopped := false
	stop := func() { stopped = true }
	depleted := func() bool {
		return stopped || requests >= in.MaxRequests || len(out.Targets) >= in.MaxTargets
	}
	unknownSeen := map[string]bool{}
	addUnknown := func(nodeID, reason string) {
		key := nodeID + "\x00" + reason
		if unknownSeen[key] {
			return
		}
		unknownSeen[key] = true
		out.Unknowns = append(out.Unknowns, UnknownSubtree{NodeID: nodeID, Reason: reason})
	}
	visited := map[string]bool{in.LocalNodeID: true}
	for _, nodeID := range in.Path {
		visited[nodeID] = true
	}
	accounted := map[string]bool{}
	routes := map[string][]string{}
	scheduled := map[string]bool{}
	targetSeen := map[string]bool{}

	addTarget := func(origin, collectionID, root string, via []string) bool {
		key := origin + "\x00" + collectionID
		if !targetSeen[key] && len(out.Targets) >= in.MaxTargets {
			return false
		}
		if previous, exists := routes[origin]; !exists || routeLess(via, previous) {
			routes[origin] = slices.Clone(via)
			out.Sources[origin] = root
		}
		if targetSeen[key] {
			return true
		}
		targetSeen[key] = true
		out.Targets = append(out.Targets, TargetKey{OriginNodeID: origin, CollectionID: collectionID, Operation: in.Operation})
		if out.Sources[origin] == "" {
			out.Sources[origin] = root
		}
		return true
	}
	bindValidUntil := func(value time.Time) {
		if value.IsZero() {
			return
		}
		if out.ValidUntil.IsZero() || value.Before(out.ValidUntil) {
			out.ValidUntil = value
		}
	}

	type job struct {
		nodeID     string
		root       string
		descriptor NodeDescriptor
		enumerate  bool
	}
	queue := []job{}

	enqueue := func(j job) {
		if visited[j.nodeID] || scheduled[j.nodeID] {
			return
		}
		if depleted() || nodes+len(queue) >= in.MaxNodes {
			addUnknown(j.nodeID, "budget_exhausted")
			return
		}
		scheduled[j.nodeID] = true
		queue = append(queue, j)
	}

	// Seed the frozen direct members. The classification mirrors the store's
	// historical fallback exactly for a deployment with no registered peers.
	for _, member := range in.Members {
		if member.State != MemberApproved || visited[member.NodeID] {
			continue
		}
		if !allowed(member.NodeID) {
			addUnknown(member.NodeID, "denied")
			continue
		}
		out.Handled[member.NodeID] = true
		if member.Descriptor == nil || !member.Descriptor.ValidUntil.After(now) || !hasFederationEndpoint(*member.Descriptor) {
			addUnknown(member.NodeID, "unknown")
			continue
		}
		configured := reachable(member.NodeID)
		if !member.Descriptor.DiscoveryCapabilities.EnumerateMembers {
			// The subtree cannot be enumerated. Its own published collections are
			// still a legitimate leaf directory when credentials are configured.
			addUnknown(member.NodeID, "enumeration_unsupported")
			if configured {
				enqueue(job{nodeID: member.NodeID, root: member.NodeID, descriptor: *member.Descriptor})
			}
			continue
		}
		if !configured {
			addUnknown(member.NodeID, "unknown")
			continue
		}
		enqueue(job{nodeID: member.NodeID, root: member.NodeID, descriptor: *member.Descriptor, enumerate: true})
	}

	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		if visited[current.nodeID] {
			continue
		}
		if depleted() {
			stop()
			addUnknown(current.nodeID, "budget_exhausted")
			continue
		}
		if !accounted[current.nodeID] && nodes >= in.MaxNodes {
			stop()
			addUnknown(current.nodeID, "budget_exhausted")
			continue
		}
		visited[current.nodeID] = true
		if !accounted[current.nodeID] {
			nodes++
			accounted[current.nodeID] = true
		}
		cfg, _ := dir.Configured(current.nodeID)

		membersPull := directoryPull{complete: true}
		if current.enumerate && !stopped {
			membersPull = dir.pullMembers(ctx, cfg, &requests, in.MaxRequests)
			if membersPull.snapshotID != "" {
				out.Revisions = append(out.Revisions, DirectoryRevision{
					NodeID: current.nodeID, RegistryRevision: membersPull.revision,
					FetchedAt: now, DirectoryRef: "members", SnapshotRef: membersPull.snapshotID,
				})
				bindValidUntil(membersPull.validUntil)
			}
			if membersPull.reason != "" {
				addUnknown(current.nodeID, membersPull.reason)
				if membersPull.reason == "budget_exhausted" {
					stop()
				}
			}
		}

		catalogResult := catalogPull{complete: true}
		if !stopped {
			catalogResult = dir.pullCatalog(ctx, cfg, &requests, in.MaxRequests)
			if catalogResult.snapshotID != "" {
				out.Revisions = append(out.Revisions, DirectoryRevision{
					NodeID: current.nodeID, RegistryRevision: catalogResult.revision,
					FetchedAt: now, DirectoryRef: "collections", SnapshotRef: catalogResult.snapshotID,
				})
				bindValidUntil(catalogResult.validUntil)
			}
			if catalogResult.reason != "" {
				addUnknown(current.nodeID, catalogResult.reason)
				if catalogResult.reason == "budget_exhausted" {
					stop()
				}
			}
			for _, item := range catalogResult.items {
				if !addTarget(current.nodeID, item.CollectionID, current.root, nil) {
					addUnknown(current.nodeID, "budget_exhausted")
					stop()
					break
				}
			}
		}

		if membersPull.snapshotID != "" {
			state := string(contracts.EnumerationStateSealed)
			if !membersPull.complete || !catalogResult.complete {
				state = string(contracts.EnumerationStatePartial)
			}
			out.Children = append(out.Children, ChildManifest{NodeID: current.nodeID, ScopeRef: membersPull.snapshotID, EnumerationState: state})
		}

		if stopped {
			continue
		}
		subtreeRead := false
		for _, child := range membersPull.items {
			// Re-entering the local node or an already scheduled directory is the
			// loop/duplicate path itself, not an unexpanded subtree.
			if child.NodeID == in.LocalNodeID || visited[child.NodeID] || scheduled[child.NodeID] {
				continue
			}
			if !allowed(child.NodeID) {
				addUnknown(child.NodeID, "denied")
				continue
			}
			// Local revocation overrides a peer's stale approval before inspecting
			// its descriptor or configuration, and before enqueueing any work.
			if in.RevokedNodeIDs[child.NodeID] {
				addUnknown(child.NodeID, "denied")
				continue
			}
			if child.State != MemberApproved {
				addUnknown(child.NodeID, "unknown")
				continue
			}
			childDescriptor := child.descriptor()
			if !child.ValidUntil.After(now) || !hasFederationEndpoint(childDescriptor) {
				addUnknown(child.NodeID, "unknown")
				continue
			}
			_, configured := dir.Configured(child.NodeID)
			unapproved := in.ApprovedNodeIDs != nil && !in.ApprovedNodeIDs[child.NodeID]
			if !configured && !unapproved {
				addUnknown(child.NodeID, "unknown")
				continue
			}
			if unapproved || !configured {
				if nodes >= in.MaxNodes {
					addUnknown(child.NodeID, "budget_exhausted")
					continue
				}
				if subtreeRead {
					continue
				}
				subtreeRead = true
				pull := dir.pullSubtree(ctx, cfg, append(slices.Clone(in.Path), in.LocalNodeID), in.AllowedNodeIDs, &requests, in.MaxRequests, max(0, in.MaxNodes-nodes))
				if pull.page != nil {
					reportedNodes := map[string]bool{}
					for _, revision := range pull.page.Revisions {
						if revision.NodeID != current.nodeID {
							reportedNodes[revision.NodeID] = true
						}
					}
					for _, target := range pull.targets {
						reportedNodes[target.TargetKey.OriginNodeID] = true
						for _, via := range target.ViaNodeIDs {
							reportedNodes[via] = true
						}
					}
					overlap := 0
					for node := range reportedNodes {
						if accounted[node] {
							overlap++
						}
						accounted[node] = true
					}
					nodes += max(0, pull.page.Consumption.Nodes-overlap)
					bindValidUntil(pull.page.ValidUntil)
					for _, revision := range pull.page.Revisions {
						revision.DirectoryRef = "subtree:" + current.nodeID + ":" + revision.DirectoryRef
						out.Revisions = append(out.Revisions, revision)
					}
					for _, unknown := range pull.page.Unknowns {
						if visited[unknown.NodeID] {
							continue
						}
						if !allowed(unknown.NodeID) {
							addUnknown(unknown.NodeID, "denied")
							continue
						}
						addUnknown(unknown.NodeID, unknown.Reason)
					}
					for _, target := range pull.targets {
						origin := target.TargetKey.OriginNodeID
						if !allowed(origin) {
							addUnknown(origin, "denied")
							continue
						}
						if in.RevokedNodeIDs[origin] || visited[origin] {
							continue
						}
						via := append([]string{current.nodeID}, target.ViaNodeIDs...)
						if reachable(origin) {
							via = nil
						}
						root := current.nodeID
						if len(via) == 0 {
							root = origin
						}
						if !addTarget(origin, target.TargetKey.CollectionID, root, via) {
							addUnknown(origin, "budget_exhausted")
							stop()
							break
						}
					}
					if pull.page.EnumerationState == "partial" && len(pull.page.Unknowns) == 0 {
						addUnknown(current.nodeID, "unknown")
					}
					out.Children = append(out.Children, ChildManifest{NodeID: current.nodeID, ScopeRef: pull.page.SnapshotID, EnumerationState: pull.page.EnumerationState})
				}
				if pull.reason != "" {
					addUnknown(child.NodeID, pull.reason)
					if pull.reason == "budget_exhausted" {
						stop()
					}
				}
				if !pull.complete && pull.reason == "" {
					addUnknown(child.NodeID, "unknown")
				}
				continue
			}
			if !child.Enumerable() {
				addUnknown(child.NodeID, "enumeration_unsupported")
				enqueue(job{nodeID: child.NodeID, root: current.root, descriptor: childDescriptor})
				continue
			}
			enqueue(job{nodeID: child.NodeID, root: current.root, descriptor: childDescriptor, enumerate: true})
		}
	}
	for origin, via := range routes {
		if len(via) > 0 {
			out.NodeRoutes = append(out.NodeRoutes, NodeRoute{NodeID: origin, ViaNodeIDs: via})
		}
	}
	slices.SortFunc(out.NodeRoutes, func(a, b NodeRoute) int { return strings.Compare(a.NodeID, b.NodeID) })
	out.Consumption = DiscoveryConsumption{Requests: requests, Nodes: nodes}
	out.TargetBudgetUsed = len(out.Targets)
	return out
}
