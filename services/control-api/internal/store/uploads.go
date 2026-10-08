package store

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

// UploadSession 是 §9.1 直传流程的服务端记录。
//
// **它存在的全部理由是"服务端要有一句话算数"**：客户端直传对象存储之后，
// 谁来保证这个对象的大小、类型、摘要与它声称的一致？答案是 finalize 时
// 由服务端核对 —— 而核对的依据就是创建会话时记下的这行。
type UploadSession struct {
	ID                   string  `json:"id"`
	CreateIdempotencyKey *string `json:"-"`
	RequestDigest        *string `json:"request_digest,omitempty"`
	AllocationState      string  `json:"allocation_state"`
	PartSize             *int64  `json:"part_size,omitempty"`
	FinalizeDigest       *string `json:"-"`
	Purpose              string  `json:"purpose"`
	RemoteComputeID      *string `json:"remote_compute_id,omitempty"`
	// TargetResourceID 是追加式版本上传的目标资源。创建时冻结：只有 permanent
	// 上传能带它（upload_target_permanent_ck），取值进入创建摘要，重试改值是
	// 幂等冲突。omitempty：没带就是独立建资源的老行为。
	TargetResourceID *string `json:"target_resource_id,omitempty"`
	// IngestStatus 是派生状态，不是列：permanent 上传字节就绪（ready）之前恒
	// 为 null；就绪后从同组织 DocumentSubmitted 事件的投递位推导
	// （pending | retrying | ready | rejected）。ready 只表示事件被 ACK
	// （2xx 或 409 duplicate_event），不是解析/索引完成。
	// 始终序列化：null 与各状态都是有意义的读取结果。
	IngestStatus *string `json:"ingest_status"`
	// IngestError 只在 rejected 时非空，取值只可能是契约枚举 ingest_rejection
	// 的码；绝不透出上游原始错误/密钥（重试原因只留在 outbox.last_error）。
	IngestError    *string         `json:"ingest_error"`
	OrganizationID string          `json:"-"`
	ActorID        string          `json:"-"`
	ActorKind      string          `json:"-"`
	Status         string          `json:"status"`
	ObjectKey      string          `json:"object_key"`
	MultipartID    string          `json:"-"`
	Filename       string          `json:"filename"`
	MIME           string          `json:"mime"`
	DeclaredSize   int64           `json:"declared_size"`
	ActualSize     *int64          `json:"actual_size,omitempty"`
	DeclaredSHA256 *string         `json:"declared_sha256,omitempty"`
	VerifiedSHA256 *string         `json:"verified_sha256,omitempty"`
	Engine         *string         `json:"engine,omitempty"`
	Options        json.RawMessage `json:"options,omitempty"`
	Error          *string         `json:"error,omitempty"`
	CreatedAt      time.Time       `json:"-"`
	ExpiresAt      time.Time       `json:"expires_at"`
}

func (s *Store) CreateUploadSession(ctx context.Context, u *UploadSession) error {
	u.ID = auth.NewID()
	var sha any
	if u.DeclaredSHA256 != nil {
		sha = *u.DeclaredSHA256
	}
	// target_resource_id 与会话同一行冻结：NULL 即独立建资源的老行为；
	// temporary_compute 带 target 由 upload_target_permanent_ck 拒掉。
	return s.pool.QueryRow(ctx, `
		INSERT INTO control.upload_sessions
		    (id, organization_id, actor_id, actor_kind, status, object_key, upload_id,
		     filename, mime, declared_size, declared_sha256, expires_at, target_resource_id)
		VALUES ($1,$2,$3,$4,'created',$5,$6,$7,$8,$9,$10,$11,$12)
		RETURNING created_at`,
		u.ID, u.OrganizationID, u.ActorID, u.ActorKind, u.ObjectKey, u.MultipartID,
		u.Filename, u.MIME, u.DeclaredSize, sha, u.ExpiresAt, nullableTarget(u.TargetResourceID)).Scan(&u.CreatedAt)
}

