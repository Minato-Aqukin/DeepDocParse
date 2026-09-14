package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
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

	idem, err := uploadIdempotencyKey(r, false)
	if err != nil {
		return err
	}
	if body.SHA256 != nil {
		normalized := strings.ToLower(*body.SHA256)
		if len(normalized) != 64 {
			return apierr.BadRequest("bad_digest", "sha256 必须是 64 位十六进制")
		}
		if _, err := hex.DecodeString(normalized); err != nil {
			return apierr.BadRequest("bad_digest", "sha256 必须是 64 位十六进制")
		}
		body.SHA256 = &normalized
	}
	if idem != "" && body.SHA256 == nil {
		return apierr.BadRequest("digest_required", "幂等上传必须声明完整文件 sha256")
	}
	partSize := s.cfg.UploadPartSize
	if partSize <= 0 || (body.Size+partSize-1)/partSize > 10000 {
		return apierr.BadRequest("bad_upload", "分片数量超过限制")
	}
	digest := uploadDigest(body)
	var idemArg *string
	if idem != "" {
		idemArg = &idem
	}
	candidate := &store.UploadSession{
		OrganizationID: actor.OrganizationID, ActorID: actor.ID, ActorKind: string(actor.Kind),
		// A random immutable key, containing no caller filename or business key.
		ObjectKey: fmt.Sprintf("uploads/%s/%s", actor.OrganizationID, auth.NewID()),
		Filename:  body.Filename, MIME: body.MIME, DeclaredSize: body.Size, DeclaredSHA256: body.SHA256,
		ExpiresAt: time.Now().Add(s.cfg.UploadTTL), CreateIdempotencyKey: idemArg, RequestDigest: &digest, PartSize: &partSize,
	}
	sess, created, err := s.store.ClaimUpload(r.Context(), candidate, int(body.Size/bytesPerPageEstimate)+1)
	if errors.Is(err, store.ErrUploadIdempotencyConflict) {
		return uploadConflict()
	}
	if errors.Is(err, store.ErrQuotaExceeded) {
		return apierr.PaymentRequired("quota_exceeded", "组织输入传输配额不足")
	}
	if err != nil {
		return err
	}
	if err = s.reconcileUploadAllocation(r.Context(), sess, true); err != nil {
		return err
	}
	sess, err = s.store.UploadSession(r.Context(), actor.OrganizationID, sess.ID)
	if err != nil {
		return err
	}
	if created {
		s.store.Audit(r.Context(), actor.OrganizationID, actor.ID, string(actor.Kind), "upload.created", sess.ID, actor.RequestID, map[string]any{"filename": body.Filename, "size": body.Size, "mime": body.MIME})
	}
	code := http.StatusOK
	if created {
		code = http.StatusCreated
	}
	if sess.AllocationState != "ready" {
		code = http.StatusAccepted
	}
	return s.writeUpload(w, r, sess, code)
}

func uploadIdempotencyKey(r *http.Request, required bool) (string, error) {
	key := r.Header.Get("Idempotency-Key")
	if key == "" && !required {
		return "", nil
	}
	if len(key) < 1 || len(key) > 200 {
		return "", apierr.BadRequest("bad_idempotency_key", "Idempotency-Key 必须是 1–200 位 ASCII 可见字符")
	}
	for _, c := range key {
		if c < 33 || c > 126 {
			return "", apierr.BadRequest("bad_idempotency_key", "Idempotency-Key 必须是 ASCII 可见字符")
		}
	}
	return key, nil
}
func uploadDigest(body any) string {
	b, _ := json.Marshal(body)
	d := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(d[:])
}
func uploadConflict() error {
	return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "idempotency_conflict", "该幂等键已绑定另一上传请求")
}

