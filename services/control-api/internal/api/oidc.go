package api

import (
	"context"
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

// OIDC 是企业登录。**管理员强制 MFA 由 IdP 策略承担** ——
// 本服务不实现第二因素：再实现一套只会多一处可绕过的地方。
type OIDC struct {
	provider *oidc.Provider
	verifier *oidc.IDTokenVerifier
	oauth    oauth2.Config
}

// NewOIDC 在未配置 issuer 时返回 (nil, nil) —— 未配置不是错误，
// 单组织部署可以只用本地账号。
func NewOIDC(ctx context.Context, c *config.Config) (*OIDC, error) {
	if c.OIDCIssuer == "" {
		return nil, nil
	}
	provider, err := oidc.NewProvider(ctx, c.OIDCIssuer)
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
	http.SetCookie(w, &http.Cookie{
		Name:     "ddp_oidc_state",
		Value:    state,
		Path:     "/api/auth/oidc",
		HttpOnly: true,
		Secure:   r.TLS != nil,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   600,
	})
	if next := r.URL.Query().Get("redirect_uri"); next != "" {
		http.SetCookie(w, &http.Cookie{
			Name: "ddp_oidc_next", Value: next, Path: "/api/auth/oidc",
			HttpOnly: true, Secure: r.TLS != nil, SameSite: http.SameSiteLaxMode, MaxAge: 600,
		})
	}
	http.Redirect(w, r, s.oidc.oauth.AuthCodeURL(state), http.StatusFound)
	return nil
}

func (s *Server) handleOIDCCallback(w http.ResponseWriter, r *http.Request) error {
	if s.oidc == nil {
		return apierr.New(http.StatusNotImplemented, apierr.TypeInvalidRequest,
			"oidc_not_configured", "该部署未配置 OIDC")
	}
	cookie, err := r.Cookie("ddp_oidc_state")
	if err != nil || cookie.Value == "" || cookie.Value != r.URL.Query().Get("state") {
		return apierr.BadRequest("bad_state", "state 校验失败")
	}
	ctx, cancel := context.WithTimeout(r.Context(), 15*time.Second)
	defer cancel()

	token, err := s.oidc.oauth.Exchange(ctx, r.URL.Query().Get("code"))
	if err != nil {
		return apierr.BadRequest("exchange_failed", "授权码换 token 失败").WithCause(err)
	}
	rawID, ok := token.Extra("id_token").(string)
	if !ok {
		return apierr.BadRequest("no_id_token", "IdP 没有返回 id_token")
	}
	idToken, err := s.oidc.verifier.Verify(ctx, rawID)
	if err != nil {
		return apierr.Unauthorized("bad_id_token", "id_token 校验失败").WithCause(err)
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
	u, err := url.Parse(next)
	if err != nil || u.IsAbs() || u.Host != "" || !hasPrefix(next, "/") {
		return "/"
	}
	return next
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