func (s *Store) UploadSession(ctx context.Context, orgID, id string) (*UploadSession, error) {
	u := &UploadSession{OrganizationID: orgID}
	err := s.pool.QueryRow(ctx, `
		SELECT id, actor_id, actor_kind, status, object_key, coalesce(upload_id, ''),
		       filename, mime, declared_size, actual_size, declared_sha256, verified_sha256,
		       engine, options, error, created_at, expires_at, create_idempotency_key, request_digest, allocation_state, part_size, finalize_digest,
		       purpose, remote_compute_id, target_resource_id
		FROM control.upload_sessions
		WHERE id = $1 AND organization_id = $2`, id, orgID).
		Scan(&u.ID, &u.ActorID, &u.ActorKind, &u.Status, &u.ObjectKey, &u.MultipartID,
			&u.Filename, &u.MIME, &u.DeclaredSize, &u.ActualSize, &u.DeclaredSHA256,
			&u.VerifiedSHA256, &u.Engine, &u.Options, &u.Error, &u.CreatedAt, &u.ExpiresAt, &u.CreateIdempotencyKey, &u.RequestDigest, &u.AllocationState, &u.PartSize, &u.FinalizeDigest, &u.Purpose, &u.RemoteComputeID, &u.TargetResourceID)
	if err != nil {
		return nil, norows(err)
	}
	// ingest_status 永远从 durable outbox 推导：字节没 ready 之前恒 null，
	// ready 之后缺事件是 pending 而不是 ready。
	if err := s.attachIngestStatus(ctx, orgID, u); err != nil {
		return nil, err
	}
	return u, nil
}

// FinalizeUpload 把会话推进到 verifying，并在**同一个事务**里写 outbox 事件。
//
// 幂等由 `(organization_id, idempotency_key)` 的唯一索引保证：
// finalize 重试不得创建两份任务（§9.1 的"必须避免"清单第 4 条）。
// 已经不是 created/uploading 的会话直接返回当前状态，不报错 ——
// 重试拿到 202 是对的，那正是幂等的表现。
func (s *Store) FinalizeUpload(ctx context.Context, orgID, id, idempotencyKey string,
	actualSize int64, engine string, options json.RawMessage) (*UploadSession, bool, error) {

	var out *UploadSession
	created := false
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		var status string
		if err := tx.QueryRow(ctx, `
			SELECT status FROM control.upload_sessions
			WHERE id = $1 AND organization_id = $2 FOR UPDATE`, id, orgID).Scan(&status); err != nil {
			return norows(err)
		}
		// 取值来自契约生成物，不手写字面量（铁律 1）
		if contracts.UploadStatus(status) != contracts.UploadStatusCreated &&
			contracts.UploadStatus(status) != contracts.UploadStatusUploading {
			return nil // 幂等：已经 finalize 过了
		}
		var engineArg any
		if engine != "" {
			engineArg = engine
		}
		if len(options) == 0 {
			options = json.RawMessage(`{}`)
		}
		if _, err := tx.Exec(ctx, `
			UPDATE control.upload_sessions
			SET status = 'verifying', actual_size = $3, engine = $4, options = $5,
			    idempotency_key = coalesce(idempotency_key, $6), updated_at = now()
			WHERE id = $1 AND organization_id = $2`,
			id, orgID, actualSize, engineArg, options, nullable(idempotencyKey)); err != nil {
			return err
		}
		created = true
		return nil
	})
	if err != nil {
		return nil, false, err
	}
	out, err = s.UploadSession(ctx, orgID, id)
	return out, created, err
}