// Allocation claims never expire into permission to create another multipart.
// An interrupted request is reconciled only by its persisted, random object key.
func (s *Server) reconcileUploadAllocation(ctx context.Context, u *store.UploadSession, allowStart bool) error {
	if u.AllocationState == "ready" || u.Status != "created" || !u.ExpiresAt.After(time.Now()) {
		return nil
	}
	started := false
	var err error
	if allowStart && u.AllocationState == "pending" {
		started, err = s.store.StartUploadAllocation(ctx, u.OrganizationID, u.ID)
		if err != nil {
			return err
		}
	}
	if started {
		id, createErr := s.objects.BeginMultipart(ctx, u.ObjectKey, u.MIME)
		// Persist an acquired receipt even if the client connection disappeared.
		persistCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		if createErr == nil {
			return s.store.AttachUploadMultipart(persistCtx, u.OrganizationID, u.ID, id)
		}
		if err := s.store.MarkUploadAllocationUnknown(persistCtx, u.OrganizationID, u.ID); err != nil {
			return err
		}
		return nil
	}
	if u.AllocationState == "pending" {
		return nil
	}
	ids, listErr := s.objects.FindMultipart(ctx, u.ObjectKey)
	if listErr == nil && len(ids) == 1 {
		return s.store.AttachUploadMultipart(ctx, u.OrganizationID, u.ID, ids[0])
	}
	// Zero receipts may mean a still-running create, not "not executed". More
	// than one needs operator cleanup. Neither case signs or creates anything.
	return s.store.MarkUploadAllocationUnknown(ctx, u.OrganizationID, u.ID)
}

func (s *Server) handleReconcileUpload(w http.ResponseWriter, r *http.Request) error {
	actor, err := mustActor(r)
	if err != nil {
		return err
	}
	if err = requireRole(actor, rbac.Role.CanUpload, "上传文档"); err != nil {
		return err
	}
	key, err := uploadIdempotencyKey(r, true)
	if err != nil {
		return err
	}
	sess, err := s.store.UploadByCreationKey(r.Context(), actor.OrganizationID, string(actor.Kind), actor.ID, key)
	if errors.Is(err, store.ErrNotFound) {
		return apierr.NotFound("no_such_upload", "该调用者没有此上传记录；并发创建尚未提交时仍需用原键重试")
	}
	if err != nil {
		return err
	}
	if err = s.reconcileUploadAllocation(r.Context(), sess, false); err != nil {
		return err
	}
	sess, err = s.store.UploadSession(r.Context(), actor.OrganizationID, sess.ID)
	if err != nil {
		return err
	}
	return s.writeUpload(w, r, sess, http.StatusOK)
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
	if sess.ActorID != actor.ID || sess.ActorKind != string(actor.Kind) {
		return apierr.NotFound("no_such_upload", "上传会话不存在")
	}
	if err = requireRole(actor, rbac.Role.CanUpload, "上传文档"); err != nil {
		return err
	}
	if err = s.reconcileUploadAllocation(r.Context(), sess, false); err != nil {
		return err
	}
	sess, err = s.store.UploadSession(r.Context(), actor.OrganizationID, sess.ID)
	if err != nil {
		return err
	}
	return s.writeUpload(w, r, sess, http.StatusOK)
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
	if sess.ActorID != actor.ID || sess.ActorKind != string(actor.Kind) {
		return apierr.NotFound("no_such_upload", "上传会话不存在")
	}
	if err = requireRole(actor, rbac.Role.CanUpload, "上传文档"); err != nil {
		return err
	}
	key, err := uploadIdempotencyKey(r, false)
	if err != nil {
		return err
	}
	if key == "" {
		key = id
	}
	var options any = map[string]any{}
	if len(body.Options) > 0 {
		decoder := json.NewDecoder(bytes.NewReader(body.Options))
		decoder.UseNumber()
		if err = decoder.Decode(&options); err != nil {
			return apierr.BadRequest("bad_options", "无效 options")
		}
	}
	finalizeDigest := uploadDigest(map[string]any{"engine": body.Engine, "options": options})
	if sess.Status == "failed" || sess.Status == "expired" {
		return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "upload_not_active", "上传已失败或过期")
	}
	if (sess.Status == "created" || sess.Status == "uploading") && !sess.ExpiresAt.After(time.Now()) {
		return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "upload_expired", "上传已过期")
	}
	if sess.AllocationState != "ready" {
		return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "allocation_unknown", "对象存储分配尚未确认；请用原幂等键对账")
	}
	if err = s.store.BindUploadFinalize(r.Context(), actor.OrganizationID, id, key, finalizeDigest); err != nil {
		if errors.Is(err, store.ErrUploadIdempotencyConflict) {
			return uploadConflict()
		}
		return err
	}
	// 幂等：已经 finalize 过的直接把当前状态返回去。
	// **重试拿到 202 是对的** —— 那正是幂等的表现，不是错误
	if contracts.UploadStatus(sess.Status) != contracts.UploadStatusCreated &&
		contracts.UploadStatus(sess.Status) != contracts.UploadStatusUploading {
		return httpx.JSON(w, http.StatusAccepted, uploadResponse(sess, nil, s.cfg.UploadPartSize))
	}

	// A prior CompleteMultipart may have committed before its HTTP/PG reply
	// was lost. HEAD the unique persisted key first and continue verification.
	if _, _, statErr := s.objects.Stat(r.Context(), sess.ObjectKey); statErr != nil {
		if !objectstore.IsMissing(statErr) {
			return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "objectstore_error", "无法确认上传对象状态")
		}
		partSize := s.cfg.UploadPartSize
		if sess.PartSize != nil {
			partSize = *sess.PartSize
		}
		storedParts, listErr := s.objects.CompletedParts(r.Context(), sess.ObjectKey, sess.MultipartID)
		if listErr != nil {
			return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "completion_unknown", "无法确认分片，请保留原会话对账")
		}
		valid := objectstore.ValidCompletedParts(storedParts, sess.DeclaredSize, partSize)
		if len(valid) != int((sess.DeclaredSize+partSize-1)/partSize) || len(valid) != len(storedParts) {
			return apierr.New(http.StatusConflict, apierr.TypeInvalidRequest, "upload_incomplete", "分片尚未完整上传，请重签并续传")
		}
		if completeErr := s.objects.CompleteMultipart(r.Context(), sess.ObjectKey, sess.MultipartID, body.Parts); completeErr != nil {
			if _, _, err = s.objects.Stat(r.Context(), sess.ObjectKey); err != nil {
				// Missing parts / interrupted Complete is retriable, never terminal failure.
				return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "completion_unknown", "合并结果未确认，请保留原会话并对账重试")
			}
		}
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
		key, actual, body.Engine, body.Options)
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

