package store

import (
	"context"
	"encoding/json"
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
	ID             string          `json:"id"`
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
	return s.pool.QueryRow(ctx, `
		INSERT INTO control.upload_sessions
		    (id, organization_id, actor_id, actor_kind, status, object_key, upload_id,
		     filename, mime, declared_size, declared_sha256, expires_at)
		VALUES ($1,$2,$3,$4,'created',$5,$6,$7,$8,$9,$10,$11)
		RETURNING created_at`,
		u.ID, u.OrganizationID, u.ActorID, u.ActorKind, u.ObjectKey, u.MultipartID,
		u.Filename, u.MIME, u.DeclaredSize, sha, u.ExpiresAt).Scan(&u.CreatedAt)
}

func (s *Store) UploadSession(ctx context.Context, orgID, id string) (*UploadSession, error) {
	u := &UploadSession{OrganizationID: orgID}
	err := s.pool.QueryRow(ctx, `
		SELECT id, actor_id, actor_kind, status, object_key, coalesce(upload_id, ''),
		       filename, mime, declared_size, actual_size, declared_sha256, verified_sha256,
		       engine, options, error, created_at, expires_at
		FROM control.upload_sessions
		WHERE id = $1 AND organization_id = $2`, id, orgID).
		Scan(&u.ID, &u.ActorID, &u.ActorKind, &u.Status, &u.ObjectKey, &u.MultipartID,
			&u.Filename, &u.MIME, &u.DeclaredSize, &u.ActualSize, &u.DeclaredSHA256,
			&u.VerifiedSHA256, &u.Engine, &u.Options, &u.Error, &u.CreatedAt, &u.ExpiresAt)
	if err != nil {
		return nil, norows(err)
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
func (s *Store) MarkUploadVerified(ctx context.Context, orgID, id, sha256 string) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		var (
			uploadID, objectKey, filename, mime string
			actorID, actorKind, engine          string
			size                                int64
			options                             json.RawMessage
		)
		if err := tx.QueryRow(ctx, `
			UPDATE control.upload_sessions
			SET status = 'ready', verified_sha256 = $3, updated_at = now()
			WHERE id = $1 AND organization_id = $2 AND status = 'verifying'
			RETURNING id, object_key, filename, mime, coalesce(actual_size, 0),
			          coalesce(engine, ''), options, actor_id, actor_kind`,
			id, orgID, sha256).
			Scan(&uploadID, &objectKey, &filename, &mime, &size,
				&engine, &options, &actorID, &actorKind); err != nil {
			return norows(err)
		}
		payload, err := json.Marshal(map[string]any{
			"upload_id":  uploadID,
			"object_key": objectKey,
			"filename":   filename,
			"mime":       mime,
			"size":       size,
			"sha256":     sha256,
			"engine":     engine,
			"options":    options,
			"actor_id":   actorID,
			"actor_kind": actorKind,
		})
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
		WHERE id = $1 AND organization_id = $2`, id, orgID, reason)
	return err
}

// PendingVerification 列出等待摘要校验的会话，供后台校验器领取。
func (s *Store) PendingVerification(ctx context.Context, limit int) ([]UploadSession, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, organization_id, object_key, coalesce(actual_size, 0), declared_sha256
		FROM control.upload_sessions
		WHERE status = 'verifying'
		ORDER BY updated_at
		LIMIT $1`, limit)
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

// ExpireStaleUploads 把过期未完成的会话标成 expired。
// **不删对象**：删除是不可逆的，回收交给 corpus 侧带宽限期的 GC。
func (s *Store) ExpireStaleUploads(ctx context.Context) (int64, error) {
	tag, err := s.pool.Exec(ctx, `
		UPDATE control.upload_sessions SET status = 'expired', updated_at = now()
		WHERE status IN ('created', 'uploading') AND expires_at < now()`)
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
