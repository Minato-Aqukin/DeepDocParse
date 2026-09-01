package store

import (
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
)

// isUniqueViolation 认 23505。
// **不要用字符串匹配错误消息**：消息随 PG 版本与 locale 变化，
// 而这个判断决定的是"返回 409 还是 500"。
func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
