package api

import (
	"errors"
	"net/http"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

type tokenResponse struct {
	AccessToken string      `json:"access_token"`
	TokenType   string      `json:"token_type"`
	ExpiresIn   int         `json:"expires_in"`
	User        *store.User `json:"user"`
}

func (s *Server) issue(u *store.User) (*tokenResponse, error) {
	token, ttl, err := s.sessions.Issue(u.ID, u.OrganizationID, string(u.Role))
	if err != nil {
		return nil, err
	}
	return &tokenResponse{
		AccessToken: token,
		TokenType:   "bearer",
		ExpiresIn:   int(ttl.Seconds()),
		User:        u,
	}, nil
}

func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) error {
	if s.cfg.RegistrationMode != "open" {
		return apierr.Forbidden("registration_closed",
			"该部署未开放自助注册（REGISTRATION_MODE="+s.cfg.RegistrationMode+"）")
	}
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
		Email    string `json:"email"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if len(body.Username) < 3 || len(body.Username) > 64 {
		return apierr.BadRequest("bad_username", "用户名长度必须在 3–64 之间")
	}
	// 注册也限速：不限的话它就是一个免费的账号生成接口
	if err := s.limitByIP(r, "register"); err != nil {
		return err
	}

	hash, err := auth.HashPassword(body.Password, s.cfg.BcryptCost)
	if err != nil {
		return apierr.BadRequest("weak_password", err.Error())
	}
	role, err := rbac.Parse(s.cfg.DefaultRole)
	if err != nil {
		return apierr.Internal("DEFAULT_MEMBER_ROLE 配错了").WithCause(err)
	}
	user, err := s.store.CreateUser(r.Context(), s.defaultOrg, body.Username, body.Email, hash, role)
	if err != nil {
		if errors.Is(err, store.ErrUsernameTaken) {
			return apierr.Conflict("username_taken", "用户名已被占用")
		}
		return err
	}
	s.store.Audit(r.Context(), s.defaultOrg, user.ID, "user", "user.register", user.ID,
		r.Header.Get(identity.HeaderRequestID), map[string]any{"role": string(user.Role)})

	resp, err := s.issue(user)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusCreated, resp)
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if err := s.limitByIP(r, "login"); err != nil {
		return err
	}

	user, err := s.store.UserByUsername(r.Context(), s.defaultOrg, body.Username)
	if err != nil && !errors.Is(err, store.ErrNotFound) {
		return err
	}

	// **两条路径必须做同样多的工作。** 用户不存在时直接 return 会让
	// "这个用户名存在吗"变成一个可以用响应时间测出来的问题 ——
	// 那就是一个用户名枚举接口。VerifyPassword 在 hash 为空时会走一遍假比对。
	hash := ""
	if user != nil {
		hash = user.PasswordHash()
	}
	if !auth.VerifyPassword(hash, body.Password) || user == nil || !user.Active() {
		s.store.Audit(r.Context(), s.defaultOrg, "", "user", "user.login_failed",
			body.Username, r.Header.Get(identity.HeaderRequestID), nil)
		return apierr.Unauthorized("invalid_credentials", "用户名或密码错误")
	}

	s.store.Audit(r.Context(), s.defaultOrg, user.ID, "user", "user.login", user.ID,
		r.Header.Get(identity.HeaderRequestID), nil)
	resp, err := s.issue(user)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, resp)
}

func (s *Server) handleMe(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	user, err := s.store.UserByID(r.Context(), actor.OrganizationID, actor.UserID)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, user)
}

// limitByIP 给未鉴权的端点限速。
//
// 按 IP 是一个已知不完美的维度（NAT 后面的一群人共用一个），
// 但在没有身份的路径上没有更好的键。**这里的目的不是精确公平，
// 是让暴力破解从"几分钟"变成"几个月"**。
func (s *Server) limitByIP(r *http.Request, bucket string) error {
	ip := clientIP(r)
	allowed, _, err := s.limiter.Allow(r.Context(),
		bucket+":"+ip, s.cfg.LoginRatePerMin, time.Minute)
	if err != nil {
		slogWarn("rate limiter unavailable on unauthenticated route", err)
		return nil
	}
	if !allowed {
		return apierr.TooMany("rate_limited", "尝试过于频繁，请稍后再试")
	}
	return nil
}