// MarkUploadVerified 校验通过：置 ready 并在同一事务里发出 DocumentSubmitted。
//
// **事件与状态必须同一个事务**：分两次写的话，进程在中间崩溃会留下一个
// 永远 ready 却没人消费的会话 —— 用户看到"上传成功"，文档却永远不出现。
//
// 领取围栏：只认"租约还在"的 verifying 行（verify_claimed_at 在窗内）。
// 两个副本同时 Digest 同一个对象时，只有一个持有租约 —— 另一个的 verify
// 落空（404），Digest 的 CPU/S3 账单只付一份。verifyUploads 循环里
// 刚从 PendingVerification 领到的行租约一定在窗内，当次 verify 不受影响。
func (s *Store) MarkUploadVerified(ctx context.Context, orgID, id, sha256 string) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		var (
			uploadID, objectKey, filename, mime string
			actorID, actorKind, engine          string
			size                                int64
			purpose, remoteComputeID            string
			target                              *string
			options                             json.RawMessage
		)
		// target 只读存储行的冻结值：finalize 输入里没有它，也绝不能从参数传进来。
		if err := tx.QueryRow(ctx, `
			UPDATE control.upload_sessions
			SET status = 'ready', verified_sha256 = $3, updated_at = now()
			WHERE id = $1 AND organization_id = $2 AND status = 'verifying'
			  AND verify_claimed_at IS NOT NULL
			  AND verify_claimed_at >= now() - ($4 * interval '1 second')
			RETURNING id, object_key, filename, mime, coalesce(actual_size, 0),
			          coalesce(engine, ''), options, actor_id, actor_kind,
			          coalesce(purpose, 'permanent'), coalesce(remote_compute_id, ''),
			          target_resource_id`,
			id, orgID, sha256, int(verifyClaimLease.Seconds())).
			Scan(&uploadID, &objectKey, &filename, &mime, &size,
				&engine, &options, &actorID, &actorKind, &purpose, &remoteComputeID, &target); err != nil {
			return norows(err)
		}
		event := DocumentSubmittedPayload{
			UploadID: uploadID, ObjectKey: objectKey, Filename: filename,
			MIME: mime, Size: size, SHA256: sha256, Engine: engine,
			Options: options, ActorID: actorID, ActorKind: actorKind,
			Purpose: purpose, RemoteComputeID: remoteComputeID,
			TargetResourceID: target,
		}
		payload, err := json.Marshal(event.marshalMap())
		if err != nil {
			return err
		}
		return EnqueueOutbox(ctx, tx, orgID, "DocumentSubmitted", payload)
	})
}

