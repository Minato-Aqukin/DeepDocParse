package store

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

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
//
// 两本账，各记各的：
//
//   - 组织账：usage_ledger 当期求和 + upload_sessions 未完成行的 reserved_pages。
//     入口只做**准入检查**（CheckQuota）：读锁 quotas 行求和比上限，不写用量或
//     hold 行（只在首次建行与周期轮换时写 quotas 本身）—— 网关只是透传，下游
//     corpus 才是结算的地方，入口没有归属行可写、
//     重复记账（旧 ReserveQuota/ReserveKeyQuota 就是这么 double-charge 的，
//     见下），而"检查加等待结算"是网关配额的标准做法：检查挡住已超的组织，
//     并发窗口内的少量超额由结算侧的真实用量收敛（超了下次就进不来）。
//   - key 账：api_keys.used_pages，**唯一写入者是 RecordUsage**（结算时累加，
//     按 event_id 幂等）。入口的检查只读不写，不存在"预占转实耗"的第二步。
//
// 旧设计为什么错：ReserveKeyQuota 在准入时 used_pages+1，RecordUsage 在结算
// 时又 +pages —— 一次 1 页解析记 2 页；失败/被代理掉的请求（没有用量事件）
// 永久占 1 页。准入与结算是两个不同的事件，不能共用同一个累加器做两次加法。

type Quota struct {
	OrganizationID string    `json:"organization_id"`
	PagesLimit     *int      `json:"pages_limit"`
	PagesUsed      int       `json:"pages_used"`
	PeriodStart    time.Time `json:"period_start"`
	PeriodEnd      time.Time `json:"period_end"`
}

// quotaTx 锁住配额行、轮转过期窗口、返回（limit, periodStart）。
// Quota / CheckQuota / ClaimUpload 三处判定共用它 —— 口径永远一致。
// 行锁只保证"读到的求和口径一致"，不保证"并发检查互斥"：检查本身不写行，
// 两个并发检查可以同时通过 —— 真正的互斥只在 ClaimUpload（检查且写行）里。
func quotaTx(ctx context.Context, tx pgx.Tx, orgID string) (*int, time.Time, int, error) {
	var (
		limit      *int
		periodDays int
		period     time.Time
	)
	if err := tx.QueryRow(ctx, `
		INSERT INTO control.quotas (organization_id) VALUES ($1)
		ON CONFLICT (organization_id) DO UPDATE SET period_days = control.quotas.period_days
		RETURNING pages_limit, period_days, period_start`, orgID).
		Scan(&limit, &periodDays, &period); err != nil {
		return nil, time.Time{}, 0, err
	}
	if !time.Now().Before(period.AddDate(0, 0, periodDays)) {
		if err := tx.QueryRow(ctx, `
			UPDATE control.quotas SET period_start = now()
			WHERE organization_id = $1
			RETURNING period_days, period_start`, orgID).
			Scan(&periodDays, &period); err != nil {
			return nil, time.Time{}, 0, err
		}
	}
	// 锁住配额行：同一组织的并发 CheckQuota/ClaimUpload 串行化到这一行上。
	if err := tx.QueryRow(ctx, `
		SELECT pages_limit FROM control.quotas
		WHERE organization_id = $1 FOR UPDATE`, orgID).Scan(&limit); err != nil {
		return nil, time.Time{}, 0, err
	}
	return limit, period, periodDays, nil
}

func (s *Store) Quota(ctx context.Context, orgID string) (*Quota, error) {
	q := &Quota{OrganizationID: orgID}
	// 窗口轮转与读数必须在同一个事务里：先把过期窗口往前拨，再按新起点求和。
	// 分两次的话，并发请求会在"还没轮转的旧起点"上重复计费 —— 配额永远不清零。
	err := s.InTx(ctx, func(tx pgx.Tx) error {
		limit, period, days, err := quotaTx(ctx, tx, orgID)
		if err != nil {
			return err
		}
		q.PagesLimit = limit
		q.PeriodStart = period
		q.PeriodEnd = period.AddDate(0, 0, days)
		return tx.QueryRow(ctx, `
			SELECT coalesce(sum(pages), 0) FROM control.usage_ledger
			WHERE organization_id = $1 AND created_at >= $2`, orgID, q.PeriodStart).
			Scan(&q.PagesUsed)
	})
	if err != nil {
		return nil, err
	}
	return q, nil
}

