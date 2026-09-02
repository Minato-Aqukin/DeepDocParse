package store

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// 审计写失败**必须留下日志**。
//
// 之前这里是 `_, _ = s.pool.Exec(...)` —— 紧挨着的注释写着"静默丢审计比
// 不做审计更糟（它让人以为有记录）"，而代码正是那样做的。
// 注释与代码矛盾时，出错的总是代码，但读代码的人会先信注释。
//
// 这条钉住失败路径确实说话了，同时钉住**它不能说太多**：
// detail 里可能有目标标识，日志不是审计表，不该把它整个抄进去。
func TestAuditFailureIsLoggedWithoutLeakingDetail(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })

	s := &Store{}
	s.auditFailed(context.Background(), "apikey.scope_denied", "parse", "req-42",
		errors.New("connection refused"))

	out := buf.String()
	for _, want := range []string{"apikey.scope_denied", "parse", "req-42", "connection refused"} {
		if !strings.Contains(out, want) {
			t.Errorf("审计失败日志里缺 %q：%s", want, out)
		}
	}
	// 日志级别必须是 error：warn 会被大多数部署的告警规则漏掉，
	// 而"审计写不进去"正是最该告警的一类
	if !strings.Contains(out, "level=ERROR") {
		t.Errorf("审计失败没记成 ERROR：%s", out)
	}
}

// 反哨兵：上面那条靠 slog 的默认 handler 生效。
// 如果 auditFailed 改成用别的 logger，上面会静默变成"什么都没断言到"。
func TestAuditFailureTestActuallyCapturesOutput(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	(&Store{}).auditFailed(context.Background(), "x", "y", "z", errors.New("e"))
	if buf.Len() == 0 {
		t.Fatal("一个字都没捕获到 —— 上面那条断言是恒真的")
	}
}

// TestAuditLogsFromTheCallSite 是上一条的**真守卫**。
//
// 上一条直接调 `auditFailed`，验的是 logger 的形状 —— 它挡不住
// "Audit() 里那句 Exec 又被改回 `_, _ =`"，而那正是它要挡的事。
// 独立验收当场演示了这一点：保留 auditFailed、把调用点改回去，测试全绿。
// 一句 docstring 写着"不要改回 `_, _ =`"是**叮嘱，不是守卫**。
//
// 这里从调用点验：给一个必然失败的 pool（建好就关掉），
// 调真正的 `Audit()`，断言日志里出现了这次审计的 action。
// 不需要真库能不能连上 —— 关掉的 pool 对任何 Exec 都返回 closed pool。
func TestAuditLogsFromTheCallSite(t *testing.T) {
	pool, err := pgxpool.New(context.Background(),
		"postgres://nobody@127.0.0.1:1/none?connect_timeout=1")
	if err != nil {
		t.Fatalf("建 pool 失败：%v", err)
	}
	pool.Close() // 之后每一次 Exec 都必然失败

	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})))
	t.Cleanup(func() { slog.SetDefault(prev) })

	(&Store{pool: pool}).Audit(context.Background(), "org-1", "actor-1", "user",
		"probe.action", "probe-target", "req-7", map[string]any{"secret": "不该出现"})

	out := buf.String()
	if !strings.Contains(out, "probe.action") {
		t.Errorf("审计写失败却没有日志 —— Audit() 里那句 Exec 可能又变回 `_, _ =` 了：%s", out)
	}
	// detail 不进日志：审计表能回答"谁对什么做了什么"，日志不该回答"内容是什么"
	if strings.Contains(out, "不该出现") {
		t.Errorf("detail 被写进日志了：%s", out)
	}
}
