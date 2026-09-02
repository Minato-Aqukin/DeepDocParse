// control-migrate 单独跑 control schema 的迁移。
//
// 与 control-api 启动时自动迁移是同一份实现（internal/migrate），
// 存在的理由是**上线窗口需要把"改库"与"起服务"分开做**：
// 先迁移、看报告、再滚服务，比"起服务顺便改库"可控得多。
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/migrate"
)

func main() {
	dsn := flag.String("database", os.Getenv("CONTROL_DATABASE_URL"), "control schema 的连接串")
	flag.Parse()

	cmd := flag.Arg(0)
	if cmd == "" {
		cmd = "status"
	}
	if *dsn == "" {
		fail("没给 -database，也没有 CONTROL_DATABASE_URL")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		fail("连不上数据库：%v", err)
	}
	defer pool.Close()

	switch cmd {
	case "up":
		ran, err := migrate.Up(ctx, pool)
		if err != nil {
			fail("%v", err)
		}
		if len(ran) == 0 {
			fmt.Println("没有待应用的迁移")
			// **这里也要设** —— 第二次 up 什么都不迁，但换过口令的部署
			// 正指望这一次把新口令写进去
			if err := migrate.SetRolePasswords(ctx, pool); err != nil {
				fail("%v", err)
			}
			return
		}
		for _, v := range ran {
			fmt.Println("已应用", v)
		}
		// 迁移建了角色但不带口令 —— 口令是部署配置，走环境变量补上。
		// 漏了这一步的表现不是报错，而是 corpus-api 每条查询都
		// InvalidPasswordError，而它的 /healthz 照样 200
		if err := migrate.SetRolePasswords(ctx, pool); err != nil {
			fail("%v", err)
		}
	case "status":
		rows, err := migrate.Check(ctx, pool)
		if err != nil {
			fail("%v", err)
		}
		drift := false
		for _, r := range rows {
			state := "待应用"
			if r.Applied {
				state = "已应用"
			}
			if r.Drifted {
				// 校验和不符 = 已经应用过的迁移文件被改过。
				// **这是错误而不是提示**：库里是旧结构，代码读起来是新的
				state = "内容已变（危险）"
				drift = true
			}
			fmt.Printf("%-28s %s\n", r.Version, state)
		}
		if drift {
			os.Exit(1)
		}
	default:
		fail("只认 up / status，收到 %q", cmd)
	}
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "::error::"+format+"\n", args...)
	os.Exit(1)
}
