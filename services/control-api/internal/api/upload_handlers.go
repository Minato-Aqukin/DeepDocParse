package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"path"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/obs"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// 估算页数用的经验值：按页平均 60KB 算上界，只用来**受理前占额度**。
// 真实页数由解析完成后的 UsageRecorded 事件修正。
//
// 为什么要估：等解析完再扣的话，一次批量上传可以把配额透支到任意程度。
// 为什么可以粗：它只决定"要不要拦"，账目以真实页数为准。
const bytesPerPageEstimate = 60 * 1024

func (s *Server) handleCreateUpload(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err := requireRole(actor, rbac.Role.CanUpload, "上传文档"); err != nil {
		return err
	}

	var body struct {
		Filename string  `json:"filename"`
		Size     int64   `json:"size"`
		MIME     string  `json:"mime"`
		SHA256   *string `json:"sha256"`
	}
	if err := httpx.DecodeJSON(r, &body); err != nil {
		return err
	}
	if body.Filename == "" || body.Size <= 0 {
		return apierr.BadRequest("bad_upload", "filename 与 size 必填")
	}
	if body.Size > s.cfg.MaxUploadBytes {
		return apierr.New(http.StatusRequestEntityTooLarge, apierr.TypeInvalidRequest,
			"too_large", fmt.Sprintf("单文件上限 %d 字节", s.cfg.MaxUploadBytes))
	}
	// **白名单而不是黑名单**：上传 text/html 并 inline 打开就是本站同源 XSS
	if !s.cfg.MIMEAllowed(body.MIME) {
		return apierr.New(http.StatusUnsupportedMediaType, apierr.TypeInvalidRequest,
			"mime_not_allowed", "不支持的文件类型："+body.MIME)
	}

	estimate := int(body.Size/bytesPerPageEstimate) + 1
	if err := s.store.ReserveQuota(r.Context(), actor.OrganizationID, estimate); err != nil {
		if errors.Is(err, store.ErrQuotaExceeded) {
			return apierr.PaymentRequired("quota_exceeded",
				fmt.Sprintf("组织配额不足（本次预估 %d 页）", estimate))
		}
		return err
	}

	// 对象键：组织 / 日期 / 随机。**不含用户可控的文件名** ——
	// 文件名进对象键会带来路径穿越与大小写冲突两类问题，
	// 而展示用的文件名存在库里就够了
	key := fmt.Sprintf("uploads/%s/%s/%s%s",
		actor.OrganizationID, time.Now().UTC().Format("2006/01/02"),
		auth.NewID(), strings.ToLower(path.Ext(body.Filename)))

	uploadID, parts, err := s.objects.CreateMultipart(r.Context(), key, body.MIME,
		body.Size, s.cfg.UploadPartSize)
	if err != nil {
		return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "objectstore_error",
			"对象存储不可用").WithCause(err)
	}
	obs.PresignedURL("upload")

	sess := &store.UploadSession{
		OrganizationID: actor.OrganizationID,
		ActorID:        actor.ID,
		ActorKind:      string(actor.Kind),
		ObjectKey:      key,
		MultipartID:    uploadID,
		Filename:       body.Filename,
		MIME:           body.MIME,
		DeclaredSize:   body.Size,
		DeclaredSHA256: body.SHA256,
		ExpiresAt:      time.Now().Add(s.cfg.UploadTTL),
	}
	if err := s.store.CreateUploadSession(r.Context(), sess); err != nil {
		// 记录建不起来就把 multipart 撤掉，别留下计费中的碎片
		_ = s.objects.AbortMultipart(r.Context(), key, uploadID)
		return err
	}
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"upload.created", sess.ID, actor.RequestID,
		map[string]any{"filename": body.Filename, "size": body.Size, "mime": body.MIME})

	return httpx.JSON(w, http.StatusCreated, uploadResponse(sess, parts, s.cfg.UploadPartSize))
}

func (s *Server) handleGetUpload(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	sess, err := s.store.UploadSession(r.Context(), actor.OrganizationID, r.PathValue("upload_id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return apierr.NotFound("no_such_upload", "上传会话不存在")
		}
		return err
	}
	return httpx.JSON(w, http.StatusOK, uploadResponse(sess, nil, s.cfg.UploadPartSize))
}

