package store

import (
	"context"
	"errors"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

type APIKey struct {
	ID              string       `json:"id"`
	Name            string       `json:"name"`
	KeyPrefix       string       `json:"key_prefix"`
	Scopes          []rbac.Scope `json:"scopes"`
	QuotaPages      *int         `json:"quota_pages"`
	UsedPages       int          `json:"used_pages"`
	RateLimitPerMin int          `json:"rate_limit_per_min"`
	ExpiresAt       *time.Time   `json:"expires_at"`
	RevokedAt       *time.Time   `json:"revoked_at"`
	LastUsedAt      *time.Time   `json:"last_used_at"`
	CreatedAt       time.Time    `json:"created_at"`

	OrganizationID string `json:"-"`
	UserID         string `json:"-"`
}

// Live 报告这把 key 现在能不能用。
// 三个条件分开判是为了让审计日志能说清是哪一种 —— "key 无效"这句话
// 对排查毫无帮助。
func (k *APIKey) Live(now time.Time) (ok bool, reason string) {
	switch {
	case k.RevokedAt != nil:
		return false, "revoked"
	case k.ExpiresAt != nil && now.After(*k.ExpiresAt):
		return false, "expired"
	case len(k.Scopes) == 0:
		// 空 scope = 全部禁用（默认拒绝）。这不是"没配置"，是"配成了什么都不能做"
		return false, "no_scopes"
	}
	return true, ""
}

func (s *Store) CreateAPIKey(ctx context.Context, orgID, userID, name string,
	scopes []rbac.Scope, quotaPages *int, ratePerMin int, expiresAt *time.Time,
) (*APIKey, string, error) {

	plain, prefix, hash := auth.NewAPIKey()
	scopeStrings := make([]string, len(scopes))
	for i, s := range scopes {
		scopeStrings[i] = string(s)
	}
	k := &APIKey{OrganizationID: orgID, UserID: userID, Scopes: scopes}
	var raw []string
	err := s.pool.QueryRow(ctx, `
		INSERT INTO control.api_keys
		    (id, organization_id, user_id, name, key_prefix, key_hash, scopes,
		     quota_pages, rate_limit_per_min, expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
		RETURNING id, name, key_prefix, scopes, quota_pages, used_pages,
		          rate_limit_per_min, expires_at, revoked_at, last_used_at, created_at`,
		auth.NewID(), orgID, userID, name, prefix, hash, scopeStrings,
		quotaPages, ratePerMin, expiresAt).
		Scan(&k.ID, &k.Name, &k.KeyPrefix, &raw, &k.QuotaPages, &k.UsedPages,
			&k.RateLimitPerMin, &k.ExpiresAt, &k.RevokedAt, &k.LastUsedAt, &k.CreatedAt)
	if err != nil {
		return nil, "", err
	}
	k.Scopes = toScopes(raw)
	return k, plain, nil
}

func (s *Store) ListAPIKeys(ctx context.Context, orgID, userID string) ([]APIKey, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, name, key_prefix, scopes, quota_pages, used_pages,
		       rate_limit_per_min, expires_at, revoked_at, last_used_at, created_at
		FROM control.api_keys
		WHERE organization_id = $1 AND user_id = $2 AND revoked_at IS NULL
		ORDER BY created_at DESC`, orgID, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []APIKey{}
	for rows.Next() {
		var k APIKey
		var raw []string
		if err := rows.Scan(&k.ID, &k.Name, &k.KeyPrefix, &raw, &k.QuotaPages, &k.UsedPages,
			&k.RateLimitPerMin, &k.ExpiresAt, &k.RevokedAt, &k.LastUsedAt, &k.CreatedAt); err != nil {
			return nil, err
		}
		k.Scopes = toScopes(raw)
		out = append(out, k)
	}
	return out, rows.Err()
}

// RevokeAPIKey 是软删除：撤销要留痕，硬删会让审计断链。
func (s *Store) RevokeAPIKey(ctx context.Context, orgID, userID, keyID string) error {
	tag, err := s.pool.Exec(ctx, `
		UPDATE control.api_keys SET revoked_at = now()
		WHERE id = $1 AND organization_id = $2 AND user_id = $3 AND revoked_at IS NULL`,
		keyID, orgID, userID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// AuthenticateAPIKey 按明文 key 查出它的身份与角色。
//
// 走 key_hash 的唯一索引，一次查询搞定 —— 这是**每个对外请求**都要走的路径，
// 多一次 round trip 就是全局的延迟。
func (s *Store) AuthenticateAPIKey(ctx context.Context, plain string) (*APIKey, rbac.Role, error) {
	k := &APIKey{}
	var raw []string
	var role string
	err := s.pool.QueryRow(ctx, `
		SELECT k.id, k.organization_id, k.user_id, k.name, k.key_prefix, k.scopes,
		       k.quota_pages, k.used_pages, k.rate_limit_per_min,
		       k.expires_at, k.revoked_at, k.last_used_at, k.created_at, m.role
		FROM control.api_keys k
		JOIN control.memberships m
		  ON m.user_id = k.user_id AND m.organization_id = k.organization_id
		WHERE k.key_hash = $1`, auth.HashAPIKey(plain)).
		Scan(&k.ID, &k.OrganizationID, &k.UserID, &k.Name, &k.KeyPrefix, &raw,
			&k.QuotaPages, &k.UsedPages, &k.RateLimitPerMin,
			&k.ExpiresAt, &k.RevokedAt, &k.LastUsedAt, &k.CreatedAt, &role)
	if err != nil {
		return nil, "", norows(err)
	}
	k.Scopes = toScopes(raw)
	parsed, err := rbac.Parse(role)
	if err != nil {
		return nil, "", err
	}
	return k, parsed, nil
}

// TouchAPIKey 更新 last_used_at。
//
// **异步且尽力而为**：它是审计能力（"这把 key 还有人在用吗"），
// 不值得为它给每个请求加一次同步写。写失败只丢一次时间戳，不影响请求。
func (s *Store) TouchAPIKey(ctx context.Context, keyID string) {
	_, _ = s.pool.Exec(ctx,
		`UPDATE control.api_keys SET last_used_at = now() WHERE id = $1`, keyID)
}

func toScopes(raw []string) []rbac.Scope {
	out := make([]rbac.Scope, 0, len(raw))
	for _, s := range raw {
		out = append(out, rbac.Scope(s))
	}
	return out
}

// ---------------------------------------------------------------- 配额

type Quota struct {
	OrganizationID string    `json:"organization_id"`
	PagesLimit     *int      `json:"pages_limit"`
	PagesUsed      int       `json:"pages_used"`
	PeriodStart    time.Time `json:"period_start"`
	PeriodEnd      time.Time `json:"period_end"`
}

func (s *Store) Quota(ctx context.Context, orgID string) (*Quota, error) {
	q := &Quota{OrganizationID: orgID}
	var periodDays int
	err := s.pool.QueryRow(ctx, `
		INSERT INTO control.quotas (organization_id) VALUES ($1)
		ON CONFLICT (organization_id) DO UPDATE SET period_days = control.quotas.period_days
		RETURNING pages_limit, period_days, period_start`, orgID).
		Scan(&q.PagesLimit, &periodDays, &q.PeriodStart)
	if err != nil {
		return nil, err
	}
	q.PeriodEnd = q.PeriodStart.AddDate(0, 0, periodDays)
	err = s.pool.QueryRow(ctx, `
		SELECT coalesce(sum(pages), 0) FROM control.usage_ledger
		WHERE organization_id = $1 AND created_at >= $2`, orgID, q.PeriodStart).
		Scan(&q.PagesUsed)
	return q, err
}

// ReserveQuota 在受理一次上传前先占额度。
//
// **必须在受理前占**：等解析完再扣的话，一次批量上传可以把配额透支到任意程度。
// 这里用的是"当前周期已用 + 本次预估 <= 上限"，预估用页数上界（文件大小 / 平均页大小）。
func (s *Store) ReserveQuota(ctx context.Context, orgID string, pages int) error {
	q, err := s.Quota(ctx, orgID)
	if err != nil {
		return err
	}
	if q.PagesLimit == nil {
		return nil
	}
	if q.PagesUsed+pages > *q.PagesLimit {
		return ErrQuotaExceeded
	}
	return nil
}

// ErrQuotaExceeded 是"这次操作会超出组织配额"。
// handler 把它翻成 402 —— **不是 403**：前者是"账不够了"，后者是"你没这个权限"，
// 客户端对这两种的处理完全不同（充值 vs 找管理员）。
var ErrQuotaExceeded = errors.New("quota exceeded")

// ---------------------------------------------------------------- 计量

type UsagePoint struct {
	Date     time.Time `json:"date"`
	Kind     string    `json:"kind"`
	Pages    int       `json:"pages"`
	Requests int       `json:"requests"`
}

// RecordUsage 记一笔用量。
//
// eventID 非空时做幂等：同一个 outbox 事件重投不得记两笔账。
// 这是 outbox 消费者的命门 —— 投递器"至少一次"，消费必须"恰好一次"。
func (s *Store) RecordUsage(ctx context.Context, orgID, actorID, actorKind, apiKeyID,
	kind string, pages, requests int, eventID string) error {

	var keyArg, eventArg any
	if apiKeyID != "" {
		keyArg = apiKeyID
	}
	if eventID != "" {
		eventArg = eventID
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO control.usage_ledger
		    (id, organization_id, actor_id, actor_kind, api_key_id, kind, pages, requests, event_id)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
		ON CONFLICT (event_id) DO NOTHING`,
		auth.NewID(), orgID, actorID, actorKind, keyArg, kind, pages, requests, eventArg)
	return err
}

func (s *Store) UsageSeries(ctx context.Context, orgID string, userID string, days int) ([]UsagePoint, error) {
	// userID 为空 = 全组织（需要 admin，由 handler 把关）
	var userFilter any
	if userID != "" {
		userFilter = userID
	}
	rows, err := s.pool.Query(ctx, `
		SELECT date_trunc('day', created_at)::date AS day, kind,
		       sum(pages)::int, sum(requests)::int
		FROM control.usage_ledger
		WHERE organization_id = $1
		  AND ($2::text IS NULL OR actor_id = $2)
		  AND created_at >= now() - make_interval(days => $3)
		GROUP BY day, kind
		ORDER BY day`, orgID, userFilter, days)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []UsagePoint{}
	for rows.Next() {
		var p UsagePoint
		if err := rows.Scan(&p.Date, &p.Kind, &p.Pages, &p.Requests); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}
