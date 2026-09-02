package store

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

// 计量的行为**全在 SQL 里**（按天/按种类聚合、按人隔离、event_id 幂等），
// 没有一处能用假 pool 测出来。旧系统那几条用例（web 的 test_usage.py）
// 跑的是真库，搬到 Go 之后只能继续跑真库。
//
// 所以这个文件连真 PostgreSQL：`CONTROL_TEST_DATABASE_URL` 指过去就跑，
// 没有就跳过并说明原因。CI 的 go job 起了 postgres 服务，**那里是真跑的** ——
// 本机跳过、CI 必跑，比"到处都跳过"诚实得多。
//
//	env CONTROL_TEST_DATABASE_URL=postgres://ddp:ddp@127.0.0.1:15432/deepdocparse go test ./internal/store/
func testPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("CONTROL_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("没有 CONTROL_TEST_DATABASE_URL —— 计量聚合只能对着真 PostgreSQL 验" +
			"（SQL 里的 date_trunc / make_interval 没有可替代的假实现）")
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Fatalf("连不上测试库：%v", err)
	}
	t.Cleanup(pool.Close)
	if err := pool.Ping(context.Background()); err != nil {
		t.Fatalf("测试库 ping 不通：%v", err)
	}
	return pool
}

// seedOrg 建一个隔离的组织，用完就删 —— 测试之间不能互相看见对方的用量，
// 否则"按组织隔离"这条恰好被测试自己的脏数据掩盖掉。
func seedOrg(t *testing.T, s *Store) string {
	t.Helper()
	ctx := context.Background()
	orgID := auth.NewID()
	_, err := s.pool.Exec(ctx,
		`INSERT INTO control.organizations (id, name, slug) VALUES ($1,$2,$3)`,
		orgID, "usage-test-"+orgID[:8], "usage-test-"+orgID[:8])
	if err != nil {
		t.Fatalf("建组织失败：%v", err)
	}
	t.Cleanup(func() {
		s.pool.Exec(context.Background(),
			`DELETE FROM control.usage_ledger WHERE organization_id = $1`, orgID)
		s.pool.Exec(context.Background(),
			`DELETE FROM control.organizations WHERE id = $1`, orgID)
	})
	return orgID
}

func TestUsageSeriesAggregatesByDayAndKind(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)

	// 同一天、同一种类的两条要合成一条；不同种类分开
	for _, rec := range []struct {
		kind  string
		pages int
	}{{"parse", 3}, {"parse", 4}, {"embed", 10}} {
		if err := s.RecordUsage(ctx, org, "user-a", "user", "", rec.kind,
			rec.pages, 1, auth.NewID()); err != nil {
			t.Fatalf("写用量失败：%v", err)
		}
	}

	points, err := s.UsageSeries(ctx, org, "", 7)
	if err != nil {
		t.Fatalf("UsageSeries 出错：%v", err)
	}

	byKind := map[string]UsagePoint{}
	for _, p := range points {
		byKind[p.Kind] = p
	}
	if got := byKind["parse"].Pages; got != 7 {
		t.Errorf("parse 页数 = %d，应为 7（3+4 合成一天一条）", got)
	}
	if got := byKind["parse"].Requests; got != 2 {
		t.Errorf("parse 请求数 = %d，应为 2", got)
	}
	// **embed 也必须计量**。它不按页收费，最容易在"只统计解析"里被漏掉，
	// 而漏掉的表现是用量报表看着正常、成本对不上
	if got := byKind["embed"].Pages; got != 10 {
		t.Errorf("embed 页数 = %d，应为 10 —— embed 也要计量", got)
	}
}

func TestUsageSeriesIsolatesUsers(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)

	s.RecordUsage(ctx, org, "user-a", "user", "", "parse", 5, 1, auth.NewID())
	s.RecordUsage(ctx, org, "user-b", "user", "", "parse", 9, 1, auth.NewID())

	mine, err := s.UsageSeries(ctx, org, "user-a", 7)
	if err != nil {
		t.Fatalf("UsageSeries 出错：%v", err)
	}
	total := 0
	for _, p := range mine {
		total += p.Pages
	}
	if total != 5 {
		t.Errorf("按 user-a 过滤得到 %d 页，应为 5 —— 别人的用量泄漏了", total)
	}

	all, _ := s.UsageSeries(ctx, org, "", 7)
	total = 0
	for _, p := range all {
		total += p.Pages
	}
	if total != 14 {
		t.Errorf("全组织合计 %d 页，应为 14", total)
	}
}

// event_id 是幂等键。投递器会重投（至少一次投递），重投必须不重复计费。
func TestRecordUsageIsIdempotentByEventID(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	event := auth.NewID()

	for i := 0; i < 3; i++ {
		if err := s.RecordUsage(ctx, org, "user-a", "user", "", "parse", 6, 1, event); err != nil {
			t.Fatalf("第 %d 次写用量失败：%v", i+1, err)
		}
	}

	points, _ := s.UsageSeries(ctx, org, "", 7)
	total := 0
	for _, p := range points {
		total += p.Pages
	}
	if total != 6 {
		t.Errorf("同一个 event_id 投三次记成了 %d 页，应为 6 —— 幂等失效就是重复计费", total)
	}
}

// 窗口边界：days 之外的不算。这条挡的是"报表数字忽大忽小"。
func TestUsageSeriesRespectsWindow(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)

	s.RecordUsage(ctx, org, "user-a", "user", "", "parse", 5, 1, auth.NewID())
	// 手工把一条推到 10 天前
	old := auth.NewID()
	s.RecordUsage(ctx, org, "user-a", "user", "", "parse", 100, 1, old)
	if _, err := s.pool.Exec(ctx,
		`UPDATE control.usage_ledger SET created_at = $1 WHERE event_id = $2`,
		time.Now().Add(-10*24*time.Hour), old); err != nil {
		t.Fatalf("改时间失败：%v", err)
	}

	points, _ := s.UsageSeries(ctx, org, "", 7)
	total := 0
	for _, p := range points {
		total += p.Pages
	}
	if total != 5 {
		t.Errorf("7 天窗口内合计 %d 页，应为 5 —— 10 天前那条不该算进来", total)
	}
}
