package store

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/jackc/pgx/v5"
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

// FileGrantByToken 只返回**当前有效**的凭证。
// 撤销与过期在这里一起判掉，调用方拿不到一个"存在但不该用"的对象 ——
// 那种对象迟早会被某个分支漏判。
// NULL 视为无效：0017 之后新行必有 expires_at，存量 NULL 行已被回填；
// 残留的 NULL 行只能是绕过 Go 路径的手工行，不得兑换。
func (s *Store) FileGrantByToken(ctx context.Context, token string) (*FileGrant, error) {
	g := &FileGrant{Token: token}
	err := s.pool.QueryRow(ctx, `
		SELECT organization_id, document_id, object_key, mime, scope, expires_at, subject_id, resource_id, filename
		FROM control.file_grants
		WHERE token = $1 AND revoked = FALSE
		  AND expires_at > now()`, token).
		Scan(&g.OrganizationID, &g.DocumentID, &g.ObjectKey, &g.MIME, &g.Scope, &g.ExpiresAt, &g.SubjectID, &g.ResourceID, &g.Filename)
	if err != nil {
		return nil, norows(err)
	}
	return g, nil
}

// StableGrantFor reuses a stable bearer capability within one authorized subject.
// An empty objectKey is read-only: downloads cannot manufacture an empty grant.
//
// 有界寿命：新签的行 expires_at = now() + fileGrantTTL（24h，见 0017 列默认）。
// FileGrantByToken 本来就把过期当无效，所以旧行到期自动失效；调用方到期后
// 重新 StableGrantFor 拿新 token —— 凭证不再是"一次签发、永久有效"。
// 续签只复用剩余寿命还长于 fileGrantRenewMargin 的行：签出去的 URL 要等网关
// 排队后才被抓取，复用一张只剩几分钟的凭证会让解析在抓取时 404。
// 已过期/已撤销的 capability 永远不再复活 —— 延长旧 token 等于把失效凭证救回来，
// 所以临近到期的行是撤销后换新 token，而不是改它的 expires_at。
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
              AND expires_at > now()`, orgID, documentID, subjectID, resourceID).
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
		  AND scope='source' AND revoked=FALSE AND expires_at <= now() + $5::interval`,
		orgID, documentID, subjectID, resourceID, fileGrantRenewMargin.String()); err != nil {
		return nil, err
	}
	// The partial unique index also protects callers outside this renewal path.
	// expires_at 由 Go 侧显式写（列默认是兜底，直写 SQL 不漏无界行）；
	// 存量行靠 0017 回填，之后没有 NULL 行。
	expiresAt := time.Now().Add(fileGrantTTL)
	g.ExpiresAt = &expiresAt
	err = tx.QueryRow(ctx, `
        INSERT INTO control.file_grants
          (token, organization_id, document_id, subject_id, resource_id, object_key, mime, filename, scope, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'source',$9)
        ON CONFLICT (organization_id, document_id, scope, subject_id, resource_id) WHERE revoked=FALSE AND subject_id<>''
        DO UPDATE SET filename=CASE WHEN file_grants.filename='' THEN EXCLUDED.filename ELSE file_grants.filename END
        RETURNING token, object_key, mime, filename`,
		auth.NewToken(), orgID, documentID, subjectID, resourceID, objectKey, mime, filename, expiresAt).
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

// fileGrantTTL 是稳定文件凭证的有界寿命：24h。
// 与 PresignTTL（15 分钟级、浏览器直链）不同 —— 这是服务端到服务端的
// capability，太短会导致网关每次解析都重新换 token 打破 doc_hash 幂等，
// 太长等于永久凭证。24h 是"每天最多换一次"的折中。
// 不要加新配置项：TTL 的来源只有两处且必须同值 —— 这里的 fileGrantTTL
// 与 0017 迁移的列默认；加配置项只会多一个漂移源。
const fileGrantTTL = 24 * time.Hour

// fileGrantRenewMargin：剩余寿命不足它的凭证不再复用，换一张新的（见 StableGrantFor）。
// 取 TTL 的一半：每份文档每天至多换两次 token，签出去的 URL 至少还能用 12h。
const fileGrantRenewMargin = fileGrantTTL / 2

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

