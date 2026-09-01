// Package api 是 control-api 的全部 HTTP 处理。
package api

import (
	"context"
	"log/slog"
	"net/http"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

type Server struct {
	cfg      *config.Config
	store    *store.Store
	objects  *objectstore.Store
	sessions *auth.Sessions
	limiter  ratelimit.Limiter
	oidc     *OIDC

	corpus  *proxy.Upstream
	gateway *proxy.Upstream
	mcp     *proxy.Upstream

	// 默认组织。单组织独占部署下，每个请求的组织都是它 ——
	// 但**所有查询仍然带 organization_id**，这样将来上多组织时
	// 要补的是隔离与 RLS，而不是给几十张表加列
	defaultOrg string
}

type Deps struct {
	Config  *config.Config
	Store   *store.Store
	Objects *objectstore.Store
	Limiter ratelimit.Limiter
	OIDC    *OIDC
}

func NewServer(ctx context.Context, d Deps) (*Server, error) {
	s := &Server{
		cfg:      d.Config,
		store:    d.Store,
		objects:  d.Objects,
		sessions: auth.NewSessions(d.Config.JWTSecret, d.Config.JWTTTL),
		limiter:  d.Limiter,
		oidc:     d.OIDC,
	}
	org, err := d.Store.DefaultOrganization(ctx)
	if err != nil {
		return nil, err
	}
	s.defaultOrg = org.ID

	if s.corpus, err = proxy.New("corpus-api", d.Config.CorpusURL, d.Config.ServiceToken); err != nil {
		return nil, err
	}
	if s.gateway, err = proxy.New("model-gateway", d.Config.GatewayURL, d.Config.ServiceToken); err != nil {
		return nil, err
	}
	if s.mcp, err = proxy.New("mcp", d.Config.MCPURL, d.Config.ServiceToken); err != nil {
		return nil, err
	}
	return s, nil
}

// Routes 挂全部路由。
//
// 路由表是**读这个服务的入口**，所以它一处集中、按前缀分组，
// 而不是散落在各个 register 函数里。
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()

	// ---- 无需鉴权 ----
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		_ = httpx.JSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.Handle("GET /readyz", httpx.Wrap(s.handleReady))
	mux.Handle("GET /metrics", obs.MetricsHandler())

	mux.Handle("POST /api/auth/register", httpx.Wrap(s.handleRegister))
	mux.Handle("POST /api/auth/login", httpx.Wrap(s.handleLogin))
	mux.Handle("GET /api/auth/oidc/login", httpx.Wrap(s.handleOIDCLogin))
	mux.Handle("GET /api/auth/oidc/callback", httpx.Wrap(s.handleOIDCCallback))

	// 稳定文件 URL：token 即凭证，不需要会话。
	// **路径必须永远稳定** —— 见 store.FileGrant 的注释
	mux.Handle("GET /files/{token}", httpx.Wrap(s.handleFileByToken))

	// ---- 会话鉴权（/api/*）----
	session := s.requireSession
	mux.Handle("GET /api/auth/me", session(httpx.Wrap(s.handleMe)))

	mux.Handle("GET /api/org", session(httpx.Wrap(s.handleOrg)))
	mux.Handle("GET /api/org/members", session(httpx.Wrap(s.handleMembers)))
	mux.Handle("POST /api/org/members", session(httpx.Wrap(s.handleAddMember)))
	mux.Handle("PATCH /api/org/members/{user_id}", session(httpx.Wrap(s.handleSetMemberRole)))
	mux.Handle("DELETE /api/org/members/{user_id}", session(httpx.Wrap(s.handleRemoveMember)))

	mux.Handle("GET /api/keys", session(httpx.Wrap(s.handleListKeys)))
	mux.Handle("POST /api/keys", session(httpx.Wrap(s.handleCreateKey)))
	mux.Handle("DELETE /api/keys/{key_id}", session(httpx.Wrap(s.handleRevokeKey)))

	mux.Handle("GET /api/usage", session(httpx.Wrap(s.handleUsage)))
	mux.Handle("GET /api/audit", session(httpx.Wrap(s.handleAudit)))

	mux.Handle("POST /api/uploads", session(httpx.Wrap(s.handleCreateUpload)))
	mux.Handle("GET /api/uploads/{upload_id}", session(httpx.Wrap(s.handleGetUpload)))
	mux.Handle("POST /api/uploads/{upload_id}/finalize", session(httpx.Wrap(s.handleFinalizeUpload)))

	mux.Handle("GET /api/documents/{document_id}/download-url",
		session(httpx.Wrap(s.handleDownloadURL)))

	// ---- 语料面：会话鉴权后整段转发给 corpus-api ----
	// **注意顺序**：上面那条更具体的 download-url 必须先注册。
	// ServeMux 按最长模式匹配，所以其实与注册顺序无关 —— 但读代码的人
	// 会按顺序理解，所以还是按具体到宽泛排
	for _, prefix := range corpusPrefixes {
		mux.Handle(prefix, session(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s.corpus.ServeHTTP(w, r, "")
		})))
	}

	// ---- 对外 API：key 鉴权 + 配额 + 限速 + 计量 ----
	mux.Handle("/v1/", s.requireAPIKey(rbac.ScopeParse, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			s.gateway.ServeHTTP(w, r, "")
		})))
	mux.Handle("/mcp", s.requireAPIKey(rbac.ScopeMCP, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			s.mcp.ServeHTTP(w, r, "/mcp")
		})))
	mux.Handle("/mcp/", s.requireAPIKey(rbac.ScopeMCP, http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) {
			s.mcp.ServeHTTP(w, r, "/mcp")
		})))

	return httpx.Chain(mux,
		httpx.Recover,
		httpx.RequestID,
		// **必须排在一切鉴权之前**：剥掉客户端伪造的内部头
		httpx.StripInboundIdentity,
		httpx.SecurityHeaders,
		httpx.CORS(s.cfg.CORSOrigins),
		s.observe,
	)
}

