package store

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

// ---------------------------------------------------------- 文件访问凭证

// FileGrant 是 `/files/{token}` 背后的那一行。
//
// **路径必须永远稳定**：model-gateway 用这个 URL 下载原件，而文档身份
// `doc_hash` 在没有 `doc_id` 时会回退成 `sha256(file_url)` —— URL 一变，
// 幂等复用与向量索引分块键全部失效（ADR #11/#12，这个项目踩过两次）。
// 所以短期签名只出现在 302 的 Location 里，路径本身不带任何随机成分。
type FileGrant struct {
	Token          string
	OrganizationID string
	SubjectID      string
	ResourceID     string
	DocumentID     string
	ObjectKey      string
	MIME           string
	// 原始文件名。**必须存下来** —— 直读 URL 是跨源的，浏览器忽略
	// `<a download>` 的提示，只认服务端签在 response-content-disposition
	// 里的那个。不存的话用户下到的是一个 document id
	Filename  string
	Scope     string
	ExpiresAt *time.Time
	Revoked   bool
}

func (s *Store) CreateFileGrant(ctx context.Context, g *FileGrant) error {
	if g.Token == "" {
		g.Token = auth.NewToken()
	}
	if g.Scope == "" {
		g.Scope = "source"
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO control.file_grants
		    (token, organization_id, document_id, object_key, mime, filename, scope, expires_at, subject_id, resource_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
		g.Token, g.OrganizationID, g.DocumentID, g.ObjectKey, g.MIME, g.Filename,
		g.Scope, g.ExpiresAt, g.SubjectID, g.ResourceID)
	return err
}

// FileGrantByToken 只返回**当前有效**的凭证。
// 撤销与过期在这里一起判掉，调用方拿不到一个"存在但不该用"的对象 ——
// 那种对象迟早会被某个分支漏判。
func (s *Store) FileGrantByToken(ctx context.Context, token string) (*FileGrant, error) {
	g := &FileGrant{Token: token}
	err := s.pool.QueryRow(ctx, `
		SELECT organization_id, document_id, object_key, mime, scope, expires_at, subject_id, resource_id, filename
		FROM control.file_grants
		WHERE token = $1 AND revoked = FALSE
		  AND (expires_at IS NULL OR expires_at > now())`, token).
		Scan(&g.OrganizationID, &g.DocumentID, &g.ObjectKey, &g.MIME, &g.Scope, &g.ExpiresAt, &g.SubjectID, &g.ResourceID, &g.Filename)
	if err != nil {
		return nil, norows(err)
	}
	return g, nil
}

// StableGrantFor reuses a stable bearer capability within one authorized subject.
// An empty objectKey is read-only: downloads cannot manufacture an empty grant.
func (s *Store) StableGrantFor(ctx context.Context, orgID, documentID, subjectID, resourceID, objectKey, mime, filename string) (*FileGrant, error) {
	if subjectID == "" {
		return nil, ErrNotFound
	}
	g := &FileGrant{OrganizationID: orgID, DocumentID: documentID, SubjectID: subjectID, ResourceID: resourceID, Scope: "source"}
	if objectKey == "" {
		err := s.pool.QueryRow(ctx, `
            SELECT token, object_key, mime, filename FROM control.file_grants
            WHERE organization_id=$1 AND document_id=$2 AND subject_id=$3
              AND resource_id=$4 AND scope='source' AND revoked=FALSE
              AND (expires_at IS NULL OR expires_at > now())`, orgID, documentID, subjectID, resourceID).
			Scan(&g.Token, &g.ObjectKey, &g.MIME, &g.Filename)
		return g, norows(err)
	}
	// Serialize renewal with creation. Expired capabilities remain revoked forever;
	// extending an old bearer token would revive a previously invalid capability.
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	domain, _ := json.Marshal([]string{orgID, documentID, subjectID, resourceID, "source"})
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, string(domain)); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE control.file_grants SET revoked=TRUE
		WHERE organization_id=$1 AND document_id=$2 AND subject_id=$3 AND resource_id=$4
		  AND scope='source' AND revoked=FALSE AND expires_at <= now()`,
		orgID, documentID, subjectID, resourceID); err != nil {
		return nil, err
	}
	// The partial unique index also protects callers outside this renewal path.
	err = tx.QueryRow(ctx, `
        INSERT INTO control.file_grants
          (token, organization_id, document_id, subject_id, resource_id, object_key, mime, filename, scope)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'source')
        ON CONFLICT (organization_id, document_id, scope, subject_id, resource_id) WHERE revoked=FALSE AND subject_id<>''
        DO UPDATE SET filename=CASE WHEN file_grants.filename='' THEN EXCLUDED.filename ELSE file_grants.filename END
        RETURNING token, object_key, mime, filename`,
		auth.NewToken(), orgID, documentID, subjectID, resourceID, objectKey, mime, filename).
		Scan(&g.Token, &g.ObjectKey, &g.MIME, &g.Filename)
	if err != nil {
		return nil, err
	}
	if g.ObjectKey != objectKey {
		return nil, errors.New("immutable file grant object mismatch")
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return g, nil
}

func (s *Store) RevokeFileGrants(ctx context.Context, orgID, documentID string) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE control.file_grants SET revoked = TRUE
		WHERE organization_id = $1 AND document_id = $2`, orgID, documentID)
	return err
}

