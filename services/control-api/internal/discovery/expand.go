package discovery

import (
	"context"
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
	Revisions        []DirectoryRevision
	Children         []ChildManifest
	Unknowns         []UnknownSubtree
	TargetBudgetUsed int
	ValidUntil       time.Time
}

type ExpansionInput struct {
	Members     []Member
	LocalNodeID string
	Operation   string
	MaxTargets  int
	MaxRequests int
	MaxNodes    int
	Now         time.Time
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
	cursor, snapshotID := "", ""
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
		page, reason := d.MembersPage(ctx, cfg, snapshotID, cursor, limit)
		*requests++
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
	cursor, snapshotID := "", ""
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
		page, reason := d.CollectionsPage(ctx, cfg, snapshotID, cursor, limit)
		*requests++
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
	scheduled := map[string]bool{}
	targetSeen := map[string]bool{}

	addTarget := func(origin, collectionID, root string) bool {
		key := origin + "\x00" + collectionID
		if targetSeen[key] {
			return true
		}
		if len(out.Targets) >= in.MaxTargets {
			return false
		}
		targetSeen[key] = true
		out.Targets = append(out.Targets, TargetKey{OriginNodeID: origin, CollectionID: collectionID, Operation: in.Operation})
		out.Sources[origin] = root
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
		if member.State != MemberApproved {
			continue
		}
		out.Handled[member.NodeID] = true
		if member.Descriptor == nil || !member.Descriptor.ValidUntil.After(now) || !hasFederationEndpoint(*member.Descriptor) {
			addUnknown(member.NodeID, "unknown")
			continue
		}
		_, configured := dir.Configured(member.NodeID)
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
		if nodes >= in.MaxNodes {
			stop()
			addUnknown(current.nodeID, "budget_exhausted")
			continue
		}
		visited[current.nodeID] = true
		nodes++
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
				if !addTarget(current.nodeID, item.CollectionID, current.root) {
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
		for _, child := range membersPull.items {
			// Re-entering the local node or an already scheduled directory is the
			// loop/duplicate path itself, not an unexpanded subtree.
			if child.NodeID == in.LocalNodeID || visited[child.NodeID] || scheduled[child.NodeID] {
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
			if !configured {
				addUnknown(child.NodeID, "unknown")
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
	out.TargetBudgetUsed = len(out.Targets)
	return out
}
