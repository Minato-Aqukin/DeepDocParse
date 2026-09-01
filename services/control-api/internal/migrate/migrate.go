// Package migrate 跑 database/control/*.sql。
//
// 用最简单的"按文件名顺序执行 + 校验和账本"，不引入迁移框架：
// control schema 的迁移数量有限、都是 DDL，而框架带来的抽象
// （自动生成的 down、模型推断）在这个规模上只会让人看不懂库里到底是什么。
package migrate

import (
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"sort"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed sql/*.sql
var files embed.FS

type Migration struct {
	Version  string
	SQL      string
	Checksum string
}

func Load() ([]Migration, error) {
	entries, err := fs.ReadDir(files, "sql")
	if err != nil {
		return nil, err
	}
	var out []Migration
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".sql") {
			continue
		}
		body, err := files.ReadFile("sql/" + e.Name())
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256(body)
		out = append(out, Migration{
			Version:  strings.TrimSuffix(e.Name(), ".sql"),
			SQL:      string(body),
			Checksum: hex.EncodeToString(sum[:]),
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Version < out[j].Version })
	return out, nil
}

// Up 把还没应用的迁移跑完。
//
// **每个迁移单独一个事务**：一次全包在一个大事务里的话，
// 中间失败会让"已经跑到哪"这个问题没有答案；PG 的 DDL 是事务性的，
// 所以单个迁移失败会干净回滚。
func Up(ctx context.Context, pool *pgxpool.Pool) ([]string, error) {
	if _, err := pool.Exec(ctx, bootstrapSQL); err != nil {
		return nil, fmt.Errorf("建迁移账本失败：%w", err)
	}
	applied, err := appliedMap(ctx, pool)
	if err != nil {
		return nil, err
	}
	migrations, err := Load()
	if err != nil {
		return nil, err
	}

	var ran []string
	for _, m := range migrations {
		if have, ok := applied[m.Version]; ok {
			// **校验和不符要报错，不能当作已执行过。**
			// "改了迁移文件但库里是旧结构"是最难查的一类问题：
			// 代码读起来是新的，行为是旧的
			if have != m.Checksum {
				return ran, fmt.Errorf("迁移 %s 的内容变了（库里 %s，文件 %s）—— "+
					"已经应用过的迁移不许改；要改结构请新加一个迁移文件",
					m.Version, have[:12], m.Checksum[:12])
			}
			continue
		}
		tx, err := pool.Begin(ctx)
		if err != nil {
			return ran, err
		}
		if _, err := tx.Exec(ctx, m.SQL); err != nil {
			_ = tx.Rollback(ctx)
			return ran, fmt.Errorf("迁移 %s 失败：%w", m.Version, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO control.schema_migrations (version, checksum) VALUES ($1, $2)`,
			m.Version, m.Checksum); err != nil {
			_ = tx.Rollback(ctx)
			return ran, err
		}
		if err := tx.Commit(ctx); err != nil {
			return ran, err
		}
		ran = append(ran, m.Version)
	}
	return ran, nil
}

type Status struct {
	Version string
	Applied bool
	Drifted bool
}

func Check(ctx context.Context, pool *pgxpool.Pool) ([]Status, error) {
	applied, err := appliedMap(ctx, pool)
	if err != nil {
		return nil, err
	}
	migrations, err := Load()
	if err != nil {
		return nil, err
	}
	out := make([]Status, 0, len(migrations))
	for _, m := range migrations {
		have, ok := applied[m.Version]
		out = append(out, Status{
			Version: m.Version,
			Applied: ok,
			Drifted: ok && have != m.Checksum,
		})
	}
	return out, nil
}

func appliedMap(ctx context.Context, pool *pgxpool.Pool) (map[string]string, error) {
	out := map[string]string{}
	rows, err := pool.Query(ctx, `SELECT version, checksum FROM control.schema_migrations`)
	if err != nil {
		// 账本还不存在（全新库）—— 不是错误
		return out, nil
	}
	defer rows.Close()
	for rows.Next() {
		var v, c string
		if err := rows.Scan(&v, &c); err != nil {
			return nil, err
		}
		out[v] = c
	}
	return out, rows.Err()
}

// bootstrapSQL 只建账本本身。它必须独立于 0001，
// 否则"账本在 0001 里建"会导致第一次跑时查不到账本而重复执行 0001。
const bootstrapSQL = `
CREATE SCHEMA IF NOT EXISTS control;
CREATE TABLE IF NOT EXISTS control.schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum   TEXT NOT NULL
);`
