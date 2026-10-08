package discovery

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"slices"
	"strings"
	"time"
)

type NodeRoute struct {
	NodeID     string   `json:"node_id"`
	ViaNodeIDs []string `json:"via_node_ids"`
}
type RoutedTarget struct {
	TargetKey  TargetKey `json:"target_key"`
	ViaNodeIDs []string  `json:"via_node_ids"`
}
type DiscoveryConsumption struct {
	Requests int `json:"requests"`
	Nodes    int `json:"nodes"`
}
type SubtreePage struct {
	AuthorityNodeID  string               `json:"authority_node_id"`
	Operation        string               `json:"operation"`
	SnapshotID       string               `json:"snapshot_id"`
	CreatedAt        time.Time            `json:"created_at"`
	ValidUntil       time.Time            `json:"valid_until"`
	FirstCursor      string               `json:"first_cursor"`
	TerminalCursor   string               `json:"terminal_cursor"`
	Targets          []RoutedTarget       `json:"targets"`
	Revisions        []DirectoryRevision  `json:"registry_revision_vector"`
	Unknowns         []UnknownSubtree     `json:"unexpanded_subtrees"`
	EnumerationState string               `json:"enumeration_state"`
	Consumption      DiscoveryConsumption `json:"consumption"`
	NextCursor       *string              `json:"next_cursor"`
	Complete         bool                 `json:"complete"`
}

type subtreePull struct {
	page     *SubtreePage
	targets  []RoutedTarget
	complete bool
	reason   string
}

// A subtree report's consumption is charged once, not again on every page.
// Physical paging reads are charged independently, including failed reads.
func (d *PeerDirectory) pullSubtree(ctx context.Context, cfg PeerConfig, path, allowed []string, requests *int, maxRequests, maxNodes int) subtreePull {
	out := subtreePull{}
	share := maxRequests - *requests - 1
	if share < 0 || maxNodes < 0 {
		out.reason = "budget_exhausted"
		return out
	}
	q := url.Values{"path": {strings.Join(path, ",")}, "max_requests": {fmt.Sprint(share)}, "max_nodes": {fmt.Sprint(maxNodes)}, "limit": {"100"}}
	if allowed != nil {
		q.Set("allowed_node_ids", strings.Join(allowed, ","))
	}
	seen := map[string]bool{}
	for {
		if *requests >= maxRequests {
			out.reason = "budget_exhausted"
			return out
		}
		body, _, reason := d.peerGet(ctx, cfg, "/api/v1/federation/subtree", q, requests)
		if reason != "" {
			out.reason = reason
			return out
		}
		var page SubtreePage
		if json.Unmarshal(body, &page) != nil || !page.valid(cfg.NodeID, path) {
			out.reason = "unknown"
			return out
		}
		if out.page == nil {
			if page.Consumption.Requests > share || page.Consumption.Nodes > maxNodes {
				out.reason = "budget_exhausted"
				return out
			}
			out.page = &page
			*requests += page.Consumption.Requests
		} else {
			first := out.page
			if page.SnapshotID != first.SnapshotID || page.Operation != first.Operation || !page.CreatedAt.Equal(first.CreatedAt) || !page.ValidUntil.Equal(first.ValidUntil) || page.FirstCursor != first.FirstCursor || page.TerminalCursor != first.TerminalCursor || page.Consumption != first.Consumption || page.EnumerationState != first.EnumerationState || !equalJSON(page.Revisions, first.Revisions) || !equalJSON(page.Unknowns, first.Unknowns) {
				out.reason = "unknown"
				out.targets = nil
				return out
			}
		}
		cursor := q.Get("cursor")
		if cursor == "" {
			cursor = page.FirstCursor
		}
		if seen[cursor] {
			out.reason = "unknown"
			return out
		}
		seen[cursor] = true
		if cursor == page.TerminalCursor {
			if !page.Complete || page.NextCursor != nil {
				out.reason = "unknown"
				return out
			}
			out.targets = append(out.targets, page.Targets...)
			out.complete = true
			return out
		}
		if page.Complete || page.NextCursor == nil || *page.NextCursor == "" {
			out.reason = "unknown"
			return out
		}
		if len(out.targets)+len(page.Targets) > 10000 {
			out.reason = "budget_exhausted"
			return out
		}
		out.targets = append(out.targets, page.Targets...)
		q.Set("snapshot_id", page.SnapshotID)
		q.Set("cursor", *page.NextCursor)
		q.Del("limit")
	}
}
func equalJSON(a, b any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return string(x) == string(y)
}
func (p SubtreePage) valid(authority string, path []string) bool {
	if p.AuthorityNodeID != authority || p.Operation == "" || p.SnapshotID == "" || p.CreatedAt.IsZero() || !p.ValidUntil.After(p.CreatedAt) || p.FirstCursor == "" || p.TerminalCursor == "" || len(p.Targets) > 100 || p.Consumption.Requests < 0 || p.Consumption.Nodes < 0 || (p.EnumerationState != "sealed" && p.EnumerationState != "partial") || p.Targets == nil || p.Revisions == nil || p.Unknowns == nil {
		return false
	}
	if p.EnumerationState == "sealed" && len(p.Unknowns) > 0 {
		return false
	}
	for _, t := range p.Targets {
		if !peerNodePattern.MatchString(t.TargetKey.OriginNodeID) || t.TargetKey.CollectionID == "" || t.TargetKey.Operation == "" || t.TargetKey.OriginNodeID == authority || slices.Contains(path, t.TargetKey.OriginNodeID) || t.ViaNodeIDs == nil {
			return false
		}
		seen := map[string]bool{}
		for _, via := range t.ViaNodeIDs {
			if !peerNodePattern.MatchString(via) || via == authority || via == t.TargetKey.OriginNodeID || slices.Contains(path, via) || seen[via] {
				return false
			}
			seen[via] = true
		}
	}
	for _, r := range p.Revisions {
		if !peerNodePattern.MatchString(r.NodeID) || r.RegistryRevision < 1 || r.FetchedAt.IsZero() {
			return false
		}
	}
	for _, u := range p.Unknowns {
		if !peerNodePattern.MatchString(u.NodeID) || !slices.Contains([]string{"unknown", "denied", "timeout", "enumeration_unsupported", "budget_exhausted"}, u.Reason) {
			return false
		}
	}
	return true
}

func routeLess(a, b []string) bool {
	if len(a) != len(b) {
		return len(a) < len(b)
	}
	return slices.Compare(a, b) < 0
}