// corpusPrefixes 是转发给语料 API 的全部前缀。
// 加一个语料端点就往这里加一行 —— 漏加的表现是 404，
// 而不是"转发到了错的地方"，所以这是可以接受的失败模式。
var corpusPrefixes = []string{
	"/api/documents",
	"/api/documents/",
	"/api/conversations",
	"/api/conversations/",
	"/api/search",
	"/api/evidence/",
	"/api/extractions/",
	"/api/knowledge/",
	"/api/wiki",
	"/api/wiki/",
	"/api/reviews",
	"/api/reviews/",
}

func (s *Server) observe(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &httpx.StatusRecorder{ResponseWriter: w}
		next.ServeHTTP(rec, r)
		if rec.Status == 0 {
			rec.Status = http.StatusOK
		}
		obs.Observe(r.Method, routePattern(r), rec.Status, time.Since(start))
		slog.DebugContext(r.Context(), "request",
			"method", r.Method, "path", r.URL.Path, "status", rec.Status,
			"ms", time.Since(start).Milliseconds(),
			"request_id", r.Header.Get(identity.HeaderRequestID))
	})
}

// routePattern 用注册的模式而不是真实路径做 metrics 标签。
// **用真实路径会让 /api/documents/{id} 每个 id 变成一个新的时间序列**，
// 那是 Prometheus 基数爆炸最经典的成因。
func routePattern(r *http.Request) string {
	if p := r.Pattern; p != "" {
		return p
	}
	return "other"
}

func (s *Server) handleReady(w http.ResponseWriter, r *http.Request) error {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()

	checks := map[string]string{}
	ok := func(name string, err error) {
		if err != nil {
			checks[name] = "error: " + err.Error()
			return
		}
		checks[name] = "ok"
	}
	ok("postgres", s.store.Ping(ctx))
	ok("objectstore", s.objects.Ping(ctx))

	// outbox 积压也算就绪信号：投不出去的事件意味着文档永远不出现，
	// 而那不该只在别人来问的时候才被发现
	count, oldest, err := s.store.OutboxBacklog(ctx)
	if err != nil {
		checks["outbox"] = "error: " + err.Error()
	} else if oldest > 5*time.Minute {
		checks["outbox"] = "stale: 最老事件 " + oldest.Truncate(time.Second).String()
	} else {
		checks["outbox"] = "ok"
	}

	ready := true
	for _, v := range checks {
		if v != "ok" {
			ready = false
		}
	}
	status := http.StatusOK
	if !ready {
		status = http.StatusServiceUnavailable
	}
	return httpx.JSON(w, status, map[string]any{
		"ready": ready, "checks": checks, "outbox_backlog": count,
	})
}

func mustActor(r *http.Request) (*identity.Actor, error) {
	a := identity.From(r.Context())
	if a == nil {
		return nil, apierr.Internal("缺少 actor 上下文（鉴权中间件没挂）")
	}
	return a, nil
}