func (s *Server) handleFinalizeUpload(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	var body struct {
		Parts   []objectstore.CompletedPart `json:"parts"`
		Engine  string                      `json:"engine"`
		Options json.RawMessage             `json:"options"`
	}
	// 空 body 也允许：分片 ETag 可以从对象存储自己列
	if r.ContentLength > 0 {
		if err := httpx.DecodeJSON(r, &body); err != nil {
			return err
		}
	}

	id := r.PathValue("upload_id")
	sess, err := s.store.UploadSession(r.Context(), actor.OrganizationID, id)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return apierr.NotFound("no_such_upload", "上传会话不存在")
		}
		return err
	}
	// 幂等：已经 finalize 过的直接把当前状态返回去。
	// **重试拿到 202 是对的** —— 那正是幂等的表现，不是错误
	if contracts.UploadStatus(sess.Status) != contracts.UploadStatusCreated &&
		contracts.UploadStatus(sess.Status) != contracts.UploadStatusUploading {
		return httpx.JSON(w, http.StatusAccepted, uploadResponse(sess, nil, s.cfg.UploadPartSize))
	}

	if err := s.objects.CompleteMultipart(r.Context(), sess.ObjectKey, sess.MultipartID, body.Parts); err != nil {
		obs.UploadFailed("complete_multipart")
		_ = s.store.MarkUploadFailed(r.Context(), actor.OrganizationID, id, "合并分片失败")
		return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "complete_failed",
			"合并分片失败").WithCause(err)
	}

	// **核对真实大小**，不信客户端声明的。差一个字节都算异常 ——
	// 大小对不上意味着传上去的不是它说的那个东西
	actual, _, err := s.objects.Stat(r.Context(), sess.ObjectKey)
	if err != nil {
		obs.UploadFailed("stat")
		return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "stat_failed",
			"读不到对象元数据").WithCause(err)
	}
	if actual != sess.DeclaredSize {
		obs.UploadFailed("size_mismatch")
		_ = s.store.MarkUploadFailed(r.Context(), actor.OrganizationID, id,
			fmt.Sprintf("对象大小 %d 与声明的 %d 不符", actual, sess.DeclaredSize))
		s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
			"upload.size_mismatch", id, actor.RequestID,
			map[string]any{"declared": sess.DeclaredSize, "actual": actual})
		return apierr.New(http.StatusUnprocessableEntity, apierr.TypeInvalidRequest,
			"size_mismatch", "对象大小与声明不符，会话已作废")
	}

	updated, _, err := s.store.FinalizeUpload(r.Context(), actor.OrganizationID, id,
		r.Header.Get("Idempotency-Key"), actual, body.Engine, body.Options)
	if err != nil {
		return err
	}
	obs.UploadCompleted(actual)
	s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind),
		"upload.finalized", id, actor.RequestID, map[string]any{"size": actual})

	// 状态是 verifying，**不是 ready**：摘要还没校验完，
	// 文档在通过校验之前不得进入解析（§9.1）
	return httpx.JSON(w, http.StatusAccepted, uploadResponse(updated, nil, s.cfg.UploadPartSize))
}

func uploadResponse(u *store.UploadSession, parts []objectstore.Part, partSize int64) map[string]any {
	out := map[string]any{
		"id":            u.ID,
		"status":        u.Status,
		"object_key":    u.ObjectKey,
		"filename":      u.Filename,
		"mime":          u.MIME,
		"declared_size": u.DeclaredSize,
		"part_size":     partSize,
		"expires_at":    u.ExpiresAt,
	}
	// **服务端自己算出来的那个摘要要透出来。**
	// 它是"文档身份不是客户端说了算"的唯一证据：客户端声明的 sha256 只是
	// 一个声明，这个是服务端流式读完整个对象算出来的。
	// 不透出来的话，"服务端到底算没算"从外面看不出来 ——
	// 而那正是 e2e 想验的东西（真实用户路径里那条断言就卡在这儿）。
	if u.VerifiedSHA256 != nil {
		out["verified_sha256"] = *u.VerifiedSHA256
	}
	if u.ActualSize != nil {
		out["actual_size"] = *u.ActualSize
	}
	if parts != nil {
		out["parts"] = parts
	}
	if u.Error != nil {
		out["error"] = *u.Error
	}
	return out
}
