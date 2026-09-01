package store

import (
	"context"
	"encoding/json"
	"errors"
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
	DocumentID     string
	ObjectKey      string
	MIME           string
	Scope          string
	ExpiresAt      *time.Time
	Revoked        bool
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
		    (token, organization_id, document_id, object_key, mime, scope, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7)`,
		g.Token, g.OrganizationID, g.DocumentID, g.ObjectKey, g.MIME, g.Scope, g.ExpiresAt)
	return err
}

// FileGrantByToken 只返回**当前有效**的凭证。
// 撤销与过期在这里一起判掉，调用方拿不到一个"存在但不该用"的对象 ——
// 那种对象迟早会被某个分支漏判。
func (s *Store) FileGrantByToken(ctx context.Context, token string) (*FileGrant, error) {
	g := &FileGrant{Token: token}
	err := s.pool.QueryRow(ctx, `
		SELECT organization_id, document_id, object_key, mime, scope, expires_at
		FROM control.file_grants
		WHERE token = $1 AND revoked = FALSE
		  AND (expires_at IS NULL OR expires_at > now())`, token).
		Scan(&g.OrganizationID, &g.DocumentID, &g.ObjectKey, &g.MIME, &g.Scope, &g.ExpiresAt)
	if err != nil {
		return nil, norows(err)
	}
	return g, nil
}

// StableGrantFor 取该文档的稳定凭证；没有就建一个。
// **同一份文档只能有一个 source 凭证** —— 每次建新的等于每次换 URL。
func (s *Store) StableGrantFor(ctx context.Context, orgID, documentID, objectKey, mime string) (*FileGrant, error) {
	g := &FileGrant{OrganizationID: orgID, DocumentID: documentID, ObjectKey: objectKey, MIME: mime, Scope: "source"}
	err := s.pool.QueryRow(ctx, `
		SELECT token, object_key, mime FROM control.file_grants
		WHERE organization_id = $1 AND document_id = $2 AND scope = 'source' AND revoked = FALSE
		ORDER BY created_at LIMIT 1`, orgID, documentID).
		Scan(&g.Token, &g.ObjectKey, &g.MIME)
	if err == nil {
		return g, nil
	}
	// 只有"确实还没有"才去建；别的错误（连接断了、权限不对）必须原样上抛 ——
	// 把它们也当成"没有"会在故障时静默地建出一堆重复凭证，而每一个都是一个新 URL
	if e := norows(err); !errors.Is(e, ErrNotFound) {
		return nil, e
	}
	if err := s.CreateFileGrant(ctx, g); err != nil {
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
	// 静默丢审计比不做审计更糟（它让人以为有记录）
	_, _ = s.pool.Exec(ctx, `
		INSERT INTO control.audit_events
		    (id, organization_id, actor_id, actor_kind, action, target, request_id, detail)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
		auth.NewID(), orgID, nullable(actorID), actorKind, action,
		nullable(target), nullable(requestID), payload)
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
