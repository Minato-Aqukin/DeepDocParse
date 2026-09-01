package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
)

// RunBackground 起三个后台循环，随 ctx 一起结束。
//
// 它们都做成**可重入、可被任意副本执行**的：不选主、不假设单实例。
// 领取用 `FOR UPDATE SKIP LOCKED`，所以多副本并行只会更快，不会重复。
func (s *Server) RunBackground(ctx context.Context) {
	go s.deliverOutbox(ctx)
	go s.verifyUploads(ctx)
	go s.housekeeping(ctx)
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
				continue
			}
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Authorization", "Bearer "+s.cfg.ServiceToken)
			req.Header.Set(identity.HeaderService, "control-api")
			req.Header.Set(identity.HeaderOrganization, e.OrganizationID)
			req.Header.Set(identity.HeaderActorKind, string(identity.KindService))
			// 幂等键就是事件 ID —— 消费端据此去重
			req.Header.Set(identity.HeaderIdempotency, e.ID)

			resp, err := client.Do(req)
			if err != nil {
				_ = s.store.MarkOutboxFailed(ctx, e.ID, e.Attempts, err.Error())
				continue
			}
			// 2xx 与 409（已处理过）都算成功：409 正是幂等消费端对
			// 重投的正确回应，把它当失败会让事件永远重投
			ok := resp.StatusCode < 300 || resp.StatusCode == http.StatusConflict
			resp.Body.Close()
			if ok {
				_ = s.store.MarkOutboxDelivered(ctx, e.ID)
			} else {
				_ = s.store.MarkOutboxFailed(ctx, e.ID, e.Attempts,
					fmt.Sprintf("corpus-api 返回 %d", resp.StatusCode))
			}
		}

		if count, oldest, err := s.store.OutboxBacklog(ctx); err == nil {
			obs.OutboxState(count, oldest)
		}
	}
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
				_ = s.store.MarkUploadFailed(ctx, sess.OrganizationID, sess.ID, "摘要计算失败")
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

// housekeeping 只做一件不可逆性最低的事：把过期未完成的上传标成 expired。
//
// **不删对象**：删除是不可逆的，回收交给 corpus 侧带宽限期与 claim 的 GC
// （那是全项目唯一会不可逆毁数据的地方，两道防护缺一不可）。
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
	}
}
