package api

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func (s *Server) mountDiscovery(mux *http.ServeMux) {
	s.mountClient(mux)
	s.mountScopes(mux)
	s.mountPeer(mux)
	mux.Handle("GET /api/v1/federation/node", httpx.Wrap(s.handleFederationNode))
	for _, path := range []string{"/api/v1/capabilities", "/api/v1/federation/capabilities", "/api/v1/client/handshake"} {
		mux.Handle("GET "+path, s.discoveryAuth(httpx.Wrap(s.handleCapabilities)))
	}
	mux.Handle("GET /api/v1/federation/nodes", s.requireSession(httpx.Wrap(s.handleNodeList)))
	mux.Handle("POST /api/v1/federation/nodes", s.requireSession(httpx.Wrap(s.handleNodeRegister)))
	mux.Handle("POST /api/v1/federation/nodes/{node_id}/approve", s.requireSession(httpx.Wrap(s.handleNodeApprove)))
	mux.Handle("POST /api/v1/federation/nodes/{node_id}/revoke", s.requireSession(httpx.Wrap(s.handleNodeRevoke)))
	mux.Handle("POST /api/v1/federation/member-snapshots", s.discoveryAuth(httpx.Wrap(s.handleSnapshotCreate)))
	mux.Handle("GET /api/v1/federation/member-snapshots/{snapshot_id}/members", s.discoveryAuth(httpx.Wrap(s.handleSnapshotPage)))
}
func (s *Server) discoveryAuth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if auth.LooksLikeAPIKey(bearer(r)) {
			s.requireAPIKey(rbac.ScopeRead, next).ServeHTTP(w, r)
			return
		}
		s.requireSession(next).ServeHTTP(w, r)
	})
}
func (s *Server) discoveryReady() error {
	if s.nodeIdentity == nil {
		return apierr.New(503, apierr.TypeInternal, "node_identity_unavailable", "持久节点身份未初始化")
	}
	return nil
}
func (s *Server) handleFederationNode(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	w.Header().Set("Cache-Control", "no-store")
	d := discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: s.nodeIdentity.NodeID(), ProtocolVersions: []string{"ddp-discovery/1", "ddp-client/1"}, ControlledEndpoints: []discovery.Endpoint{{Purpose: "federation", URL: strings.TrimRight(s.cfg.PublicBaseURL, "/") + "/api/v1/federation"}}, AuthMethods: []string{"session", "user_api_key"}, DiscoveryCapabilities: discovery.DiscoveryCapabilities{EnumerateMembers: true, CatalogEvents: false}, Revision: s.nodeRevision, ValidUntil: time.Now().UTC().Add(5 * time.Minute)}
	body := map[string]any{"authority_node_id": s.nodeIdentity.NodeID(), "public_key": s.nodeIdentity.PublicKey(), "key_fingerprint": s.nodeIdentity.Fingerprint(), "descriptor": d}
	if nonces, ok := r.URL.Query()["challenge"]; ok {
		if len(nonces) != 1 || !discovery.ValidChallenge(nonces[0]) {
			return apierr.BadRequest("invalid_node_challenge", "challenge 必须为32..64字符无padding的base64url nonce")
		}
		proof, err := s.nodeIdentity.Proof(nonces[0], s.cfg.PublicBaseURL, time.Now())
		if err != nil {
			return err
		}
		body["proof"] = proof
	}
	return httpx.JSON(w, 200, body)
}

// Only a configured internal producer may report current capability health. Registered
// directory endpoints never participate in this request and redirects are prohibited.
func (s *Server) capabilityProfiles(ctx context.Context, a *identity.Actor) ([]discovery.CapabilityProfile, string) {
	unknown := func() ([]discovery.CapabilityProfile, string) { return []discovery.CapabilityProfile{}, "unknown" }
	if s.corpus == nil {
		return unknown()
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(s.cfg.CorpusURL, "/")+"/internal/capabilities", nil)
	if err != nil {
		return unknown()
	}
	// This endpoint reports node health and requires the control service itself.
	// Resource/client reads still forward the freshly authenticated user's actor.
	controlServiceActor().Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return unknown()
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return unknown()
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, (1<<20)+1))
	if err != nil {
		return unknown()
	}
	return discovery.ProjectProfiles(body, s.nodeIdentity.NodeID(), time.Now())
}
func (s *Server) handleCapabilities(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	if a.UserID == "" {
		return apierr.Unauthorized("principal_required", "缺少已验证用户")
	}
	profiles, status := s.capabilityProfiles(r.Context(), a)
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, map[string]any{"protocol_version": "ddp-client/1", "identity": map[string]string{"environment_id": s.nodeIdentity.NodeID(), "authority_node_id": s.nodeIdentity.NodeID(), "workspace_id": a.OrganizationID}, "profile": map[string]string{"issuer": s.nodeIdentity.NodeID(), "subject": a.UserID}, "capabilities": s.clientCapabilities(r.Context()), "profiles": profiles, "capability_status": status, "accepting_admissions": acceptingAdmissions(profiles, status)})
}

