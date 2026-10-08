package store

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func TestQuotaWindowRotatesAfterPeriodEnd(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	if _, err := s.Quota(ctx, org); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.quotas SET pages_limit=100, period_days=30, period_start=now()-interval '60 days' WHERE organization_id=$1`, org); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordUsage(ctx, org, "u", "user", "", "parse", 90, 1, auth.NewID()); err != nil {
		t.Fatal(err)
	}
	q, err := s.Quota(ctx, org)
	if err != nil {
		t.Fatal(err)
	}
	if q.PagesUsed != 0 {
		t.Fatalf("lapsed window still counts old pages: used=%d", q.PagesUsed)
	}
	if time.Since(q.PeriodStart) > time.Hour {
		t.Fatalf("period_start not rotated: %v", q.PeriodStart)
	}
}

func TestCheckQuotaCountsSettledUsagePlusHeldSessions(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	if _, err := s.Quota(ctx, org); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.quotas SET pages_limit=3 WHERE organization_id=$1`, org); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordUsage(ctx, org, "u", "user", "", "parse", 1, 1, auth.NewID()); err != nil {
		t.Fatal(err)
	}
	// 真实的 held 行：ClaimUpload 建会话即占 1 页（与生产路径同函数）。
	held := handoffClaim(t, s, org, "u", "check-held", "check-held-digest", nil)
	if held == nil {
		t.Fatal("held session not created")
	}
	// 已用 1（结算）+ 持有 1（未完成上传）= 2，上限 3：再进 1 页可过，进 2 页超。
	if err := s.CheckQuota(ctx, org, 1); err != nil {
		t.Fatalf("within quota should pass: %v", err)
	}
	if err := s.CheckQuota(ctx, org, 2); !isQuotaExceeded(err) {
		t.Fatalf("overrun should fail with ErrQuotaExceeded: %v", err)
	}
	// 检查只读不写：连续检查不累加，used 口径不变。
	if err := s.CheckQuota(ctx, org, 1); err != nil {
		t.Fatalf("repeated check must not consume quota: %v", err)
	}
	q, err := s.Quota(ctx, org)
	if err != nil {
		t.Fatal(err)
	}
	if q.PagesUsed != 1 {
		t.Fatalf("check mutated settled usage: used=%d want 1", q.PagesUsed)
	}
}

