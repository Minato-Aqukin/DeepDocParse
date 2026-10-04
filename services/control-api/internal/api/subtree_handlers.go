package api

import (
	"context"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
)

func subtreeOptions(r *http.Request) ([]string, int, int, int, string, error) {
	q := r.URL.Query()
	for key, values := range q {
		if len(values) != 1 || !slices.Contains([]string{"path", "max_requests", "max_nodes", "snapshot_id", "cursor", "limit", "allowed_node_ids"}, key) {
			return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "子树参数不合法")
		}
	}
	path := strings.Split(q.Get("path"), ",")
	if len(path) > 64 || len(q.Get("path")) > 4096 {
		return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "调用链过长")
	}
	seen := map[string]bool{}
	for _, id := range path {
		if !nodeIDPath.MatchString(id) || seen[id] {
			return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "调用链无效")
		}
		seen[id] = true
	}
	requests, e1 := strconv.Atoi(q.Get("max_requests"))
	nodes, e2 := strconv.Atoi(q.Get("max_nodes"))
	if e1 != nil || e2 != nil || requests < 0 || requests > 10000 || nodes < 0 || nodes > 10000 {
		return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "发现预算必须为 0..10000")
	}
	limit := 0
	if raw := q.Get("limit"); raw != "" {
		var err error
		limit, err = strconv.Atoi(raw)
		if err != nil || limit < 1 || limit > 100 {
			return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "limit 必须为 1..100")
		}
	}
	boundary := url.Values{"path": {q.Get("path")}, "max_requests": {q.Get("max_requests")}, "max_nodes": {q.Get("max_nodes")}}
	if raw, provided := q["allowed_node_ids"]; provided {
		ids := strings.Split(raw[0], ",")
		if len(ids) > 100 {
			return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "批准边界过长")
		}
		seen := map[string]bool{}
		for _, id := range ids {
			if !nodeIDPath.MatchString(id) || seen[id] {
				return nil, 0, 0, 0, "", apierr.BadRequest("invalid_peer_page", "批准边界无效")
			}
			seen[id] = true
		}
		boundary.Set("allowed_node_ids", raw[0])
	}
	binding := boundary.Encode()
	return path, requests, nodes, limit, peerReadDigest(binding), nil
}

func (s *Server) handlePeerSubtree(w http.ResponseWriter, r *http.Request) error {
	path, requests, nodes, limit, binding, err := subtreeOptions(r)
	if err != nil {
		return err
	}
	issuer, err := discovery.CredentialIssuer(r.Header.Get(discovery.HeaderNodeCredential))
	if err != nil {
		return err
	}
	if path[len(path)-1] != issuer {
		return apierr.Forbidden("credential_scope_denied", "调用链末项必须是凭据签发者")
	}
	id := r.URL.Query().Get("snapshot_id")
	cursor := r.URL.Query().Get("cursor")
	var page *discovery.SubtreePage
	if id != "" {
		page, err = s.store.SubtreeSnapshotPage(r.Context(), s.defaultOrg, issuer, binding, id, cursor, limit)
	} else {
		if cursor != "" {
			return apierr.BadRequest("invalid_peer_page", "cursor 必须与 snapshot_id 一起提供")
		}
		size := limit
		if size == 0 {
			size = 50
		}
		localID := s.nodeIdentity.NodeID()
		var allowed []string
		if raw, provided := r.URL.Query()["allowed_node_ids"]; provided {
			allowed = strings.Split(raw[0], ",")
		}
		remote := discovery.RemoteExpansion{}
		revisions := []discovery.DirectoryRevision{}
		if !slices.Contains(path, localID) {
			snap, e := s.store.CreatePeerMemberSnapshot(r.Context(), s.defaultOrg, localID, 100, 5*time.Minute)
			if e != nil {
				return peerPageError(e)
			}
			members, complete, e := s.store.AuthorizedSnapshotMembers(r.Context(), s.defaultOrg, "", snap.CallerScopeHash, snap.ID, false)
			if e != nil {
				return peerPageError(e)
			}
			approved, e := s.store.ApprovedScopeNodes(r.Context(), s.defaultOrg, true)
			if e != nil {
				return e
			}
			revoked, e := s.store.RevokedScopeNodes(r.Context(), s.defaultOrg)
			if e != nil {
				return e
			}
			ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
			remote = discovery.ExpandScope(ctx, s.peers, discovery.ExpansionInput{Members: members, LocalNodeID: localID, Operation: "search", Path: path, AllowedNodeIDs: allowed, ApprovedNodeIDs: approved, RevokedNodeIDs: revoked, MaxTargets: 10000, MaxRequests: requests, MaxNodes: nodes, Now: s.clock().UTC()})
			cancel()
			for _, member := range members {
				if member.State == discovery.MemberApproved && member.NodeID != localID && !slices.Contains(path, member.NodeID) && (allowed == nil || slices.Contains(allowed, member.NodeID)) && !remote.Handled[member.NodeID] {
					remote.Unknowns = append(remote.Unknowns, discovery.UnknownSubtree{NodeID: member.NodeID, Reason: "unknown"})
				}
			}
			if remote.ValidUntil.IsZero() || snap.ExpiresAt.Before(remote.ValidUntil) {
				remote.ValidUntil = snap.ExpiresAt
			}
			if !complete {
				remote.Unknowns = append(remote.Unknowns, discovery.UnknownSubtree{NodeID: localID, Reason: "unknown"})
			}
			revisions = append(revisions, discovery.DirectoryRevision{NodeID: localID, RegistryRevision: snap.RegistryRevision, FetchedAt: snap.CreatedAt, DirectoryRef: "members", SnapshotRef: snap.ID})
		}
		revisions = append(revisions, remote.Revisions...)
		unknowns := remote.Unknowns
		if unknowns == nil {
			unknowns = []discovery.UnknownSubtree{}
		}
		state := "sealed"
		if len(unknowns) > 0 {
			state = "partial"
		}
		routes := map[string][]string{}
		for _, route := range remote.NodeRoutes {
			routes[route.NodeID] = route.ViaNodeIDs
		}
		targets := make([]discovery.RoutedTarget, 0, len(remote.Targets))
		for _, target := range remote.Targets {
			via := routes[target.OriginNodeID]
			if via == nil {
				via = []string{}
			}
			targets = append(targets, discovery.RoutedTarget{TargetKey: target, ViaNodeIDs: via})
		}
		page, err = s.store.CreateSubtreeSnapshot(r.Context(), s.defaultOrg, issuer, binding, size, discovery.SubtreePage{AuthorityNodeID: localID, ValidUntil: remote.ValidUntil, Revisions: revisions, Unknowns: unknowns, EnumerationState: state, Consumption: remote.Consumption}, targets)
	}
	if err != nil {
		return peerPageError(err)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, page)
}
