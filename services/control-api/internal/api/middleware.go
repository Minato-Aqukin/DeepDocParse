package api

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func bearer(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// requireSession 校验浏览器会话。
//
// **每次都回查 membership**，不信任 JWT 里的 role：
// 把角色写进 token 是常见做法，但那意味着"降级一个管理员"要等到他的
// token 过期（默认 7 天）才生效 —— 而降权的场合往往正是最急的场合。
// 多一次索引查询换即时生效，值。
func (s *Server) requireSession(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearer(r)
		if token == "" {
			// Cookie 兜底：前端可以选择用 HttpOnly cookie 而不是 localStorage
			if c, err := r.Cookie("ddp_session"); err == nil {
				token = c.Value
			}
		}
		if token == "" {
			apierr.Write(w, r, apierr.Unauthorized("no_session", "缺少会话凭据"))
			return
		}
		// API key 走错了门：给一句说得清的错，而不是"会话无效"
		if auth.LooksLikeAPIKey(token) {
			apierr.Write(w, r, apierr.Unauthorized("api_key_on_session_route",
				"/api/* 用浏览器会话，API key 请调 /v1/*"))
			return
		}
		claims, err := s.sessions.Verify(token)
		if err != nil {
			apierr.Write(w, r, apierr.Unauthorized("invalid_session", "会话无效或已过期").WithCause(err))
			return
		}
		user, err := s.store.UserByID(r.Context(), claims.OrganizationID, claims.Subject)
		if err != nil {
			if errors.Is(err, store.ErrNotFound) {
				// 用户被移出组织后，他手上的 token 立刻失效 —— 这正是每次回查的意义
				apierr.Write(w, r, apierr.Unauthorized("membership_revoked", "账号已不在该组织内"))
				return
			}
			apierr.Write(w, r, err)
			return
		}
		if !user.Active() {
			apierr.Write(w, r, apierr.Forbidden("account_disabled", "账号已停用"))
			return
		}

		actor := &identity.Actor{
			Kind:           identity.KindUser,
			ID:             user.ID,
			UserID:         user.ID,
			OrganizationID: user.OrganizationID,
			Role:           user.Role,
			RequestID:      r.Header.Get(identity.HeaderRequestID),
		}
		s.store.TouchLastSeen(r.Context(), user.ID)
		next.ServeHTTP(w, r.WithContext(identity.With(r.Context(), actor)))
	})
}

// apiKeyStore 是 requireAPIKey 用到的那一小块 store 能力。
//
// **抽这个接口只有一个理由：让入口中间件可测。** 它是全站唯一同时做
// key 校验、撤销/过期、作用域、限速、配额的地方 —— 也就是说
// 越权、超额、免费用都在这一处拦住。合仓时它从 Python 搬到 Go，
// 旧系统那 13 条用例一条都没跟过来，而它偏偏是最不能漏的一段。
// 接口只列真正用到的四个方法，不要长成整个 Store 的镜像。
type apiKeyStore interface {
	AuthenticateAPIKey(ctx context.Context, plain string) (*store.APIKey, rbac.Role, error)
	ReserveQuota(ctx context.Context, orgID string, pages int) error
	TouchAPIKey(ctx context.Context, keyID string)
	Audit(ctx context.Context, orgID, actorID, actorKind, action, target,
		requestID string, detail map[string]any)
}

// requireAPIKey 校验对外 key，并在同一处做作用域、配额与限速。
//
// 四件事放在一起是刻意的：它们共同构成"这次调用能不能进来"，
// 拆开会让某条路径漏掉其中一项 —— 而漏掉限速的表现是账单，
// 漏掉作用域的表现是越权。
func (s *Server) requireAPIKey(scope rbac.Scope, next http.Handler) http.Handler {
	return apiKeyGate(s.store, s.limiter, scope, next)
}

