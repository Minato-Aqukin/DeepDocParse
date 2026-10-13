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
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// 游客缺省的身份。ID 固定填 "guest"：guest 没有 user id，也没有 key id，
// 给下游一个稳定的占位 —— corpus 侧看 kind 生效，绝不能用它关联任何行。
const guestActorID = "guest"

// guestPrefixes 是游客白名单：按**路径段**前缀匹配（契约 control-v1.yaml
// 「游客模式」一节，代码与契约是同一份清单的两处写法，改一边必须改另一边）。
//
// 只列段前缀，不在尾部加 `/`：`/api/documents` 既要放行它自己也要放行
// `/api/documents/…`，而 `/api/documentsX` 一个字符都不能多 ——
// strings.HasPrefix 会把它也放进来，所以匹配函数按段边界比。
var guestPrefixes = []string{
	"/api/resources",
	"/api/v1/resources",
	"/api/v1/collections",
	"/api/documents",
	"/api/search",
	"/api/evidence",
	"/api/wikis",
}

// guestPathOK 报告 path 是否落在游客白名单里。
//
// **为什么不用 strings.HasPrefix**：前缀匹配不认段边界，
// `/api/wikis` 的 HasPrefix 会把 `/api/wikis-evil` 也放进来。
// 这里要求要么正好相等，要么下一个字符是 `/`。
func guestPathOK(path string) bool {
	for _, prefix := range guestPrefixes {
		if path == prefix {
			return true
		}
		if strings.HasPrefix(path, prefix+"/") {
			return true
		}
	}
	return false
}

// sessionOrGuest 是语料转发与下载 URL 的门：有凭据走会话语义，
// 无凭据且开着游客模式时按白名单放行只读游客。
//
// 顺序是**凭据优先**：带了凭据的请求永远走 requireSession 那条路 ——
// 凭据无效是 401，绝不静默降级成游客。否则一个过期会话的用户会
// 在毫无提示的情况下看到公开视图，还以为自己的东西丢了。
func (s *Server) sessionOrGuest(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearer(r)
		if token == "" {
			// Cookie 兜底：与 requireSession 认同一组凭据，见它的注释
			if c, err := r.Cookie("ddp_session"); err == nil {
				token = c.Value
			}
		}
		if token != "" {
			// 有凭据：一字不改地走会话语义（含"API key 走错了门"那句错）。
			// 复用 authenticateSession 的共享实现，不复制它的 if 链。
			actor, err := s.authenticateSession(r.Context(), token)
			if err != nil {
				apierr.Write(w, r, err)
				return
			}
			actor.RequestID = r.Header.Get(identity.HeaderRequestID)
			s.store.TouchLastSeen(r.Context(), actor.UserID)
			next.ServeHTTP(w, r.WithContext(identity.With(r.Context(), actor)))
			return
		}
		if s.cfg.GuestMode != "read" {
			apierr.Write(w, r, apierr.Unauthorized("no_session", "缺少会话凭据"))
			return
		}
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			apierr.Write(w, r, apierr.Unauthorized("no_session", "缺少会话凭据"))
			return
		}
		if !guestPathOK(r.URL.Path) {
			apierr.Write(w, r, apierr.Unauthorized("no_session", "缺少会话凭据"))
			return
		}
		if err := s.limitGuest(r); err != nil {
			apierr.Write(w, r, err)
			return
		}
		actor := &identity.Actor{
			Kind:           identity.KindGuest,
			ID:             guestActorID,
			OrganizationID: s.defaultOrg,
			Role:           rbac.Viewer,
			RequestID:      r.Header.Get(identity.HeaderRequestID),
		}
		next.ServeHTTP(w, r.WithContext(identity.With(r.Context(), actor)))
	})
}

// limitGuest 按客户端 IP 给游客限速：通用桶 + 检索专用桶。
//
// 两只桶是**与**的关系：检索请求先过通用桶再过检索桶，
// 任何一只满了都是 429 `rate_limited`。限速器坏了与站内其它门
// 同一个取舍：放行并记日志，不让 Redis 抖动变成全站不可用。
func (s *Server) limitGuest(r *http.Request) error {
	ip := clientIPVia(r, s.cfg.TrustedProxies)
	allowed, _, err := s.limiter.Allow(r.Context(),
		"guest:"+ip, s.cfg.GuestRatePerMin, time.Minute)
	if err != nil {
		slogWarn("guest rate limiter unavailable, failing open", err)
		return nil
	}
	if !allowed {
		return apierr.TooMany("rate_limited", "请求过于频繁")
	}
	if r.URL.Path == "/api/search" {
		allowed, _, err := s.limiter.Allow(r.Context(),
			"guest-search:"+ip, s.cfg.GuestSearchRatePerMin, time.Minute)
		if err != nil {
			slogWarn("guest search rate limiter unavailable, failing open", err)
			return nil
		}
		if !allowed {
			return apierr.TooMany("rate_limited", "检索过于频繁")
		}
	}
	return nil
}

// authenticateSession 是 requireSession 校验凭据的那一半：
// API-key-走错门 → 会话验签 → membership 回查 → 账号停用。
// requireSession 与 sessionOrGuest 共用它 —— 会话语义只有一份实现，
// 改了一处的行为另一处自动跟上。
func (s *Server) authenticateSession(ctx context.Context, token string) (*identity.Actor, error) {
	if auth.LooksLikeAPIKey(token) {
		return nil, apierr.Unauthorized("api_key_on_session_route",
			"/api/* 用浏览器会话，API key 请调 /v1/*")
	}
	claims, err := s.sessions.Verify(token)
	if err != nil {
		return nil, apierr.Unauthorized("invalid_session", "会话无效或已过期").WithCause(err)
	}
	user, err := s.store.UserByID(ctx, claims.OrganizationID, claims.Subject)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			// 用户被移出组织后，他手上的 token 立刻失效 —— 这正是每次回查的意义
			return nil, apierr.Unauthorized("membership_revoked", "账号已不在该组织内")
		}
		return nil, err
	}
	if !user.Active() {
		return nil, apierr.Forbidden("account_disabled", "账号已停用")
	}
	return &identity.Actor{
		Kind:           identity.KindUser,
		ID:             user.ID,
		UserID:         user.ID,
		OrganizationID: user.OrganizationID,
		Role:           user.Role,
	}, nil
}
