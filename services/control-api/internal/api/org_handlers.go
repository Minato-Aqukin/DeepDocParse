package api

import (
	"errors"
	"net/http"
	"strconv"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func (s *Server) handleOrg(w http.ResponseWriter, r *http.Request) error {
	org, err := s.store.DefaultOrganization(r.Context())
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, org)
}

func (s *Server) handleMembers(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	members, err := s.store.Members(r.Context(), actor.OrganizationID)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, members)
}

func (s *Server) handleAddMember(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanManageOrg, "管理成员"); err != nil {
		return err
	}
	var body struct {
		Username string `json:"username"`
		Role     string `json:"role"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	role, err := rbac.Parse(body.Role)
	if err != nil {
		return apierr.BadRequest("bad_role", err.Error())
	}
	member, err := s.store.AddMember(r.Context(), actor.OrganizationID, body.Username, role)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return apierr.NotFound("no_such_user", "没有这个账号")
		}
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"org.member_added", member.UserID, actor.RequestID,
		map[string]any{"role": string(role)})
	return httpx.JSON(w, http.StatusCreated, member)
}

func (s *Server) handleSetMemberRole(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanManageOrg, "改成员角色"); err != nil {
		return err
	}
	var body struct {
		Role string `json:"role"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	role, err := rbac.Parse(body.Role)
	if err != nil {
		return apierr.BadRequest("bad_role", err.Error())
	}
	target := r.PathValue("user_id")
	if err := s.store.SetMemberRole(r.Context(), actor.OrganizationID, target, role); err != nil {
		switch {
		case errors.Is(err, store.ErrLastAdmin):
			return apierr.Conflict("last_admin",
				"不能把最后一个管理员降级 —— 那会让组织永久失去管理能力")
		case errors.Is(err, store.ErrNotFound):
			return apierr.NotFound("no_such_member", "该账号不是本组织成员")
		}
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"org.role_changed", target, actor.RequestID, map[string]any{"role": string(role)})

	members, err := s.store.Members(r.Context(), actor.OrganizationID)
	if err != nil {
		return err
	}
	for _, m := range members {
		if m.UserID == target {
			return httpx.JSON(w, http.StatusOK, m)
		}
	}
	return apierr.NotFound("no_such_member", "该账号不是本组织成员")
}

func (s *Server) handleRemoveMember(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanManageOrg, "移除成员"); err != nil {
		return err
	}
	target := r.PathValue("user_id")
	if err := s.store.RemoveMember(r.Context(), actor.OrganizationID, target); err != nil {
		switch {
		case errors.Is(err, store.ErrLastAdmin):
			return apierr.Conflict("last_admin", "不能移除最后一个管理员")
		case errors.Is(err, store.ErrNotFound):
			return apierr.NotFound("no_such_member", "该账号不是本组织成员")
		}
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"org.member_removed", target, actor.RequestID, nil)
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// ---------------------------------------------------------------- API key

func (s *Server) handleListKeys(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	keys, err := s.store.ListAPIKeys(r.Context(), actor.OrganizationID, actor.UserID)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, keys)
}

func (s *Server) handleCreateKey(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanIssueKeys, "签发 API key"); err != nil {
		return err
	}
	var body struct {
		Name            string   `json:"name"`
		Scopes          []string `json:"scopes"`
		ExpiresInDays   *int     `json:"expires_in_days"`
		QuotaPages      *int     `json:"quota_pages"`
		RateLimitPerMin *int     `json:"rate_limit_per_min"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if body.Name == "" {
		body.Name = "default"
	}

	scopes := actor.Role.DefaultScopes()
	if len(body.Scopes) > 0 {
		scopes = scopes[:0]
		for _, s := range body.Scopes {
			scopes = append(scopes, rbac.Scope(s))
		}
		// **不能签发比自己权限更大的 key**，否则 viewer 可以发一把
		// 能上传的 key 来绕过自己的角色
		if err := actor.Role.AllowedScopes(scopes); err != nil {
			return apierr.Forbidden("scope_escalation", err.Error())
		}
	}

	rate := s.cfg.DefaultRatePerMin
	if body.RateLimitPerMin != nil {
		rate = *body.RateLimitPerMin
	}
	var expires *time.Time
	if body.ExpiresInDays != nil {
		t := time.Now().AddDate(0, 0, *body.ExpiresInDays)
		expires = &t
	}

	key, plain, err := s.store.CreateAPIKey(r.Context(), actor.OrganizationID, actor.UserID,
		body.Name, scopes, body.QuotaPages, rate, expires)
	if err != nil {
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"apikey.created", key.ID, actor.RequestID,
		// **审计里不放明文 key**，只放它的 id 与作用域
		map[string]any{"scopes": body.Scopes, "name": key.Name})

	return httpx.JSON(w, http.StatusCreated, struct {
		*store.APIKey
		Key string `json:"key"`
	}{key, plain})
}

func (s *Server) handleRevokeKey(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	id := r.PathValue("key_id")
	if err := s.store.RevokeAPIKey(r.Context(), actor.OrganizationID, actor.UserID, id); err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return apierr.NotFound("no_such_key", "key 不存在或不属于你")
		}
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"apikey.revoked", id, actor.RequestID, nil)
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// ---------------------------------------------------------- 计量与审计

func (s *Server) handleUsage(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	days := intQuery(r, "days", 30, 1, 365)
	userFilter := actor.UserID
	if r.URL.Query().Get("scope") == "organization" {
		if err := requireRole(actor, rbac.Role.CanManageOrg, "查看全组织用量"); err != nil {
			return err
		}
		userFilter = ""
	}
	points, err := s.store.UsageSeries(r.Context(), actor.OrganizationID, userFilter, days)
	if err != nil {
		return err
	}
	quota, err := s.store.Quota(r.Context(), actor.OrganizationID)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, map[string]any{"points": points, "quota": quota})
}

func (s *Server) handleAudit(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanReadAudit, "查看审计日志"); err != nil {
		return err
	}
	limit := intQuery(r, "limit", 100, 1, 1000)
	var before *time.Time
	if raw := r.URL.Query().Get("before"); raw != "" {
		t, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			return apierr.BadRequest("bad_before", "before 必须是 RFC3339 时间")
		}
		before = &t
	}
	events, err := s.store.AuditEvents(r.Context(), actor.OrganizationID,
		r.URL.Query().Get("action"), before, limit)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, events)
}

func intQuery(r *http.Request, name string, def, lo, hi int) int {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return def
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		return def
	}
	// 夹在区间里而不是报错：limit=99999 的意图是"给我尽量多"，
	// 报 400 只会让调用方去试出上限。但**必须夹**，否则它是一个 DoS 入口
	return min(max(v, lo), hi)
}