// ---------------------------------------------------------------- 审计

type AuditEvent struct {
	ID             string          `json:"id"`
	At             time.Time       `json:"at"`
	ActorID        *string         `json:"actor_id"`
	ActorKind      string          `json:"actor_kind"`
	Action         string          `json:"action"`
	Target         *string         `json:"target"`
	RequestID      *string         `json:"request_id"`
	Detail         json.RawMessage `json:"detail"`
	OrganizationID string          `json:"-"`
}

// Audit 记一条审计。
//
// **detail 里绝不能放**：原文全文、JWT、API key、SERVICE_TOKEN、
// 预签名 URL 的查询串、上传内容。审计要能回答"谁在什么时候对什么做了什么"，
// 不需要也不应该能回答"内容是什么"。
func (s *Store) Audit(ctx context.Context, orgID, actorID, actorKind, action, target,
	requestID string, detail map[string]any) {

	if detail == nil {
		detail = map[string]any{}
	}
	payload, err := json.Marshal(detail)
	if err != nil {
		payload = []byte(`{}`)
	}
	// 审计写失败不该让业务请求失败，但**必须留下日志** ——
	// 静默丢审计比不做审计更糟（它让人以为有记录）。
	// detail 不进日志（见上面那段"绝不能放"的清单），只记 action/target 与错误
	if _, err := s.pool.Exec(ctx, `
		INSERT INTO control.audit_events
		    (id, organization_id, actor_id, actor_kind, action, target, request_id, detail)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		auth.NewID(), orgID, nullable(actorID), actorKind, action,
		nullable(target), nullable(requestID), payload); err != nil {

		s.auditFailed(ctx, action, target, requestID, err)
	}
}

func (s *Store) AuditEvents(ctx context.Context, orgID, action string, before *time.Time, limit int) ([]AuditEvent, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, at, actor_id, actor_kind, action, target, request_id, detail
		FROM control.audit_events
		WHERE organization_id = $1
		  AND ($2::text IS NULL OR action = $2)
		  AND ($3::timestamptz IS NULL OR at < $3)
		ORDER BY at DESC
		LIMIT $4`, orgID, nullable(action), before, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []AuditEvent{}
	for rows.Next() {
		var e AuditEvent
		if err := rows.Scan(&e.ID, &e.At, &e.ActorID, &e.ActorKind, &e.Action,
			&e.Target, &e.RequestID, &e.Detail); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// auditFailed 是审计写失败的唯一出口。抽成方法是为了让守卫能钉住它 ——
// `TestAuditLogsWriteFailures` 用一个必然失败的 pool 跑一次，
// 断言日志里出现了 action。**不要改回 `_, _ =`**。
func (s *Store) auditFailed(ctx context.Context, action, target, requestID string, err error) {
	slog.ErrorContext(ctx, "审计事件写入失败",
		"action", action, "target", target, "request_id", requestID, "err", err)
}
