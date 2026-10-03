package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// RunBackground starts independent loops that stop with ctx. Durable database
// claims arbitrate concurrent replicas; descriptor renewal never changes approval.
func (s *Server) RunBackground(ctx context.Context) {
	go s.deliverOutbox(ctx)
	go s.verifyUploads(ctx)
	go s.housekeeping(ctx)
	go s.renewDiscoveryLeases(ctx)
}

// deliverOutbox 把 control 侧的事件投给 corpus-api。
//
// **至少一次**语义：消费端按 event_id 幂等。投递失败会指数退避并把原因
// 持久化 —— 只写日志的话，运维看到的是"文档没进来"而不是"事件投了 7 次都是 502"。
func (s *Server) deliverOutbox(ctx context.Context) {
	ticker := time.NewTicker(s.cfg.OutboxInterval)
	defer ticker.Stop()

	client := &http.Client{Timeout: 30 * time.Second}
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		events, err := s.store.ClaimOutbox(ctx, 32)
		if err != nil {
			slog.Error("outbox 领取失败", "err", err)
			continue
		}
		for _, e := range events {
			s.deliverEvent(ctx, client, e)
		}

		if count, oldest, err := s.store.OutboxBacklog(ctx); err == nil {
			obs.OutboxState(count, oldest)
		}
	}
}

// deliverEvent 投递一条已领取的事件并把结果落回同一行：ACK、终态拒绝或退避重试。
func (s *Server) deliverEvent(ctx context.Context, client *http.Client, e store.OutboxEvent) {
	body, _ := json.Marshal(map[string]any{
		"event_id":        e.ID,
		"type":            e.Type,
		"organization_id": e.OrganizationID,
		"payload":         e.Payload,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		s.cfg.CorpusURL+"/internal/events", bytes.NewReader(body))
	if err != nil {
		_ = s.store.MarkOutboxFailed(ctx, e.ID, e.Attempts, err.Error())
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
	// **一整套 actor 上下文，不能只发两个头。** corpus 侧的
	// `current_actor` 缺任何一个就 401，而且它是故意这样设计的
	// （缺头给默认值的话，"入口挂错中间件"会表现为"这个人突然只读了"）。
	// 之前这里只发了 Organization + ActorKind，于是**每一条事件
	// 永远投不出去**：outbox 忠实地重试、如实记下 401、
	// `/readyz` 也如实报 stale —— 一切都"正确地"坏着，
	// 而产品的主链路（上传完 -> 文档入库）一次都没通过。
	// 单测碰不到它：那边直接调消费函数，不经过 HTTP 头这一层。
	(&identity.Actor{
		Kind:           identity.KindService,
		ID:             "control-api",
		OrganizationID: e.OrganizationID,
		// 服务身份用最高角色：corpus 侧只校验 kind，
		// 但 role 必须是契约里的合法值，否则 403 unknown_role
		Role: rbac.Admin,
	}).Apply(req, "control-api")
	// 幂等键就是事件 ID —— 消费端据此去重
	req.Header.Set(identity.HeaderIdempotency, e.ID)

	resp, err := client.Do(req)
	if err != nil {
		_ = s.store.MarkOutboxFailed(ctx, e.ID, e.Attempts, err.Error())
		return
	}
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	resp.Body.Close()
	switch outcome, code, diagnostic := classifyDelivery(e.Type, resp.StatusCode, raw); outcome {
	case deliveryAcked:
		_ = s.store.MarkOutboxDelivered(ctx, e.ID)
	case deliveryRejected:
		_ = s.store.MarkOutboxRejected(ctx, e.ID, code)
	default:
		_ = s.store.MarkOutboxFailed(ctx, e.ID, e.Attempts, diagnostic)
	}
}

type deliveryOutcome int

const (
	deliveryRetry deliveryOutcome = iota
	deliveryAcked
	deliveryRejected
)

// classifyDelivery 把 corpus 对一次投递的回应分成三类（契约 upload-control-format）：
//
//   - 2xx，或 409 + `duplicate_event`（已处理过的重投）：ACK；
//   - DocumentSubmitted 的 4xx + 契约枚举 ingest_rejection 里的码：确定性拒绝，终态；
//   - 其余一切 —— 5xx、401/403（配置错）、408/429、别的 409（如
//     `document_state_changed` 明说了 retry）、读不出错误码的回应（路由不存在、
//     中间代理）：暂时故障，按退避重投同一事件。
//
// 以前**所有 409 都算成功**：目标资源被删、同内容版本已存在这类确定性冲突
// 会被记成"已投递"，上传者看到"已登记"，语料库里却什么都没有。
// 反过来把可恢复的失败判成终态同样危险 —— 已校验的上传会永远进不了库，
// 所以终态只认白名单里的码，宁可多重试也不误判。
func classifyDelivery(eventType string, status int, body []byte) (deliveryOutcome, contracts.IngestRejection, string) {
	if status >= 200 && status < 300 {
		return deliveryAcked, "", ""
	}
	var envelope struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	_ = json.Unmarshal(body, &envelope)
	code := envelope.Error.Code
	diagnostic := fmt.Sprintf("corpus-api 返回 %d", status)
	if code != "" {
		diagnostic += " " + code
	}
	if status == http.StatusConflict && code == "duplicate_event" {
		return deliveryAcked, "", ""
	}
	transientStatus := status == http.StatusUnauthorized || status == http.StatusForbidden ||
		status == http.StatusRequestTimeout || status == http.StatusTooManyRequests
	if eventType == "DocumentSubmitted" && status >= 400 && status < 500 && !transientStatus {
		if rejection, ok := store.ClassifyIngestRejection(code); ok {
			return deliveryRejected, rejection, diagnostic
		}
	}
	return deliveryRetry, "", diagnostic
}

// verifyUploads 是 §9.1 的服务端摘要校验。
//
// **它是"不信客户端声明的哈希"这条要求的落点**：finalize 只核对大小
// （便宜、同步），真正的内容摘要在这里流式重算 —— 常数内存，不阻塞请求路径。
// 校验完成前文档状态是 verifying，不能进入解析。
func (s *Server) verifyUploads(ctx context.Context) {
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}

		sessions, err := s.store.PendingVerification(ctx, 4)
		if err != nil {
			slog.Error("待校验上传列表读取失败", "err", err)
			continue
		}
		for _, sess := range sessions {
			digest, size, err := s.objects.Digest(ctx, sess.ObjectKey)
			if err != nil {
				slog.Error("摘要校验失败", "upload_id", sess.ID, "err", err)
				obs.UploadFailed("digest_error")
				// A transient read failure is unknown, not proof of corrupt content.
				// Keep verifying so restart/reconnect retries the full-object digest.
				continue
			}
			if sess.DeclaredSize > 0 && size != sess.DeclaredSize {
				obs.UploadFailed("size_mismatch_async")
				_ = s.store.MarkUploadFailed(ctx, sess.OrganizationID, sess.ID,
					fmt.Sprintf("对象大小 %d 与记录的 %d 不符", size, sess.DeclaredSize))
				continue
			}
			// 客户端报过哈希就比对。**不一致直接作废整个会话** ——
			// 那意味着传上去的内容与它声称的不是同一个东西
			if sess.DeclaredSHA256 != nil && *sess.DeclaredSHA256 != "" &&
				*sess.DeclaredSHA256 != digest {
				obs.UploadFailed("digest_mismatch")
				s.store.Audit(ctx, sess.OrganizationID, "", string(identity.KindService),
					"upload.digest_mismatch", sess.ID, "", nil)
				_ = s.store.MarkUploadFailed(ctx, sess.OrganizationID, sess.ID,
					"内容摘要与客户端声明不符")
				continue
			}
			if err := s.store.MarkUploadVerified(ctx, sess.OrganizationID, sess.ID, digest); err != nil {
				slog.Error("上传标记 ready 失败", "upload_id", sess.ID, "err", err)
			}
		}
	}
}