// ClaimUpload 的并发受理是原子占住的：上限 2 页、8 个并发各占 1 页，
// 恰好 2 个成功，其余 402 —— 建行即占，集体读到同一 used 一起通过不可能发生。
func TestClaimUploadConcurrentAdmissionHoldsAtomically(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	if _, err := s.Quota(ctx, org); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.quotas SET pages_limit=2 WHERE organization_id=$1`, org); err != nil {
		t.Fatal(err)
	}
	const n = 8
	var g sync.WaitGroup
	won := make(chan bool, n)
	for i := range n {
		g.Go(func() {
			sha := strings.Repeat("e", 64)
			part := int64(5 << 20)
			key := "race-" + auth.NewID()
			digest := "race-digest-" + string(rune('a'+i))
			_, created, err := s.ClaimUpload(ctx, &UploadSession{
				OrganizationID: org, ActorID: "racer", ActorKind: "user",
				ObjectKey: "uploads/" + org + "/" + auth.NewID(),
				Filename:  "input.pdf", MIME: "application/pdf",
				DeclaredSize: 20, DeclaredSHA256: &sha,
				ExpiresAt:            time.Now().Add(time.Hour),
				CreateIdempotencyKey: &key, RequestDigest: &digest, PartSize: &part,
			}, 1)
			if err == nil && created {
				won <- true
				return
			}
			if !isQuotaExceeded(err) {
				t.Errorf("non-quota admission error: %v", err)
			}
			won <- false
		})
	}
	g.Wait()
	close(won)
	passed := 0
	for ok := range won {
		if ok {
			passed++
		}
	}
	if passed != 2 {
		t.Fatalf("concurrent admission passed %d, want exactly 2 (limit 2 x 1 page)", passed)
	}
	var held int
	if err := s.pool.QueryRow(ctx, `SELECT coalesce(sum(reserved_pages),0) FROM control.upload_sessions WHERE organization_id=$1 AND status IN ('created','uploading','verifying')`, org).Scan(&held); err != nil || held != 2 {
		t.Fatalf("held=%d err=%v, want exactly the 2 winners' pages", held, err)
	}
}

func isQuotaExceeded(err error) bool {
	return err != nil && (err == ErrQuotaExceeded || strings.Contains(err.Error(), "quota exceeded"))
}

// key 账单次记账：入口检查只读不写（used 不动），结算 RecordUsage 累加一次，
// 同 event_id 重投不重复记。一次 1 页解析最终 used==1 —— 检查与结算各做各的事。
func TestKeyQuotaCheckedAtAdmissionAccruedOnceOnUsage(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u, err := s.CreateUser(ctx, org, "keyquota-"+auth.NewID(), "", "hash", rbac.Contributor)
	if err != nil {
		t.Fatal(err)
	}
	quota := 2
	k, _, err := s.CreateAPIKey(ctx, org, u.ID, "k", []rbac.Scope{rbac.Scope("parse")}, &quota, 60, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.CheckKeyQuota(ctx, k.ID, 1); err != nil {
		t.Fatal(err)
	}
	// quota=2：进 1 可过（1<=2），进 3 超（0+3>2）。检查不写账，
	// 连续两次检查看到的是同一 used —— 第二次不会因为第一次"占了"而变。
	if err := s.CheckKeyQuota(ctx, k.ID, 3); !isQuotaExceeded(err) {
		t.Fatalf("key overrun should fail: %v", err)
	}
	// 检查不写账：used 仍是 0。
	var used int
	if err := s.pool.QueryRow(ctx, `SELECT used_pages FROM control.api_keys WHERE id=$1`, k.ID).Scan(&used); err != nil || used != 0 {
		t.Fatalf("admission check mutated key usage: used=%d err=%v", used, err)
	}
	ev := auth.NewID()
	if err := s.RecordUsage(ctx, org, u.ID, "user", k.ID, "parse", 1, 1, ev); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordUsage(ctx, org, u.ID, "user", k.ID, "parse", 1, 1, ev); err != nil {
		t.Fatal(err)
	}
	if err := s.pool.QueryRow(ctx, `SELECT used_pages FROM control.api_keys WHERE id=$1`, k.ID).Scan(&used); err != nil {
		t.Fatal(err)
	}
	if used != 1 {
		t.Fatalf("used_pages=%d, want 1 (settlement accrues once, retry not double counted)", used)
	}
	// 结算后检查口径收敛：剩 1 页额度，进 1 可过，进 2 超。
	if err := s.CheckKeyQuota(ctx, k.ID, 1); err != nil {
		t.Fatalf("remaining page should pass: %v", err)
	}
	if err := s.CheckKeyQuota(ctx, k.ID, 2); !isQuotaExceeded(err) {
		t.Fatalf("settled overrun should fail: %v", err)
	}
}

func TestActorNamesScopedToOrganization(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	other := seedOrg(t, s)
	u, err := s.CreateUser(ctx, org, "shown-"+auth.NewID(), "", "hash", rbac.Contributor)
	if err != nil {
		t.Fatal(err)
	}
	k, _, err := s.CreateAPIKey(ctx, org, u.ID, "display-key", []rbac.Scope{rbac.Scope("read")}, nil, 60, nil)
	if err != nil {
		t.Fatal(err)
	}
	got, err := s.ActorNames(ctx, org, []string{u.ID, k.ID, "no-such-id"})
	if err != nil {
		t.Fatal(err)
	}
	if got[u.ID] == "" || got[k.ID] == "" {
		t.Fatalf("same-org actors not resolved: %v", got)
	}
	if _, ok := got["no-such-id"]; ok {
		t.Fatalf("unknown id should stay absent (caller renders placeholder): %v", got)
	}
	cross, err := s.ActorNames(ctx, other, []string{u.ID, k.ID})
	if err != nil {
		t.Fatal(err)
	}
	if len(cross) != 0 {
		t.Fatalf("cross-org disclosure: %v", cross)
	}
}

func TestUpsertOIDCUserSurvivesSquattedIdentity(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	// 固定名在脏库里会撞上历史残留（users.username 全局唯一、无组织隔离）：
	// 每次用随机名，保证测的是"占位"，不是"脏库"。
	squat := "victim-" + auth.NewID()[:8]
	squatEmail := squat + "@example.com"
	victimSub := "victim-" + auth.NewID()
	if _, err := s.CreateUser(ctx, org, squat, squatEmail, "hash", rbac.Contributor); err != nil {
		t.Fatal(err)
	}
	u, err := s.UpsertOIDCUser(ctx, org, "https://idp.example", victimSub, squat, squatEmail, rbac.Contributor)
	if err != nil {
		t.Fatalf("squatted OIDC login must not 500: %v", err)
	}
	if u.Username == squat {
		t.Fatalf("suffixed username expected, got exact squatted name")
	}
	again, err := s.UpsertOIDCUser(ctx, org, "https://idp.example", victimSub, squat, squatEmail, rbac.Contributor)
	if err != nil || again.ID != u.ID {
		t.Fatalf("second login must return same user: %+v %v", again, err)
	}
	if _, err := s.UserByID(ctx, org, u.ID); err != nil {
		t.Fatalf("OIDC user must be org member: %v", err)
	}
}

// 两个回调同时首次登录（同一 issuer/subject）：ON CONFLICT 下恰建一行，
// 两边拿到同一用户 —— double-insert 与"第二登录 500"都不许发生。
func TestUpsertOIDCUserConcurrentFirstLogin(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	issuer := "https://idp.example"
	sub := "race-" + auth.NewID()
	name := "racer-" + auth.NewID()[:8]
	const n = 8
	var g sync.WaitGroup
	ids := make(chan string, n)
	errs := make(chan error, n)
	for range n {
		g.Go(func() {
			u, err := s.UpsertOIDCUser(ctx, org, issuer, sub, name, name+"@example.com", rbac.Contributor)
			if err != nil {
				errs <- err
				return
			}
			ids <- u.ID
		})
	}
	g.Wait()
	close(ids)
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent first login failed: %v", err)
	}
	seen := map[string]bool{}
	for id := range ids {
		seen[id] = true
	}
	if len(seen) != 1 {
		t.Fatalf("concurrent first login made %d users, want 1", len(seen))
	}
	var count int
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM control.users WHERE oidc_issuer=$1 AND oidc_subject=$2`, issuer, sub).Scan(&count); err != nil || count != 1 {
		t.Fatalf("users rows=%d err=%v, want exactly 1", count, err)
	}
}
