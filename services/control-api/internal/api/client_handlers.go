package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func (s *Server) mountClient(mux *http.ServeMux) {
	for _, path := range []string{"GET /api/v1/client/snapshot", "GET /api/v1/client/events", "GET /api/v1/client/receipts/{operation_key}", "POST /api/v1/client/query"} {
		mux.Handle(path, s.discoveryAuth(s.domainThrottle(httpx.Wrap(s.handleClientRead))))
	}
	mux.Handle("POST /api/v1/client/commands", s.discoveryAuth(httpx.Wrap(func(w http.ResponseWriter, r *http.Request) error {
		return apierr.Conflict("approved_plan_required", "远端操作需要已批准计划")
	})))
}

func (s *Server) handleClientRead(w http.ResponseWriter, r *http.Request) error {
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
	if s.corpus == nil {
		return apierr.New(503, apierr.TypeUpstream, "upstream_unreachable", "语料服务未连接")
	}
	// The caller cannot choose a scope, even with an otherwise valid signed credential.
	r.Header.Set(identity.HeaderClientScope, discovery.ScopeHash(a))
	r.Header.Set(identity.HeaderAuthorityNode, s.nodeIdentity.NodeID())
	if r.Method == http.MethodPost {
		body, readErr := io.ReadAll(io.LimitReader(r.Body, 65537))
		if readErr != nil || len(body) > 65536 {
			return apierr.BadRequest("input_too_large", "读取查询参数过大")
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
	}
	w.Header().Set("Cache-Control", "no-store")
	s.corpus.ServeHTTP(w, r, "")
	return nil
}

func controlServiceActor() *identity.Actor {
	return &identity.Actor{Kind: identity.KindService, ID: "control-api", OrganizationID: "control-system", Role: rbac.Admin}
}

func (s *Server) clientCapabilities(ctx context.Context) []string {
	empty := []string{}
	if s.corpus == nil {
		return empty
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(s.cfg.CorpusURL, "/")+"/internal/client/protocol", nil)
	if err != nil {
		return empty
	}
	controlServiceActor().Apply(req, "control-api")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	client := &http.Client{Transport: s.corpus.Transport(), CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		return empty
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return empty
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	if err != nil || len(body) > 65536 {
		return empty
	}
	var value struct {
		Protocol     string   `json:"protocol_version"`
		Capabilities []string `json:"capabilities"`
	}
	if json.Unmarshal(body, &value) != nil || value.Protocol != "ddp-client/1" {
		return empty
	}
	allowed := map[string]bool{"client.snapshot": true, "client.events": true, "client.receipt": true, "client.query": true, "client.windows": true}
	out := []string{}
	for _, capability := range value.Capabilities {
		if allowed[capability] {
			out = append(out, capability)
		}
	}
	return out
}