// housekeeping expires unfinished uploads and claims terminal original cleanup.
// Corpus owns the final reference check and irreversible object deletion.
func (s *Server) housekeeping(ctx context.Context) {
	ticker := time.NewTicker(5 * time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		if n, err := s.store.ExpireStaleUploads(ctx); err != nil {
			slog.Error("过期上传清理失败", "err", err)
		} else if n > 0 {
			slog.Info("已把过期上传标为 expired", "count", n)
		}
		if n, err := s.reclaimUploads(ctx); err != nil {
			slog.Error("terminal upload reclamation failed", "err", err)
		} else if n > 0 {
			slog.Info("terminal upload originals reclaimed", "count", n)
		}
	}
}

func (s *Server) reclaimUploads(ctx context.Context) (int, error) {
	client := &http.Client{Timeout: 30 * time.Second}
	return s.store.ReclaimTerminalUploads(ctx, time.Hour, 20, func(u store.UploadReclamation) (bool, error) {
		body, err := json.Marshal(map[string]any{"object_key": u.ObjectKey, "eligible_at": u.EligibleAt})
		if err != nil {
			return false, err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.cfg.CorpusURL+"/internal/upload-reclamation", bytes.NewReader(body))
		if err != nil {
			return false, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
		(&identity.Actor{Kind: identity.KindService, ID: "control-api", OrganizationID: u.OrganizationID, Role: rbac.Admin}).Apply(req, "control-api")
		resp, err := client.Do(req)
		if err != nil {
			return false, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return false, fmt.Errorf("corpus reclamation status %d", resp.StatusCode)
		}
		var result struct {
			Reclaimed bool `json:"reclaimed"`
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&result); err != nil {
			return false, err
		}
		if !result.Reclaimed {
			return false, nil
		}
		ids, err := s.objects.FindMultipart(ctx, u.ObjectKey)
		if err != nil {
			return false, err
		}
		for _, id := range ids {
			if err := s.objects.AbortMultipart(ctx, u.ObjectKey, id); err != nil {
				return false, err
			}
		}
		return true, nil
	})
}