// Refresh only missing parts. No signed URL is persisted or returned across
// actor boundaries, for expired sessions, or while allocation is unknown.
func (s *Server) writeUpload(w http.ResponseWriter, r *http.Request, u *store.UploadSession, code int) error {
	partSize := s.cfg.UploadPartSize
	if u.PartSize != nil {
		partSize = *u.PartSize
	}
	w.Header().Set("Cache-Control", "no-store")
	out := uploadResponse(u, nil, partSize)
	if (u.Status == "created" || u.Status == "uploading") && u.AllocationState == "ready" && u.ExpiresAt.After(time.Now()) {
		completed, err := s.objects.CompletedParts(r.Context(), u.ObjectKey, u.MultipartID)
		if err == nil {
			parts, err := s.objects.PresignParts(r.Context(), u.ObjectKey, u.MultipartID, u.DeclaredSize, partSize, completed)
			if err != nil {
				return apierr.New(http.StatusBadGateway, apierr.TypeUpstream, "presign_unavailable", "上传会话已持久化，暂时无法签发分片 URL")
			}
			out["parts"] = parts
			out["completed_parts"] = objectstore.ValidCompletedParts(completed, u.DeclaredSize, partSize)
			obs.PresignedURL("upload")
		} else {
			out["transfer_state"] = "unknown"
			if _, _, statErr := s.objects.Stat(r.Context(), u.ObjectKey); statErr == nil {
				out["transfer_state"] = "complete_pending_finalize"
			}
		}
	}
	return httpx.JSON(w, code, out)
}

func uploadResponse(u *store.UploadSession, parts []objectstore.Part, partSize int64) map[string]any {
	out := map[string]any{
		"id":               u.ID,
		"allocation_state": u.AllocationState,
		"status":           u.Status,
		"object_key":       u.ObjectKey,
		"filename":         u.Filename,
		"mime":             u.MIME,
		"declared_size":    u.DeclaredSize,
		"part_size":        partSize,
		"expires_at":       u.ExpiresAt,
	}
	if u.RequestDigest != nil {
		out["request_digest"] = *u.RequestDigest
	}
	inputState := "waiting_input"
	if u.Status == "verifying" {
		inputState = "content_verifying"
	}
	if u.Status == "ready" && u.VerifiedSHA256 != nil {
		inputState = "content_verified"
	}
	if u.Status == "failed" || u.Status == "expired" {
		inputState = u.Status
	}
	out["input_state"] = inputState
	if u.AllocationState == "unknown" {
		out["allocation_reason"] = "receipt_unconfirmed_or_ambiguous"
		out["recovery_action"] = "reconcile_same_key_or_operator_cleanup"
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
