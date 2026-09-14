package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// Peer-facing reads are authenticated by one node-level credential. An
// unconfigured token fails closed (401) and comparison is constant time; the
// corpus peer endpoints use the same rule.
func (s *Server) mountPeer(mux *http.ServeMux) {
	mux.Handle("GET /api/v1/federation/members", s.requirePeerCredentials(httpx.Wrap(s.handlePeerMembers)))
	mux.Handle("GET /api/v1/federation/collections", s.requirePeerCredentials(httpx.Wrap(s.handlePeerCollections)))
}

func (s *Server) requirePeerCredentials(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := s.discoveryReady(); err != nil {
			apierr.Write(w, r, err)
			return
		}
		configured := s.cfg.FederationPeerToken
		presented := r.Header.Get(discovery.HeaderPeerToken)
		if configured == "" || presented == "" || subtle.ConstantTimeCompare([]byte(configured), []byte(presented)) != 1 {
			apierr.Write(w, r, apierr.Unauthorized("peer_unauthenticated", "缺少或无效的同伴凭据"))
			return
		}
		if target := r.Header.Get(discovery.HeaderPeerTarget); target != "" && target != s.nodeIdentity.NodeID() {
			apierr.Write(w, r, apierr.Conflict("wrong_target", "请求指向另一个节点"))
			return
		}
		next.ServeHTTP(w, r)
	})
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
	limit := 50
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
		snap, err := s.store.CreatePeerMemberSnapshot(r.Context(), s.defaultOrg, s.nodeIdentity.NodeID(), limit, 5*time.Minute)
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