// FederationCorrelation 是联邦生命周期的关联元组（plan.md §8.6）：
// probe → admission → task/step → delivery 的端到端追踪就靠这几个 ID。
// 进 audit_events.detail 的同名字段，也进 obs 的统一日志字段
// （见 obs.FederationFields），两边同名 —— 查审计的人
// 与查日志的人用同一组 key。
//
// 全指针：没走联邦的审计一行也不写，保持老事件字节不变。
type FederationCorrelation struct {
	RootTaskID    *string `json:"root_task_id,omitempty"`
	StepID        *string `json:"step_id,omitempty"`
	ProbeID       *string `json:"probe_id,omitempty"`
	AdmissionID   *string `json:"admission_id,omitempty"`
	Attempt       *int    `json:"attempt,omitempty"`
	CoverageRef   *string `json:"coverage_ref,omitempty"`
	DeliveryState *string `json:"delivery_state,omitempty"`
}

// AuditFederation 把关联元组写进 detail 再调 Audit。
// 关联 ID 不是密钥，原样透；detail 里已有的同名字段不覆盖 ——
// 调用方显式给的值优先。密钥红线与 Audit 同（原文/JWT/key/token/URL 查询串/内容不进审计）。
func (s *Store) AuditFederation(ctx context.Context, orgID, actorID, actorKind, action, target,
	requestID string, detail map[string]any, corr FederationCorrelation) {
	if detail == nil {
		detail = map[string]any{}
	}
	mergeCorrelation(detail, corr)
	s.Audit(ctx, orgID, actorID, actorKind, action, target, requestID, detail)
}

// mergeCorrelation 把关联元组写进 detail：与 obs.FederationFields 同名同义。
// detail 里已有的同名字段不覆盖 —— 调用方显式给的值优先；空指针不写键。
func mergeCorrelation(detail map[string]any, corr FederationCorrelation) {
	put := func(k string, v any) {
		if _, ok := detail[k]; !ok && v != nil {
			detail[k] = v
		}
	}
	if corr.RootTaskID != nil {
		put("root_task_id", *corr.RootTaskID)
	}
	if corr.StepID != nil {
		put("step_id", *corr.StepID)
	}
	if corr.ProbeID != nil {
		put("probe_id", *corr.ProbeID)
	}
	if corr.AdmissionID != nil {
		put("admission_id", *corr.AdmissionID)
	}
	if corr.Attempt != nil {
		put("attempt", *corr.Attempt)
	}
	if corr.CoverageRef != nil {
		put("coverage_ref", *corr.CoverageRef)
	}
	if corr.DeliveryState != nil {
		put("delivery_state", *corr.DeliveryState)
	}
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

// AuditPeerDenialOnce records a peer credential denial attributed to issuer,
// keeping snapshot_id, credential_jti and reason in detail, but at most one
// row per (organization, issuer) per minute. The denial carries the verified
// credential's federation correlation (root/step): tracing a federated read
// that died on trust re-check starts from this row. The check+insert runs in
// one short transaction that first takes pg_advisory_xact_lock over
// ("peer-denial-audit", org, issuer) — the same hashtextextended key
// convention the file-grant renewal path uses — so concurrent requests
// serialize on the same predicate and exactly one wins even under READ
// COMMITTED, where a bare INSERT ... WHERE NOT EXISTS would let two
// transactions both see no row and both insert. The row's `at` and the window
// cutoff both use statement_timestamp() of the INSERT issued after the lock:
// the column default now() is the transaction start, which predates a lock
// wait, and clock_timestamp() is volatile, so it cannot bound the
// audit_org_at_idx range scan. No migration. Like Audit it is fire-and-forget
// (write failures only log), so denials never fail because the audit write did.
func (s *Store) AuditPeerDenialOnce(ctx context.Context, orgID, issuer, action, target,
	requestID string, detail map[string]any, corr FederationCorrelation) {
	if detail == nil {
		detail = map[string]any{}
	}
	// 关联元组进 detail，与 AuditFederation 同键：显式 detail 优先，空指针不写。
	mergeCorrelation(detail, corr)
	payload, err := json.Marshal(detail)
	if err != nil {
		payload = []byte(`{}`)
	}
	if err := s.InTx(ctx, func(tx pgx.Tx) error {
		domain, _ := json.Marshal([]string{"peer-denial-audit", orgID, issuer})
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, string(domain)); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `
			INSERT INTO control.audit_events
			    (id, organization_id, at, actor_id, actor_kind, action, target, request_id, detail)
			SELECT $1,$2,statement_timestamp(),$3,$4,$5,$6,$7,$8
			WHERE NOT EXISTS (
			    SELECT 1 FROM control.audit_events
			    WHERE organization_id = $2
			      AND action = $5
			      AND actor_id = $3
			      AND at > statement_timestamp() - make_interval(secs => 60)
			)`,
			auth.NewID(), orgID, nullable(issuer), string("service"), action,
			nullable(target), nullable(requestID), payload)
		return err
	}); err != nil {
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