func apiKeyGate(keys apiKeyStore, limiter ratelimit.Limiter, scope rbac.Scope,
	next http.Handler) http.Handler {

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearer(r)
		if token == "" {
			apierr.Write(w, r, apierr.Unauthorized("no_api_key", "缺少 API key"))
			return
		}
		if !auth.LooksLikeAPIKey(token) {
			apierr.Write(w, r, apierr.Unauthorized("not_an_api_key",
				"/v1/* 与 /mcp 需要 sk- 开头的 API key，浏览器会话请调 /api/*"))
			return
		}
		key, role, err := keys.AuthenticateAPIKey(r.Context(), token)
		if err != nil {
			if errors.Is(err, store.ErrNotFound) {
				apierr.Write(w, r, apierr.Unauthorized("invalid_api_key", "API key 无效"))
				return
			}
			apierr.Write(w, r, err)
			return
		}
		if live, reason := key.Live(time.Now()); !live {
			// 分开报原因：撤销 / 过期 / 无作用域 对调用方是三件不同的事
			apierr.Write(w, r, apierr.Unauthorized("api_key_"+reason, "API key 不可用："+reason))
			return
		}

		actor := &identity.Actor{
			Kind:            identity.KindAPIKey,
			ID:              key.ID,
			UserID:          key.UserID,
			OrganizationID:  key.OrganizationID,
			Role:            role,
			APIKeyID:        key.ID,
			Scopes:          key.Scopes,
			RateLimitPerMin: key.RateLimitPerMin,
			RequestID:       r.Header.Get(identity.HeaderRequestID),
		}

		// 作用域：路由声明它需要哪个平面
		if !actor.HasScope(scope) {
			keys.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
				"apikey.scope_denied", string(scope), actor.RequestID,
				map[string]any{"path": r.URL.Path})
			apierr.Write(w, r, apierr.Forbidden("scope_denied",
				"这把 key 没有 "+string(scope)+" 作用域"))
			return
		}

		// 限速：按 key 计，跨副本共享计数
		allowed, remaining, err := limiter.Allow(r.Context(),
			"key:"+key.ID, key.RateLimitPerMin, time.Minute)
		if err != nil {
			// 限速器坏了**放行**并记日志：把它做成硬失败等于让 Redis 抖动
			// 变成全站不可用。这是一个明确的取舍，写在这里以免以后被当成 bug 改掉
			slogWarn("rate limiter unavailable, failing open", err)
		} else {
			w.Header().Set("X-RateLimit-Limit", itoa(key.RateLimitPerMin))
			w.Header().Set("X-RateLimit-Remaining", itoa(remaining))
			if !allowed {
				apierr.Write(w, r, apierr.TooMany("rate_limited", "请求过于频繁"))
				return
			}
		}

		// 配额：只在会消耗页数的平面上查（解析/抽取）。
		// 查询类不查是因为它们不按页计费，多一次查询只是纯开销
		if scope == rbac.ScopeParse || scope == rbac.ScopeExtract {
			if err := keys.ReserveQuota(r.Context(), actor.OrganizationID, 1); err != nil {
				if errors.Is(err, store.ErrQuotaExceeded) {
					apierr.Write(w, r, apierr.PaymentRequired("quota_exceeded",
						"组织配额已用尽"))
					return
				}
				apierr.Write(w, r, err)
				return
			}
		}

		keys.TouchAPIKey(r.Context(), key.ID)
		next.ServeHTTP(w, r.WithContext(identity.With(r.Context(), actor)))
	})
}

// requireRole 是能力检查的入口。**问能力，不要问角色名** ——
// handler 里写 `if actor.Role == "admin"` 会在加了新角色之后静默失效。
func requireRole(a *identity.Actor, ok func(rbac.Role) bool, what string) error {
	if !ok(a.Role) {
		return apierr.Forbidden("insufficient_role", "当前角色（"+string(a.Role)+"）不能"+what)
	}
	return nil
}

// domainThrottle 是**按操作代价**的限速，与按 key 的通用限速是两回事。
//
// 问答、图谱生成、批量抽取都会打模型，一次调用的成本远高于一次列表查询 ——
// 按同一个 per-minute 数字限它们，要么把便宜的操作卡死，要么给贵的操作
// 开了绿灯。合仓前这三道闸散在语料侧的三个 router 里，各自读一个配置项；
// 现在统一在入口做：**限速只有一处，"到底是谁把我限了"才答得上来**。
//
// 匹配按前缀 + 方法。加一条贵操作就往 domainLimits 里加一行。
type domainLimit struct {
	method string
	prefix string
	suffix string // 非空时还要求路径以它结尾（区分 /ask 这种子资源）
	bucket string
	limit  int
}

func (s *Server) domainLimits() []domainLimit {
	return []domainLimit{
		{http.MethodPost, "/api/conversations/", "/ask", "qa", s.cfg.QARatePerMin},
		{http.MethodPost, "/api/knowledge/build", "", "knowledge", s.cfg.KnowledgeRatePerMin},
		{http.MethodPost, "/api/extractions/runs", "", "extract", s.cfg.ExtractRatePerMin},
	}
}

func (s *Server) domainThrottle(next http.Handler) http.Handler {
	limits := s.domainLimits()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		actor := identity.From(r.Context())
		if actor == nil {
			apierr.Write(w, r, apierr.Internal("限速中间件挂在了鉴权之前"))
			return
		}
		for _, l := range limits {
			if r.Method != l.method || !strings.HasPrefix(r.URL.Path, l.prefix) {
				continue
			}
			if l.suffix != "" && !strings.HasSuffix(r.URL.Path, l.suffix) {
				continue
			}
			allowed, remaining, err := s.limiter.Allow(r.Context(),
				l.bucket+":"+actor.ID, l.limit, time.Minute)
			if err != nil {
				// 与按 key 的限速同一个取舍：限速器坏了放行并记日志，
				// 而不是让 Redis 抖动变成全站不可用
				slogWarn("domain rate limiter unavailable, failing open", err)
				break
			}
			w.Header().Set("X-RateLimit-Limit", itoa(l.limit))
			w.Header().Set("X-RateLimit-Remaining", itoa(remaining))
			if !allowed {
				apierr.Write(w, r, apierr.TooMany("rate_limited",
					"这类操作很贵，请求过于频繁（"+l.bucket+"）"))
				return
			}
			break
		}
		next.ServeHTTP(w, r)
	})
}
