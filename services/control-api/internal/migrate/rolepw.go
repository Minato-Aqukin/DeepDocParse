package migrate

import (
	"context"
	"fmt"
	"log/slog"
	"os"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// SetRolePasswords 把两个服务角色的口令设成环境变量里的值。
//
// # 为什么口令不在 0002_roles.sql 里
//
// 那个文件是 schema 的一部分，会进 git、会被读、会被贴进工单。
// 口令是部署配置，不是 schema —— 混在一起的话，改口令要改迁移文件，
// 而迁移文件的校验和是被账本钉住的（改一个字就是"迁移被篡改"）。
//
// # 为什么必须有这一步
//
// `CREATE ROLE ddp_corpus LOGIN` **不带口令**，而 PostgreSQL 默认的
// scram-sha-256 认证对没有口令的角色一律拒绝。于是全新部署的表现是：
// 迁移全部成功、control-api 健康、`/readyz` 全绿，而 corpus-api
// 每一次查询都 `InvalidPasswordError` —— 它自己的 `/healthz` 还回 200，
// 因为那条只证明进程活着。
//
// 2026-09-02 第一次真起全栈时就是这样：上传、直传、摘要校验全通过，
// 文档却永远不入库。**这个缺陷单测一条都碰不到**，因为单测用 SQLite。
//
// 没设环境变量就跳过（本机用 trust 认证的部署不需要它），
// 但**跳过要说出来** —— 静默跳过与"设好了"长得一模一样。
func SetRolePasswords(ctx context.Context, pool *pgxpool.Pool) error {
	for _, role := range []struct{ name, env string }{
		{"ddp_control", "CONTROL_DB_PASSWORD"},
		{"ddp_corpus", "CORPUS_DB_PASSWORD"},
	} {
		password := os.Getenv(role.env)
		if password == "" {
			slog.Warn("没设角色口令，跳过（该角色将无法用口令登录）",
				"role", role.name, "env", role.env)
			continue
		}
		// 角色名与口令都不能拼进 SQL 字符串。角色名走标识符转义，
		// 口令走字面量转义 —— ALTER ROLE 不接受占位符参数
		stmt := fmt.Sprintf("ALTER ROLE %s WITH PASSWORD %s",
			pgx.Identifier{role.name}.Sanitize(), quoteLiteral(password))
		if _, err := pool.Exec(ctx, stmt); err != nil {
			// 错误里不能带 stmt —— 它含口令
			return fmt.Errorf("设置角色 %s 的口令失败：%w", role.name, err)
		}
		slog.Info("已设置角色口令", "role", role.name)
	}
	return nil
}

// quoteLiteral 按 PostgreSQL 的规则转义字符串字面量。
// 用 E” 形式并转义反斜杠与单引号，这样口令里带什么字符都安全。
func quoteLiteral(s string) string {
	out := make([]byte, 0, len(s)+8)
	out = append(out, 'E', '\'')
	for i := 0; i < len(s); i++ {
		switch s[i] {
		case '\'':
			out = append(out, '\\', '\'')
		case '\\':
			out = append(out, '\\', '\\')
		default:
			out = append(out, s[i])
		}
	}
	return string(append(out, '\''))
}
