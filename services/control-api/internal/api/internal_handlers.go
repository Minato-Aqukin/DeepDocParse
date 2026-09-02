package api

import (
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// requireServiceCredentials 守 `/internal/*`：只认服务凭据，不认用户会话。
//
// **它不读 actor 上下文头**：那些头是本服务下发给上游的，
// 反过来接受它们等于把提权面又开回来。
func (s *Server) requireServiceCredentials(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !auth.ConstantTimeEqual(bearer(r), s.cfg.ServiceToken) {
			apierr.Write(w, r, apierr.Unauthorized("invalid_service_token",
				"/internal/* 需要服务凭据"))
			return
		}
		next.ServeHTTP(w, r)
	})
}

// handleInternalFileGrant 给语料侧签发（或复用）**稳定**文件凭证。
//
// 语料侧需要它是因为 `file_grants` 住在 control schema，而 corpus 对那个
// schema 没有任何权限（企业边界 5）。
//
// **必须幂等**：同一份文档反复调用要拿到同一个 URL，否则每次重解析都会
// 换一个 `doc_hash` —— 网关的幂等复用与向量索引分块键全部失效
// （ADR #11/#12，这个项目踩过两次）。
func (s *Server) handleInternalFileGrant(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		OrganizationID string `json:"organization_id"`
		DocumentID     string `json:"document_id"`
		ObjectKey      string `json:"object_key"`
		MIME           string `json:"mime"`
		// 原始文件名。语料侧知道它，control 侧不知道 —— 而签名 URL 的
		// Content-Disposition 只能由 control 侧签（跨源 <a download> 不算数）
		Filename string `json:"filename"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if body.DocumentID == "" || body.ObjectKey == "" {
		return apierr.BadRequest("bad_grant_request", "document_id 与 object_key 必填")
	}
	if body.OrganizationID == "" {
		body.OrganizationID = s.defaultOrg
	}
	grant, err := s.store.StableGrantFor(r.Context(), body.OrganizationID,
		body.DocumentID, body.ObjectKey, body.MIME, body.Filename)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, map[string]any{
		"token": grant.Token,
		// **内网地址**：这条 URL 的消费者是 model-gateway（容器里的进程），
		// 不是浏览器。用公网地址的话它下载不了原件，而表现是
		// "解析失败：连接不上"，看着像模型服务挂了
		"url": s.cfg.InternalBaseURL + "/files/" + grant.Token,
	})
}

// handleInternalActors 把 actor_id 批量渲染成显示名。
//
// 语料侧用它给"上传者"、"复核人"这些字段配上人能看懂的名字。
// **查不到的一律给占位名，不省略**：省略会让调用方分不清
// "这个人没有名字"与"这个 id 不存在"。
func (s *Server) handleInternalActors(w http.ResponseWriter, r *http.Request) error {
	raw := r.URL.Query().Get("ids")
	if raw == "" {
		return httpx.JSON(w, http.StatusOK, map[string]string{})
	}
	var ids []string
	for _, id := range strings.Split(raw, ",") {
		if id = strings.TrimSpace(id); id != "" {
			ids = append(ids, id)
		}
	}
	// 上限：这是内部端点，但一个超长的 ids 仍然是个便宜的 DoS
	if len(ids) > 500 {
		ids = ids[:500]
	}
	out, err := s.store.ActorNames(r.Context(), ids)
	if err != nil {
		return err
	}
	return httpx.JSON(w, http.StatusOK, out)
}

// handleInternalUsage 接收语料侧发来的用量事件。
//
// 计量的**真相**在 control（Go 扣配额、出账单），而"这次解析用了几页"
// 只有语料侧知道 —— 所以它把用量作为事件发过来，而不是自己写
// `usage_ledger`（那就是两个写入所有者，违反企业边界 5）。
//
// 幂等键是 `event_id`，`usage_ledger.event_id` 上有唯一约束。
func (s *Server) handleInternalUsage(w http.ResponseWriter, r *http.Request) error {
	var body struct {
		EventID        string `json:"event_id"`
		Type           string `json:"type"`
		OrganizationID string `json:"organization_id"`
		Payload        struct {
			ActorID    string `json:"actor_id"`
			APIKeyID   string `json:"api_key_id"`
			ParseJobID string `json:"parse_job_id"`
			Kind       string `json:"kind"`
			Pages      int    `json:"pages"`
			Requests   int    `json:"requests"`
		} `json:"payload"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if body.EventID == "" {
		return apierr.BadRequest("missing_event_id", "event_id 必填（它是幂等键）")
	}
	if body.Type != "UsageRecorded" {
		// 不认识的事件回 2xx 并留痕：投递器会一直重投 4xx/5xx，
		// 而"control 还没升级到认识这个事件"不是投递器能解决的问题
		return httpx.JSON(w, http.StatusOK, map[string]any{
			"ok": true, "ignored": body.Type,
		})
	}
	org := body.OrganizationID
	if org == "" {
		org = s.defaultOrg
	}
	// 计量种类必须在契约里。**不校验的后果是静默的**：
	// 一个拼错的 kind 会被 usage_ledger 原样收下（那一列没有 CHECK），
	// 于是它既不进任何按种类分组的报表，也不触发任何告警 ——
	// 用量凭空少了一块，而所有请求都是 200
	kind := body.Payload.Kind
	if !contracts.UsageKind(kind).Valid() {
		// **与上面"未知事件类型"同样处理：2xx + 留痕，不是 4xx。**
		// 理由一样 —— 投递器会一直重投 4xx/5xx，而"control 还没升级到
		// 认识这个 kind"不是投递器能解决的问题。语料侧的投递器只把
		// <300 与 409 当成功，且它没有死信队列，所以一个 400 会让那笔
		// 用量**永远重投、永远不进账**，而只有一行 warning。
		//
		// 记 ERROR 是因为这确实是个部署顺序问题，需要有人看见 ——
		// 但它不该表现为"这条事件卡死"
		slog.ErrorContext(r.Context(), "未知的计量种类，已忽略",
			"kind", kind, "event_id", body.EventID, "organization_id", org)
		return httpx.JSON(w, http.StatusOK, map[string]any{
			"ok": true, "ignored": "unknown_usage_kind:" + kind,
		})
	}
	actorKind := string(contracts.ActorKindUser)
	if body.Payload.APIKeyID != "" {
		actorKind = string(contracts.ActorKindApiKey)
	}
	if err := s.store.RecordUsage(r.Context(), org, body.Payload.ActorID, actorKind,
		body.Payload.APIKeyID, kind, body.Payload.Pages, max(body.Payload.Requests, 1),
		body.EventID); err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return apierr.NotFound("unknown_org", "未知组织")
		}
		return err
	}
	return httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
}
