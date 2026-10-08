package api

import (
	"context"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// oidcHTTPClient 给 OIDC 的 discovery / token 交换 / userinfo 用。
// **不读环境代理变量**（铁律 8，见 proxy.New / discovery.NewPeerDirectory）：
// 带代理的机器会把内网 IdP 调用也塞进代理，表现是卡住而不是报错 ——
// 而这条正是企业登录路径。超时分开：discovery 走调用方 ctx，
// token 交换在 callback 里另有 15s 上下文。
func oidcHTTPClient(timeout time.Duration) *http.Client {
	transport := &http.Transport{
		Proxy: nil,
		DialContext: (&net.Dialer{
			Timeout:   5 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:          32,
		MaxIdleConnsPerHost:   8,
		IdleConnTimeout:       60 * time.Second,
		ExpectContinueTimeout: time.Second,
		ForceAttemptHTTP2:     true,
	}
	return &http.Client{
		Transport: transport,
		Timeout:   timeout,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

// oidcCtx 把 proxy-free client 挂进 ctx：go-oidc 的 NewProvider 与
// oauth2 的 Exchange 都从 ctx 里取 client（oauth2.HTTPClient key）。
func oidcCtx(ctx context.Context, client *http.Client) context.Context {
	return oidc.ClientContext(ctx, client)
}

// OIDC 是企业登录。**管理员强制 MFA 由 IdP 策略承担** ——
// 本服务不实现第二因素：再实现一套只会多一处可绕过的地方。
type OIDC struct {
	provider *oidc.Provider
	verifier idTokenVerifier
	oauth    oauth2.Config
}

// idTokenVerifier 是 oidc.IDTokenVerifier 用到的那一个方法。
// 抽接口只为让 callback 的 nonce 检查可测：生产传真正的 verifier，
// 单测传 stub —— nonce 比对逻辑本身仍在 handleOIDCCallback 里，不绕过。
type idTokenVerifier interface {
	Verify(ctx context.Context, rawIDToken string) (*oidc.IDToken, error)
}

// NewOIDC 在未配置 issuer 时返回 (nil, nil) —— 未配置不是错误，
// 单组织部署可以只用本地账号。
func NewOIDC(ctx context.Context, c *config.Config) (*OIDC, error) {
	if c.OIDCIssuer == "" {
		return nil, nil
	}
	discCtx, discCancel := context.WithTimeout(ctx, 15*time.Second)
	defer discCancel()
	provider, err := oidc.NewProvider(oidcCtx(discCtx, oidcHTTPClient(15*time.Second)), c.OIDCIssuer)
	if err != nil {
		return nil, err
	}
	return &OIDC{
		provider: provider,
		verifier: provider.Verifier(&oidc.Config{ClientID: c.OIDCClientID}),
		oauth: oauth2.Config{
			ClientID:     c.OIDCClientID,
			ClientSecret: c.OIDCClientSecret,
			Endpoint:     provider.Endpoint(),
			RedirectURL:  c.OIDCRedirectURL,
			Scopes:       []string{oidc.ScopeOpenID, "profile", "email"},
		},
	}, nil
}

func (s *Server) handleOIDCLogin(w http.ResponseWriter, r *http.Request) error {
	if s.oidc == nil {
		return apierr.New(http.StatusNotImplemented, apierr.TypeInvalidRequest,
			"oidc_not_configured", "该部署未配置 OIDC")
	}
	// state 防 CSRF。**必须是随机的且与这次浏览器会话绑定** ——
	// 固定 state 等于没有 state
	state := auth.NewToken()
	// PKCE（S256）：授权码被截获也换不成 token —— 换 token 要出示
	// 只有这次浏览器会话才知道的 verifier。
	verifier := oauth2.GenerateVerifier()
	// nonce 把 id_token 绑到这次浏览器会话：回来的 token 里 nonce
	// 必须与这里存的一致，否则是重放别的会话的 token。
	nonce := auth.NewToken()
	oidcCookies(w, r, map[string]string{
		"ddp_oidc_state":    state,
		"ddp_oidc_verifier": verifier,
		"ddp_oidc_nonce":    nonce,
	})
	if next := r.URL.Query().Get("redirect_uri"); next != "" {
		if !validNext(next) {
			return apierr.BadRequest("bad_redirect", "redirect_uri 必须是站内路径")
		}
		oidcCookies(w, r, map[string]string{"ddp_oidc_next": next})
	}
	http.Redirect(w, r, s.oidc.oauth.AuthCodeURL(state,
		oauth2.S256ChallengeOption(verifier),
		oauth2.SetAuthURLParam("nonce", nonce),
	), http.StatusFound)
	return nil
}

// oidcCookies 写 OIDC 往返 cookie：HttpOnly + SameSite=Lax + 短 TTL。
// Path 限定在 /api/auth/oidc，callback 验完即清 —— 它们是单次登录的
// 临时秘密，不是会话。
func oidcCookies(w http.ResponseWriter, r *http.Request, kv map[string]string) {
	for name, value := range kv {
		http.SetCookie(w, &http.Cookie{
			Name:     name,
			Value:    value,
			Path:     "/api/auth/oidc",
			HttpOnly: true,
			Secure:   r.TLS != nil,
			SameSite: http.SameSiteLaxMode,
			MaxAge:   600,
		})
	}
}

// clearOIDCCookies 清掉单次登录的临时 cookie：state / verifier / nonce / next。
// 登录完成（无论成功失败）后它们必须消失 —— 留着就是给重放留窗口。
func clearOIDCCookies(w http.ResponseWriter, r *http.Request) {
	for _, name := range []string{
		"ddp_oidc_state", "ddp_oidc_verifier", "ddp_oidc_nonce", "ddp_oidc_next",
	} {
		http.SetCookie(w, &http.Cookie{
			Name:     name,
			Value:    "",
			Path:     "/api/auth/oidc",
			HttpOnly: true,
			Secure:   r.TLS != nil,
			SameSite: http.SameSiteLaxMode,
			MaxAge:   -1,
		})
	}
}

// oidcCookie 取单次登录的临时 cookie，缺失/空一律报错。
func oidcCookie(r *http.Request, name string) (string, error) {
	c, err := r.Cookie(name)
	if err != nil || c.Value == "" {
		return "", apierr.BadRequest("bad_oidc_cookie", "OIDC 登录会话已过期，请重新登录")
	}
	return c.Value, nil
}

func (s *Server) handleOIDCCallback(w http.ResponseWriter, r *http.Request) error {
	if s.oidc == nil {
		return apierr.New(http.StatusNotImplemented, apierr.TypeInvalidRequest,
			"oidc_not_configured", "该部署未配置 OIDC")
	}
	state, err := oidcCookie(r, "ddp_oidc_state")
	if err != nil || state != r.URL.Query().Get("state") {
		clearOIDCCookies(w, r)
		return apierr.BadRequest("bad_state", "state 校验失败")
	}
	verifier, err := oidcCookie(r, "ddp_oidc_verifier")
	if err != nil {
		clearOIDCCookies(w, r)
		return err
	}
	wantNonce, err := oidcCookie(r, "ddp_oidc_nonce")
	if err != nil {
		clearOIDCCookies(w, r)
		return err
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()
	ctx = oidcCtx(ctx, oidcHTTPClient(15*time.Second))

	token, err := s.oidc.oauth.Exchange(ctx, r.URL.Query().Get("code"), oauth2.VerifierOption(verifier))
	if err != nil {
		clearOIDCCookies(w, r)
		return apierr.BadRequest("exchange_failed", "授权码换 token 失败").WithCause(err)
	}
	rawID, ok := token.Extra("id_token").(string)
	if !ok {
		clearOIDCCookies(w, r)
		return apierr.BadRequest("no_id_token", "IdP 没有返回 id_token")
	}
	idToken, err := s.oidc.verifier.Verify(ctx, rawID)
	if err != nil {
		clearOIDCCookies(w, r)
		return apierr.Unauthorized("bad_id_token", "id_token 校验失败").WithCause(err)
	}
	// nonce 必须与登录时存的一致：不一致说明这个 id_token 不是给这次
	// 浏览器会话签发的 —— 重放别的会话的 token 到这里就停。
	if idToken.Nonce == "" || !auth.ConstantTimeEqual(idToken.Nonce, wantNonce) {
		clearOIDCCookies(w, r)
		return apierr.Unauthorized("bad_nonce", "id_token nonce 校验失败")
	}
	var claims struct {
		Sub               string `json:"sub"`
		Email             string `json:"email"`
		PreferredUsername string `json:"preferred_username"`
		Name              string `json:"name"`
	}
	if err := idToken.Claims(&claims); err != nil {
		return apierr.Internal("解析 id_token claims 失败").WithCause(err)
	}
	username := firstNonEmpty(claims.PreferredUsername, claims.Email, claims.Sub)

	role, err := rbac.Parse(s.cfg.DefaultRole)
	if err != nil {
		return apierr.Internal("DEFAULT_MEMBER_ROLE 配错了").WithCause(err)
	}
	// **按 (issuer, subject) 认人**：email 会变、username 会重名，
	// 只有 subject 是稳定的
	user, err := s.store.UpsertOIDCUser(ctx, s.defaultOrg, idToken.Issuer, claims.Sub,
		username, claims.Email, role)
	if err != nil {
		return err
	}
	s.store.Audit(ctx, s.defaultOrg, user.ID, "user", "user.login_oidc", user.ID,
		"", map[string]any{"issuer": idToken.Issuer})

	session, ttl, err := s.sessions.Issue(user.ID, user.OrganizationID, string(user.Role))
	if err != nil {
		return err
	}
	next := "/"
	if c, err := r.Cookie("ddp_oidc_next"); err == nil && c.Value != "" {
		next = c.Value
	}
	clearOIDCCookies(w, r)
	// 会话放 HttpOnly cookie 而不是 URL 片段：
	// 放 URL 里会进浏览器历史、进 Referer、进日志
	http.SetCookie(w, &http.Cookie{
		Name: "ddp_session", Value: session, Path: "/",
		HttpOnly: true, Secure: r.TLS != nil, SameSite: http.SameSiteLaxMode,
		MaxAge: int(ttl.Seconds()),
	})
	http.Redirect(w, r, safeRedirect(next), http.StatusFound)
	return nil
}

// safeRedirect 只允许站内跳转。
// **开放重定向是钓鱼的标准入口** —— 带着有效会话跳到攻击者的域名。
func safeRedirect(next string) string {
	if !validNext(next) {
		return "/"
	}
	return next
}

// validNext 只放行单前导 `/` 的站内路径。挡掉的三类：
//   - 含 `\`：浏览器会把它当 `/`，`/\evil.example` 就是 `//evil.example`；
//   - `//` 开头：protocol-relative URL，直接跳到别的域名；
//   - 非 `/` 开头：绝对 URL（http/https/javascript:…）或相对路径。
//
// 登录入口在写 ddp_oidc_next cookie 之前先验，callback 只做兜底。
func validNext(next string) bool {
	if next == "" || !hasPrefix(next, "/") || hasPrefix(next, "//") {
		return false
	}
	for i := range len(next) {
		if next[i] == '\\' {
			return false
		}
	}
	u, err := url.Parse(next)
	if err != nil || u.IsAbs() || u.Host != "" {
		return false
	}
	return true
}

func hasPrefix(s, p string) bool { return len(s) >= len(p) && s[:len(p)] == p }

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}