// acceptingAdmissions is the node-level roll-up of the producer's per-operation
// claims. It is true only when the observation is current and at least one
// profile declares acceptance; an absent or unknown corpus value stays false and
// control never invents willingness on its own.
func acceptingAdmissions(profiles []discovery.CapabilityProfile, status string) bool {
	if status != "observed" {
		return false
	}
	for _, p := range profiles {
		if p.AcceptingAdmissions {
			return true
		}
	}
	return false
}
func (s *Server) discoveryAdmin(r *http.Request) (*identity.Actor, error) {
	a, err := mustActor(r)
	if err != nil {
		return nil, err
	}
	if err = s.discoveryReady(); err != nil {
		return nil, err
	}
	if a.Kind != identity.KindUser {
		return nil, apierr.Forbidden("session_required", "节点管理需要管理员会话")
	}
	if err = requireRole(a, rbac.Role.CanManageOrg, "管理节点目录"); err != nil {
		return nil, err
	}
	return a, nil
}
func discoveryError(err error) error {
	switch {
	case errors.Is(err, store.ErrNotFound):
		return apierr.NotFound("discovery_not_found", "目录项或游标不可用")
	case errors.Is(err, store.ErrSnapshotExpired):
		return apierr.New(410, apierr.TypeInvalidRequest, "scope_expired", "成员快照已过期，请创建新范围")
	case errors.Is(err, store.ErrDiscoveryConflict):
		return apierr.Conflict("discovery_revision_conflict", "目录修订或审批状态不允许此修改")
	default:
		return err
	}
}
func (s *Server) handleNodeRegister(w http.ResponseWriter, r *http.Request) error {
	a, err := s.discoveryAdmin(r)
	if err != nil {
		return err
	}
	var in discovery.Registration
	if err = httpx.DecodeJSON(r, &in); err != nil {
		return err
	}
	if err = in.Descriptor.Validate(in.PublicKey, s.nodeIdentity.NodeID(), time.Now()); err != nil {
		return apierr.BadRequest("invalid_node_descriptor", "节点身份、协议或入口描述无效")
	}
	if len(in.AllowedSubjects) > 100 {
		return apierr.BadRequest("invalid_scope", "成员共享列表过长")
	}
	rev, err := s.store.RegisterNode(r.Context(), a.OrganizationID, in)
	if err != nil {
		return discoveryError(err)
	}
	s.store.Audit(r.Context(), a.OrganizationID, a.ID, string(a.Kind), "node.register", in.Descriptor.NodeID, a.RequestID, map[string]any{"revision": rev})
	return httpx.JSON(w, 201, map[string]any{"node_id": in.Descriptor.NodeID, "state": discovery.MemberPending, "registry_revision": rev, "health": "unknown", "accepting_admissions": false})
}
func (s *Server) handleNodeList(w http.ResponseWriter, r *http.Request) error {
	a, err := s.discoveryAdmin(r)
	if err != nil {
		return err
	}
	items, err := s.store.ListNodes(r.Context(), a.OrganizationID, s.nodeIdentity.NodeID())
	if err != nil {
		return err
	}
	return httpx.JSON(w, 200, map[string]any{"members": items})
}
func (s *Server) handleNodeApprove(w http.ResponseWriter, r *http.Request) error {
	return s.changeNodeState(w, r, discovery.MemberApproved)
}
func (s *Server) handleNodeRevoke(w http.ResponseWriter, r *http.Request) error {
	return s.changeNodeState(w, r, discovery.MemberRevoked)
}
func (s *Server) changeNodeState(w http.ResponseWriter, r *http.Request, state string) error {
	a, err := s.discoveryAdmin(r)
	if err != nil {
		return err
	}
	node := r.PathValue("node_id")
	rev, err := s.store.SetNodeState(r.Context(), a.OrganizationID, node, state)
	if err != nil {
		return discoveryError(err)
	}
	s.store.Audit(r.Context(), a.OrganizationID, a.ID, string(a.Kind), "node."+state, node, a.RequestID, map[string]any{"revision": rev})
	return httpx.JSON(w, 200, map[string]any{"node_id": node, "state": state, "registry_revision": rev, "health": "unknown", "accepting_admissions": false})
}
func (s *Server) handleSnapshotCreate(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	if a.UserID == "" {
		return apierr.Unauthorized("principal_required", "缺少已验证用户")
	}
	in := struct {
		PageSize   int `json:"page_size"`
		TTLSeconds int `json:"ttl_seconds"`
	}{50, 900}
	if r.ContentLength != 0 {
		if err = httpx.DecodeJSON(r, &in); err != nil {
			return err
		}
	}
	if in.PageSize < 1 || in.PageSize > 100 || in.TTLSeconds < 30 || in.TTLSeconds > 3600 {
		return apierr.BadRequest("invalid_snapshot_options", "page_size 必须1..100，ttl_seconds 必须30..3600")
	}
	snap, err := s.store.CreateMemberSnapshot(r.Context(), a.OrganizationID, a.UserID, discovery.ScopeHash(a), s.nodeIdentity.NodeID(), a.Role.CanManageOrg(), in.PageSize, time.Duration(in.TTLSeconds)*time.Second)
	if err != nil {
		return discoveryError(err)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 201, snap)
}
func (s *Server) handleSnapshotPage(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	a, err := mustActor(r)
	if err != nil {
		return err
	}
	page, err := s.store.MemberSnapshotPage(r.Context(), a.OrganizationID, a.UserID, discovery.ScopeHash(a), r.PathValue("snapshot_id"), r.URL.Query().Get("cursor"), a.Role.CanManageOrg())
	if err != nil {
		return discoveryError(err)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, page)
}