// CheckQuota 在网关放行一次计费调用前做准入检查。
//
// 口径与上传受理（ClaimUpload）同一求和：当期 usage_ledger 求和 + 未完成
// 上传的 reserved_pages，两边都超才拦。**检查只读不写**：网关是透传，
// 真正的用量由下游 corpus 结算（RecordUsage），入口写 hold 会与结算重复记账。
//
// 并发语义说清楚：两个同时到达的检查可以同时看到"还有 1 页"并同时通过 ——
// 这是检查的固有窗口，不是 bug。超额由结算收敛：用量一落账，下一次检查就
// 进不来。需要硬互斥的场景（上传受理）走 ClaimUpload，那里检查与建行在
// 同一事务里，原子地占住额度。
func (s *Store) CheckQuota(ctx context.Context, orgID string, pages int) error {
	return s.InTx(ctx, func(tx pgx.Tx) error {
		limit, period, _, err := quotaTx(ctx, tx, orgID)
		if err != nil {
			return err
		}
		if limit == nil {
			return nil
		}
		var used, held int64
		if err := tx.QueryRow(ctx, `SELECT coalesce(sum(pages),0) FROM control.usage_ledger WHERE organization_id=$1 AND created_at >= $2`, orgID, period).Scan(&used); err != nil {
			return err
		}
		if err := tx.QueryRow(ctx, `SELECT coalesce(sum(reserved_pages),0) FROM control.upload_sessions WHERE organization_id=$1 AND status IN ('created','uploading','verifying')`, orgID).Scan(&held); err != nil {
			return err
		}
		if used+held+int64(pages) > int64(*limit) {
			return ErrQuotaExceeded
		}
		return nil
	})
}

// CheckKeyQuota 按 key 的 quota_pages 做准入检查。
//
// 只读不写：used_pages 的唯一写入者是结算（RecordUsage），入口检查绝不累加。
// QuotaPages 为空（NULL）= 不限，直接过。
func (s *Store) CheckKeyQuota(ctx context.Context, keyID string, pages int) error {
	var quota *int
	var used int
	if err := s.pool.QueryRow(ctx, `
		SELECT quota_pages, used_pages FROM control.api_keys
		WHERE id = $1`, keyID).Scan(&quota, &used); err != nil {
		return norows(err)
	}
	if quota == nil {
		return nil
	}
	if used+pages > *quota {
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
// 同一事务里把 key 的 used_pages 一起累加：重投（ON CONFLICT 没插入）
// 不得重复扣 key 的额度，否则一次重试就吃掉两份 key 配额。
func (s *Store) RecordUsage(ctx context.Context, orgID, actorID, actorKind, apiKeyID,
	kind string, pages, requests int, eventID string) error {

	var keyArg, eventArg any
	if apiKeyID != "" {
		keyArg = apiKeyID
	}
	if eventID != "" {
		eventArg = eventID
	}
	return s.InTx(ctx, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `
			INSERT INTO control.usage_ledger
			    (id, organization_id, actor_id, actor_kind, api_key_id, kind, pages, requests, event_id)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
			ON CONFLICT (event_id) DO NOTHING`,
			auth.NewID(), orgID, actorID, actorKind, keyArg, kind, pages, requests, eventArg)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 1 && apiKeyID != "" && pages > 0 {
			_, err = tx.Exec(ctx, `
				UPDATE control.api_keys SET used_pages = used_pages + $2
				WHERE id = $1`, apiKeyID, pages)
			return err
		}
		return nil
	})
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
