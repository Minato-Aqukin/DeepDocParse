package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// Each directory request is signed for this receiver and consumed once.
// SERVICE_TOKEN remains local to the control-to-corpus hop below.
func (s *Server) mountPeer(mux *http.ServeMux) {
	mux.Handle("GET /api/v1/federation/members", s.requirePeerCredentials(httpx.Wrap(s.handlePeerMembers)))
	mux.Handle("GET /api/v1/federation/collections", s.requirePeerCredentials(httpx.Wrap(s.handlePeerCollections)))
	mux.Handle("GET /api/v1/federation/generation-descriptor", s.requirePeerCredentials(httpx.Wrap(s.handlePeerGenerationDescriptor)))
}

func (s *Server) requirePeerCredentials(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := s.discoveryReady(); err != nil {
			apierr.Write(w, r, err)
			return
		}
		if err := s.authenticatePeerRead(r); err != nil {
			apierr.Write(w, r, err)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func peerReadOperation(path string) (string, error) {
	switch path {
	case "/api/v1/federation/members":
		return string(contracts.NodeCredentialOperationDirectoryMembersRead), nil
	case "/api/v1/federation/collections":
		return string(contracts.NodeCredentialOperationDirectoryCollectionsRead), nil
	case "/api/v1/federation/generation-descriptor":
		return string(contracts.NodeCredentialOperationDirectoryCapabilitiesRead), nil
	default:
		return "", discovery.ErrCredentialInvalid
	}
}

func peerReadDigest(value string) string {
	digest := sha256.Sum256([]byte(value))
	return "sha256:" + hex.EncodeToString(digest[:])
}

func (s *Server) signPeerRead(ctx context.Context, cfg discovery.PeerConfig, request *http.Request) error {
	record, err := s.peerTrust().PeerTrust(ctx, s.defaultOrg, cfg.NodeID)
	if refusal := trustRefusal(record, err, http.StatusForbidden); refusal != nil {
		return refusal
	}
	endpoint, err := url.Parse(cfg.Endpoint)
	if err != nil {
		return err
	}
	route := strings.TrimPrefix(request.URL.Path, endpoint.Path)
	operation, err := peerReadOperation(route)
	if err != nil {
		return err
	}
	jti, err := discovery.NewCredentialJTI()
	if err != nil {
		return err
	}
	now := s.clock().UTC().Unix()
	claims := discovery.CredentialClaims{
		Schema: discovery.CredentialSchema, Alg: discovery.CredentialAlg,
		IssuerNodeID: s.nodeIdentity.NodeID(), AudienceNodeID: cfg.NodeID,
		Actor:     discovery.CredentialActor{OrganizationID: s.defaultOrg, Subject: "control-api", Kind: "service"},
		Operation: operation, IssuedAt: now, ExpiresAt: now + 60, JTI: jti,
		Constraints: discovery.CredentialConstraints{RootTaskID: "directory:" + jti, ScopeRef: peerReadDigest(request.URL.Query().Encode())},
		Request:     discovery.CredentialRequest{Method: request.Method, Path: route, BodyDigest: peerReadDigest("")},
	}
	token, err := s.nodeIdentity.SignCredential(claims)
	if err != nil {
		return err
	}
	request.Header.Set(discovery.HeaderNodeCredential, token)
	return nil
}

func (s *Server) authenticatePeerRead(r *http.Request) error {
	token := r.Header.Get(discovery.HeaderNodeCredential)
	issuer, err := discovery.CredentialIssuer(token)
	if err != nil {
		return apierr.Unauthorized("credential_invalid", "缺少或无效的节点凭据")
	}
	record, err := s.peerTrust().PeerTrust(r.Context(), s.defaultOrg, issuer)
	if refusal := trustRefusal(record, err, http.StatusForbidden); refusal != nil {
		return refusal
	}
	derived, err := discovery.NodeIDForPublicKey(record.PublicKey)
	if err != nil || derived != issuer {
		return apierr.Unauthorized("credential_invalid", "签发者身份与公钥不符")
	}
	claims, err := discovery.VerifyCredential(token, record.PublicKey)
	if err != nil {
		return apierr.Unauthorized("credential_invalid", "节点签名无效")
	}
	if claims.AudienceNodeID != s.nodeIdentity.NodeID() {
		return apierr.Forbidden("credential_audience_mismatch", "凭据不是签给本节点")
	}
	now := s.clock().UTC().Unix()
	if claims.ExpiresAt <= now || claims.IssuedAt > now+30 {
		return apierr.Unauthorized("credential_expired", "节点凭据不在有效期内")
	}
	operation, err := peerReadOperation(r.URL.Path)
	if err != nil || claims.Operation != operation {
		return apierr.Forbidden("credential_operation_denied", "凭据未授权此目录操作")
	}
	if claims.Request.Method != r.Method || claims.Request.Path != r.URL.Path ||
		claims.Request.BodyDigest != peerReadDigest("") || r.ContentLength != 0 ||
		claims.Constraints.ScopeRef != peerReadDigest(r.URL.Query().Encode()) ||
		claims.Actor.Kind != "service" || claims.Actor.Subject != "control-api" {
		return apierr.Forbidden("credential_scope_denied", "凭据与请求或查询范围不符")
	}
	if target := r.Header.Get(discovery.HeaderPeerTarget); target != "" && target != s.nodeIdentity.NodeID() {
		return apierr.Conflict("wrong_target", "请求指向另一个节点")
	}
	if s.store == nil {
		return apierr.New(503, apierr.TypeUpstream, "credential_store_unavailable", "重放保护存储不可用")
	}
	consumed, err := s.store.ConsumePeerCredential(r.Context(), s.defaultOrg, claims, record.PublicKey)
	if err != nil {
		return err
	}
	if !consumed {
		latest, lookupErr := s.peerTrust().PeerTrust(r.Context(), s.defaultOrg, issuer)
		if refusal := trustRefusal(latest, lookupErr, http.StatusForbidden); refusal != nil {
			return refusal
		}
		return apierr.Unauthorized("credential_replayed", "节点凭据已使用")
	}
	return nil
}

func peerPageError(err error) error {
	if errors.Is(err, store.ErrDiscoveryConflict) {
		return apierr.BadRequest("invalid_peer_page", "快照或分页参数不可用")
	}
	return discoveryError(err)
}

func (s *Server) handlePeerMembers(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	snapshotID := r.URL.Query().Get("snapshot_id")
	cursor := r.URL.Query().Get("cursor")
	limit := 0
	if raw := r.URL.Query().Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > 100 {
			return apierr.BadRequest("invalid_peer_page", "limit 必须为 1..100")
		}
		limit = n
	}
	if snapshotID == "" {
		if cursor != "" {
			return apierr.BadRequest("invalid_peer_page", "cursor 必须与 snapshot_id 一起提供")
		}
		pageSize := limit
		if pageSize == 0 {
			pageSize = 50
		}
		snap, err := s.store.CreatePeerMemberSnapshot(r.Context(), s.defaultOrg, s.nodeIdentity.NodeID(), pageSize, 5*time.Minute)
		if err != nil {
			return peerPageError(err)
		}
		snapshotID, cursor = snap.ID, snap.FirstCursor
		limit = 0
	}
	page, err := s.store.PeerMemberSnapshotPage(r.Context(), s.defaultOrg, s.nodeIdentity.NodeID(), snapshotID, cursor, limit)
	if err != nil {
		return peerPageError(err)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, page)
}

type peerCatalogResponse struct {
	AuthorityNodeID  string            `json:"authority_node_id"`
	SnapshotID       string            `json:"snapshot_id"`
	RegistryRevision int64             `json:"registry_revision"`
	CreatedAt        time.Time         `json:"created_at"`
	ValidUntil       time.Time         `json:"valid_until"`
	FirstCursor      string            `json:"first_cursor"`
	TerminalCursor   string            `json:"terminal_cursor"`
	Total            int               `json:"total"`
	Collections      []json.RawMessage `json:"collections"`
	NextCursor       *string           `json:"next_cursor"`
	Complete         bool              `json:"complete"`
}

type corpusCatalogPage struct {
	SnapshotID       string            `json:"snapshot_id"`
	ScopeID          string            `json:"scope_id"`
	CallerScopeHash  string            `json:"caller_scope_hash"`
	OriginNodeID     string            `json:"origin_node_id"`
	RegistryRevision int64             `json:"registry_revision"`
	CreatedAt        time.Time         `json:"created_at"`
	ValidUntil       time.Time         `json:"valid_until"`
	FirstCursor      string            `json:"first_cursor"`
	TerminalCursor   string            `json:"terminal_cursor"`
	Total            *int              `json:"total"`
	Collections      []json.RawMessage `json:"collections"`
	NextCursor       *string           `json:"next_cursor"`
	Complete         bool              `json:"complete"`
}

// handlePeerCollections proxies one stable page of this node's published
// collection descriptors. Corpus owns publication and ACL; control only
// supplies its service identity and re-checks that every descriptor is
// originated by this node before a peer sees it.
func (s *Server) handlePeerCollections(w http.ResponseWriter, r *http.Request) error {
	if err := s.discoveryReady(); err != nil {
		return err
	}
	if s.corpus == nil {
		return apierr.New(503, apierr.TypeUpstream, "upstream_unreachable", "语料服务未连接")
	}
	q := url.Values{}
	if v := r.URL.Query().Get("snapshot_id"); v != "" {
		q.Set("snapshot_id", v)
	}
	if v := r.URL.Query().Get("cursor"); v != "" {
		q.Set("cursor", v)
	}
	if v := r.URL.Query().Get("limit"); v != "" {
		q.Set("limit", v)
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(s.cfg.CorpusURL, "/")+"/internal/federation/published-collections?"+q.Encode(), nil)
	if err != nil {
		return apierr.Internal("无法构造语料目录请求")
	}
	service := controlServiceActor()
	service.OrganizationID = s.defaultOrg
	service.Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) {
			return apierr.New(504, apierr.TypeUpstream, "upstream_timeout", "语料目录请求超时")
		}
		return apierr.New(502, apierr.TypeUpstream, "upstream_unreachable", "语料目录不可达")
	}
	defer resp.Body.Close()
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, (8<<20)+1))
	if readErr != nil || len(body) > 8<<20 {
		return apierr.New(502, apierr.TypeUpstream, "upstream_invalid", "语料目录响应超限")
	}
	if resp.StatusCode != 200 {
		var failure struct {
			Error struct {
				Message string `json:"message"`
				Type    string `json:"type"`
				Code    string `json:"code"`
			} `json:"error"`
		}
		_ = json.Unmarshal(body, &failure)
		code := failure.Error.Code
		if code == "" {
			code = "upstream_error"
		}
		message := failure.Error.Message
		if message == "" {
			message = "语料目录拒绝了请求"
		}
		return apierr.New(resp.StatusCode, apierr.TypeUpstream, code, message)
	}
	var page corpusCatalogPage
	if json.Unmarshal(body, &page) != nil {
		return apierr.New(502, apierr.TypeUpstream, "upstream_invalid", "语料目录响应无法解析")
	}
	if page.SnapshotID == "" || page.OriginNodeID != s.nodeIdentity.NodeID() || page.RegistryRevision < 1 ||
		page.CreatedAt.IsZero() || page.ValidUntil.IsZero() || !page.ValidUntil.After(page.CreatedAt) ||
		page.FirstCursor == "" || page.TerminalCursor == "" || page.Total == nil || *page.Total < 0 || *page.Total > 10000 {
		return apierr.New(502, apierr.TypeUpstream, "upstream_invalid", "语料目录响应与节点身份不一致")
	}
	out := peerCatalogResponse{
		AuthorityNodeID:  s.nodeIdentity.NodeID(),
		SnapshotID:       page.SnapshotID,
		RegistryRevision: page.RegistryRevision,
		CreatedAt:        page.CreatedAt,
		ValidUntil:       page.ValidUntil,
		FirstCursor:      page.FirstCursor,
		TerminalCursor:   page.TerminalCursor,
		Total:            *page.Total,
		Collections:      []json.RawMessage{},
		NextCursor:       page.NextCursor,
		Complete:         page.Complete,
	}
	for _, raw := range page.Collections {
		var identity struct {
			CollectionID string `json:"collection_id"`
			OriginNodeID string `json:"origin_node_id"`
		}
		if json.Unmarshal(raw, &identity) != nil || identity.CollectionID == "" || identity.OriginNodeID != s.nodeIdentity.NodeID() {
			return apierr.New(502, apierr.TypeUpstream, "upstream_invalid", "语料目录含有非本节点来源的集合")
		}
		out.Collections = append(out.Collections, raw)
	}
	w.Header().Set("Cache-Control", "no-store")
	return httpx.JSON(w, 200, out)
}
