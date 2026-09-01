package store

import (
	"context"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

// Outbox：**跨服务边界的唯一正确姿势。**
//
// 业务数据与事件在同一个本地事务里提交，再由投递器发出去；消费者按事件 ID
// 幂等处理。分两次写（先改状态、再发请求）的话，进程在中间崩溃会留下
// 一个"状态已变但没人知道"的洞 —— 用户看到上传成功，文档却永远不出现。
//
// 投递是**至少一次**的，所以消费端必须幂等（corpus 侧按 event_id 去重）。
type OutboxEvent struct {
	ID             string          `json:"id"`
	OrganizationID string          `json:"organization_id"`
	Type           string          `json:"type"`
	Payload        json.RawMessage `json:"payload"`
	Attempts       int             `json:"attempts"`
	CreatedAt      time.Time       `json:"created_at"`
}

// EnqueueOutbox 必须在**调用方的事务里**执行，所以收的是 pgx.Tx 而不是池。
func EnqueueOutbox(ctx context.Context, tx pgx.Tx, orgID, typ string, payload json.RawMessage) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO control.control_outbox (id, organization_id, type, payload)
		VALUES ($1, $2, $3, $4)`, auth.NewID(), orgID, typ, payload)
	return err
}

// ClaimOutbox 领一批待投递事件。
//
// `FOR UPDATE SKIP LOCKED` 让多个副本可以并行投递而不互相阻塞，
// 也不会把同一条投两次 —— 这是 PG 做队列的标准姿势，比自己写 lease 简单得多。
func (s *Store) ClaimOutbox(ctx context.Context, limit int) ([]OutboxEvent, error) {
	rows, err := s.pool.Query(ctx, `
		WITH claimed AS (
		  SELECT id FROM control.control_outbox
		  WHERE delivered_at IS NULL AND next_attempt_at <= now()
		  ORDER BY created_at
		  LIMIT $1
		  FOR UPDATE SKIP LOCKED
		)
		UPDATE control.control_outbox o
		SET attempts = o.attempts + 1
		FROM claimed c WHERE o.id = c.id
		RETURNING o.id, o.organization_id, o.type, o.payload, o.attempts, o.created_at`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []OutboxEvent{}
	for rows.Next() {
		var e OutboxEvent
		if err := rows.Scan(&e.ID, &e.OrganizationID, &e.Type, &e.Payload,
			&e.Attempts, &e.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

func (s *Store) MarkOutboxDelivered(ctx context.Context, id string) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE control.control_outbox SET delivered_at = now(), last_error = NULL WHERE id = $1`, id)
	return err
}

// MarkOutboxFailed 记下失败原因并按指数退避安排下一次。
//
// **失败原因必须持久化**：投递失败如果只写日志，运维看到的是"文档没进来"
// 而不是"事件投了 7 次都是 502"。退避上限 5 分钟 —— 再长就等于放弃，
// 而放弃必须是人来决定的。
func (s *Store) MarkOutboxFailed(ctx context.Context, id string, attempts int, reason string) error {
	backoff := time.Duration(1<<min(attempts, 8)) * time.Second
	if backoff > 5*time.Minute {
		backoff = 5 * time.Minute
	}
	_, err := s.pool.Exec(ctx, `
		UPDATE control.control_outbox
		SET last_error = $2, next_attempt_at = now() + $3::interval
		WHERE id = $1`, id, truncate(reason, 500), backoff.String())
	return err
}

// OutboxBacklog 供 /metrics 与 /readyz 用：积压与最老事件年龄。
// **队列年龄比队列长度更能说明问题**：长度 100 可能只是刚来一批，
// 最老一条 20 分钟没投出去才是故障。
func (s *Store) OutboxBacklog(ctx context.Context) (count int, oldest time.Duration, err error) {
	var oldestAt *time.Time
	err = s.pool.QueryRow(ctx, `
		SELECT count(*), min(created_at) FROM control.control_outbox
		WHERE delivered_at IS NULL`).Scan(&count, &oldestAt)
	if err == nil && oldestAt != nil {
		oldest = time.Since(*oldestAt)
	}
	return
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
