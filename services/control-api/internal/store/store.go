// Package store 是 control schema 的全部数据访问。
//
// 用 pgx 直接写 SQL，不上 ORM：这一层的查询数量有限、形状稳定，
// 而 ORM 会把"这条 SQL 到底扫了什么"藏起来 —— 控制面是所有请求的必经之路，
// 每一条查询的代价都必须一眼看得见。
//
// # 只碰 control schema
//
// 这个包里**不允许出现 `corpus.` 开头的表名**（企业边界 5）。
// 连接用的 `ddp_control` 角色在数据库层面也没有 corpus 的表权限，
// 所以就算写了也会在运行时被拒 —— 两道保险。
// `scripts/check_data_ownership.py` 静态扫这件事。
package store

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ErrNotFound 是"这一行不存在"。handler 据此翻成 404，
// 而不是把 pgx.ErrNoRows 泄露到 HTTP 层。
var ErrNotFound = errors.New("not found")

type Store struct {
	pool *pgxpool.Pool
}

func Open(ctx context.Context, dsn string, maxConns, minConns int32) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("解析 CONTROL_DATABASE_URL 失败：%w", err)
	}
	cfg.MaxConns = maxConns
	cfg.MinConns = minConns
	// 连接预算是全站雪崩的一个入口（风险台账：多 worker 耗尽 PG 连接）。
	// 空闲连接留一会就回收，别让扩容后的副本把连接数堆满
	cfg.MaxConnIdleTime = 5 * time.Minute
	cfg.MaxConnLifetime = time.Hour
	// search_path 钉死：不设的话一个同名的 public 表就能把查询悄悄接管
	cfg.ConnConfig.RuntimeParams["search_path"] = "control"

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, err
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("连不上 control 数据库：%w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close() { s.pool.Close() }

func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

// Pool 只给迁移器与健康检查用。业务代码走本包的方法。
func (s *Store) Pool() *pgxpool.Pool { return s.pool }

// InTx 跑一个本地事务。
//
// **跨边界流程一律是"本地事务 + Outbox"**：业务数据与事件在同一个事务里提交，
// 再由投递器发送（docs/refactor/DATA-OWNERSHIP.md §5）。
// 一次请求里绝不能出现跨服务的分布式事务 —— 那在 HTTP 之上做不对。
func (s *Store) InTx(ctx context.Context, fn func(pgx.Tx) error) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() {
		// Rollback 在已提交的事务上是 no-op，所以这里无条件 defer 是安全的
		_ = tx.Rollback(context.WithoutCancel(ctx))
	}()
	if err := fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func norows(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	return err
}