func (s *Store) MarkUploadFailed(ctx context.Context, orgID, id, reason string) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE control.upload_sessions
		SET status = 'failed', error = $3, updated_at = now()
		WHERE id = $1 AND organization_id = $2 AND status IN ('created','uploading','verifying')`, id, orgID, reason)
	return err
}

// PendingVerification 领取一批等待摘要校验的会话，供后台校验器消费。
//
// **领取即加租约**：UPDATE ... FOR UPDATE SKIP LOCKED 让多个副本并行领取
// 而不互相阻塞，也不会把同一行领两次 —— PG 做队列的标准姿势。领到的行
// 在 verifyClaimLease 窗内不再被领走（verify_claimed_at），多副本部署下
// 同一个对象不会被 N 个副本同时全量 Digest。每次领取 verify_attempts +1：
// Digest 一直失败的行计数涨到上限后由 FailStalledVerifications 置 failed
// 并释放 reserved_pages，而不是永远 verifying 把配额吃光。
//
// MarkUploadVerified 只认"租约还在自己手里"的行：
// verifyUploads 循环里刚领到的行当次就能 verify，别的路径（测试/手工）
// 调 MarkUploadVerified 走无租约行会 404 —— 校验工作只从领取处来。
func (s *Store) PendingVerification(ctx context.Context, limit int) ([]UploadSession, error) {
	rows, err := s.pool.Query(ctx, `
		UPDATE control.upload_sessions
		SET verify_claimed_at = now(), verify_attempts = verify_attempts + 1, updated_at = now()
		WHERE id IN (
			SELECT id FROM control.upload_sessions
			WHERE status = 'verifying'
			  AND (verify_claimed_at IS NULL OR verify_claimed_at < now() - ($2 * interval '1 second'))
			ORDER BY updated_at
			LIMIT $1
			FOR UPDATE SKIP LOCKED
		)
		RETURNING id, organization_id, object_key, coalesce(actual_size, 0), declared_sha256`, limit, int(verifyClaimLease.Seconds()))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []UploadSession{}
	for rows.Next() {
		var u UploadSession
		if err := rows.Scan(&u.ID, &u.OrganizationID, &u.ObjectKey, &u.DeclaredSize,
			&u.DeclaredSHA256); err != nil {
			return nil, err
		}
		out = append(out, u)
	}
	return out, rows.Err()
}

// verifyClaimLease 是 verify 领取的租约窗：一次全对象 Digest（含 S3 抖动）
// 通常几十秒内完成，5 分钟盖得住慢对象；崩溃的校验器最多卡住一行 5 分钟，
// 之后别的副本自动接手。FailStalledVerifications 的上限（maxVerifyAttempts）
// 远大于租约窗内的正常重试，所以租约过期重领不会误杀慢对象。
const verifyClaimLease = 5 * time.Minute

// maxVerifyAttempts 是 verify 领取次数上限：Digest 一直失败（对象丢了、
// 权限没了）超过这个次数就不是"慢"，是"永远好不了" —— 置 failed，
// reserved_pages 占用随状态离开 verifying 自动释放（ClaimUpload 的 held
// 求和只看 created/uploading/verifying），配额不再被僵尸会话吃掉。
const maxVerifyAttempts = 20

// FailStalledVerifications 把"领了太多次还 verify 不出来"的会话置 failed。
// verifyUploads 在 Digest 失败时只计数（领取时已 +1），不断言内容损坏；
// 这里是唯一判死刑的地方 —— 阈值之外的行继续 verifying 等对象恢复。
// limit 是单次最多判的行数（UPDATE 没有 LIMIT，用 ctid 子查询圈定）：
// verifyUploads 每轮只处理一小批，剩下的下轮继续 —— 一次扫全表会锁住太多行。
func (s *Store) FailStalledVerifications(ctx context.Context, limit int) (int64, error) {
	if limit <= 0 {
		return 0, nil
	}
	tag, err := s.pool.Exec(ctx, `
		UPDATE control.upload_sessions u SET status = 'failed', error = 'verify stalled: object unreadable', updated_at = now()
		WHERE u.ctid IN (
			SELECT s.ctid FROM control.upload_sessions s
			WHERE s.status = 'verifying' AND s.verify_attempts >= $1
			AND (s.verify_claimed_at IS NULL OR s.verify_claimed_at < now() - ($2 * interval '1 second'))
			LIMIT $3
		)`, maxVerifyAttempts, int(verifyClaimLease.Seconds()), limit)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// ExpireStaleUploads 把过期未完成的会话标成 expired。
// **不删对象**：删除是不可逆的，回收交给 corpus 侧带宽限期的 GC。
// verifying 也过期：Digest 一直失败的行不能靠 expires_at 一直占着 reserved_pages，
// 到期后同样 expired —— 与 created/uploading 同规则。
func (s *Store) ExpireStaleUploads(ctx context.Context) (int64, error) {
	tag, err := s.pool.Exec(ctx, `
		UPDATE control.upload_sessions SET status = 'expired', updated_at = now()
		WHERE status IN ('created', 'uploading', 'verifying') AND expires_at < now()`)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

var ErrUploadIdempotencyConflict = errors.New("upload idempotency conflict")

func purposeOrDefault(purpose string) string {
	if purpose == "" {
		return "permanent"
	}
	return purpose
}

func nullableRemoteCompute(id *string) any {
	if id == nil || *id == "" {
		return nil
	}
	return *id
}

// nullableTarget 把空目标压成 SQL NULL：空串不是"指向一个叫空串的资源"，
// 它是没有目标的老行为；存空串会让事件里多一个无意义的 target 键。
func nullableTarget(id *string) any {
	if id == nil || *id == "" {
		return nil
	}
	return *id
}

// ClassifyIngestRejection 判断 corpus 的错误码是不是对 DocumentSubmitted 的
// **确定性**拒绝（契约枚举 ingest_rejection）。只有这组码是终态；其余一律按
// 暂时故障重试 —— 把可恢复的失败判成终态，已校验的上传就永远进不了语料库。
func ClassifyIngestRejection(code string) (contracts.IngestRejection, bool) {
	r := contracts.IngestRejection(code)
	return r, r.Valid()
}

// DocumentSubmittedPayload 是 DocumentSubmitted 事件的命名载荷。
// TargetResourceID 只从存储行取：finalize 输入里没有它，方法签名里也不收它。
type DocumentSubmittedPayload struct {
	UploadID         string
	ObjectKey        string
	Filename         string
	MIME             string
	Size             int64
	SHA256           string
	Engine           string
	Options          json.RawMessage
	ActorID          string
	ActorKind        string
	Purpose          string
	RemoteComputeID  string
	TargetResourceID *string
}

// marshalMap 把载荷压成事件 JSON。target 为空时不写键：corpus 侧
// p.get("target_resource_id") 缺键即独立建资源，与老事件字节一致。
func (p DocumentSubmittedPayload) marshalMap() map[string]any {
	m := map[string]any{
		"upload_id":         p.UploadID,
		"object_key":        p.ObjectKey,
		"filename":          p.Filename,
		"mime":              p.MIME,
		"size":              p.Size,
		"sha256":            p.SHA256,
		"engine":            p.Engine,
		"options":           p.Options,
		"actor_id":          p.ActorID,
		"actor_kind":        p.ActorKind,
		"purpose":           p.Purpose,
		"remote_compute_id": p.RemoteComputeID,
	}
	if p.TargetResourceID != nil && *p.TargetResourceID != "" {
		m["target_resource_id"] = *p.TargetResourceID
	}
	return m
}

// attachIngestStatus 从 durable outbox 推导派生 ingest 状态。
// 规则：字节没 ready（或非 permanent）恒 null；ready 后缺事件是 pending；
// delivered_at 有值是 ready；rejected_at 有值是 rejected（ingest_error 为
// 落库的拒绝码，只可能是 ingest_rejection 枚举里的值）；
// 否则 last_error 有值是 retrying，无值是 pending。
// temporary_compute 没有 ingest 语义：即使 ready 也不推导，保持 null。
func (s *Store) attachIngestStatus(ctx context.Context, orgID string, u *UploadSession) error {
	u.IngestStatus = nil
	u.IngestError = nil
	if u.Status != string(contracts.UploadStatusReady) || purposeOrDefault(u.Purpose) != "permanent" {
		return nil
	}
	var deliveredAt, rejectedAt *time.Time
	var lastErr *string
	var attempts int
	err := s.pool.QueryRow(ctx, `
		SELECT delivered_at, rejected_at, last_error, attempts
		FROM control.control_outbox
		WHERE organization_id = $1 AND type = 'DocumentSubmitted'
		  AND payload->>'upload_id' = $2
		ORDER BY created_at DESC
		LIMIT 1`, orgID, u.ID).Scan(&deliveredAt, &rejectedAt, &lastErr, &attempts)
	if err != nil {
		if norows(err) == ErrNotFound {
			u.IngestStatus = new(string(contracts.IngestStatusPending))
			return nil
		}
		return err
	}
	switch {
	case deliveredAt != nil:
		u.IngestStatus = new(string(contracts.IngestStatusReady))
	case rejectedAt != nil:
		u.IngestStatus = new(string(contracts.IngestStatusRejected))
		// 拒绝行的 last_error 只由 MarkOutboxRejected 写入，且只写枚举码；
		// 读出时再过一遍白名单，历史脏值落回最保守的 invalid_upload_target。
		code := contracts.IngestRejectionInvalidUploadTarget
		if lastErr != nil {
			if known, ok := ClassifyIngestRejection(*lastErr); ok {
				code = known
			}
		}
		u.IngestError = new(string(code))
	case attempts > 0 && lastErr != nil && *lastErr != "":
		// 重试中的原因（HTTP 码、上游原文）只留在 last_error 给运维：上游载荷
		// 可能带密钥或内部地址。对上传者 retrying 本身就是全部信息。
		u.IngestStatus = new(string(contracts.IngestStatusRetrying))
	default:
		u.IngestStatus = new(string(contracts.IngestStatusPending))
	}
	return nil
}

// ClaimUpload atomically binds the actor's business key, quota reservation and
// random object key. No S3 operation may run before this transaction commits.
func (s *Store) ClaimUpload(ctx context.Context, u *UploadSession, pages int) (*UploadSession, bool, error) {
	var id string
	created := false
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		// 配额门与 CheckQuota 同一求和、同一行锁（quotaTx）：窗口轮转、
		// 行锁、used+held 判定全在建行之前 —— 建行即占住额度，并发受理不会集体通过。
		limit, period, _, err := quotaTx(ctx, tx, u.OrganizationID)
		if err != nil {
			return err
		}
		if u.CreateIdempotencyKey != nil {
			var digest *string
			err := tx.QueryRow(ctx, `SELECT id, request_digest FROM control.upload_sessions
    WHERE organization_id=$1 AND actor_kind=$2 AND actor_id=$3 AND create_idempotency_key=$4`,
				u.OrganizationID, u.ActorKind, u.ActorID, *u.CreateIdempotencyKey).Scan(&id, &digest)
			if err == nil {
				if digest == nil || u.RequestDigest == nil || *digest != *u.RequestDigest {
					return ErrUploadIdempotencyConflict
				}
				return nil
			}
			if !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}
		if limit != nil {
			var used, held int64
			if err := tx.QueryRow(ctx, `SELECT coalesce(sum(pages),0) FROM control.usage_ledger WHERE organization_id=$1 AND created_at >= $2`, u.OrganizationID, period).Scan(&used); err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `SELECT coalesce(sum(reserved_pages),0) FROM control.upload_sessions WHERE organization_id=$1 AND status IN ('created','uploading','verifying')`, u.OrganizationID).Scan(&held); err != nil {
				return err
			}
			if used+held+int64(pages) > int64(*limit) {
				return ErrQuotaExceeded
			}
		}
		id = auth.NewID()
		_, err = tx.Exec(ctx, `INSERT INTO control.upload_sessions
   (id,organization_id,actor_id,actor_kind,status,object_key,filename,mime,declared_size,declared_sha256,expires_at,create_idempotency_key,request_digest,allocation_state,part_size,reserved_pages,purpose,remote_compute_id,target_resource_id)
   VALUES($1,$2,$3,$4,'created',$5,$6,$7,$8,$9,$10,$11,$12,'pending',$13,$14,$15,$16,$17)`,
			id, u.OrganizationID, u.ActorID, u.ActorKind, u.ObjectKey, u.Filename, u.MIME, u.DeclaredSize, u.DeclaredSHA256, u.ExpiresAt, u.CreateIdempotencyKey, u.RequestDigest, u.PartSize, pages, purposeOrDefault(u.Purpose), nullableRemoteCompute(u.RemoteComputeID), nullableTarget(u.TargetResourceID))
		created = err == nil
		return err
	})
	if err != nil {
		return nil, false, err
	}
	out, err := s.UploadSession(ctx, u.OrganizationID, id)
	return out, created, err
}

func (s *Store) UploadByCreationKey(ctx context.Context, org, kind, actor, key string) (*UploadSession, error) {
	var id string
	err := s.pool.QueryRow(ctx, `SELECT id FROM control.upload_sessions WHERE organization_id=$1 AND actor_kind=$2 AND actor_id=$3 AND create_idempotency_key=$4`, org, kind, actor, key).Scan(&id)
	if err != nil {
		return nil, norows(err)
	}
	return s.UploadSession(ctx, org, id)
}

// StartUploadAllocation is a permanent at-most-once claim, not a lease which
// could expire while S3 is still committing. Recovery only lists the fixed key.
func (s *Store) StartUploadAllocation(ctx context.Context, org, id string) (bool, error) {
	tag, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET allocation_state='allocating',updated_at=now()
 WHERE organization_id=$1 AND id=$2 AND allocation_state='pending' AND status='created' AND expires_at>now()`, org, id)
	return tag.RowsAffected() == 1, err
}
func (s *Store) AttachUploadMultipart(ctx context.Context, org, id, multipart string) error {
	tag, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET upload_id=$3,allocation_state='ready',error=NULL,updated_at=now()
 WHERE organization_id=$1 AND id=$2 AND (upload_id IS NULL OR upload_id=$3)`, org, id, multipart)
	if err == nil && tag.RowsAffected() == 0 {
		return ErrUploadIdempotencyConflict
	}
	return err
}
func (s *Store) MarkUploadAllocationUnknown(ctx context.Context, org, id string) error {
	_, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET allocation_state='unknown',updated_at=now()
 WHERE organization_id=$1 AND id=$2 AND upload_id IS NULL AND allocation_state IN ('allocating','unknown')`, org, id)
	return err
}

// The options/key binding commits before multipart completion; a response loss
// cannot turn a repeated finalize into a different parse command.
func (s *Store) BindUploadFinalize(ctx context.Context, org, id, key, digest string) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		var oldDigest *string
		var oldKey *string
		if err := tx.QueryRow(ctx, `SELECT finalize_digest,idempotency_key FROM control.upload_sessions WHERE organization_id=$1 AND id=$2 FOR UPDATE`, org, id).Scan(&oldDigest, &oldKey); err != nil {
			return norows(err)
		}
		if oldDigest != nil {
			if *oldDigest != digest || oldKey == nil || *oldKey != key {
				return ErrUploadIdempotencyConflict
			}
			return nil
		}
		_, err := tx.Exec(ctx, `UPDATE control.upload_sessions SET finalize_digest=$3,idempotency_key=$4 WHERE organization_id=$1 AND id=$2`, org, id, digest, key)
		if isUniqueViolation(err) {
			return ErrUploadIdempotencyConflict
		}
		return err
	})
}
