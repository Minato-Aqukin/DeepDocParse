package migrate

import (
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
)

// 只有 undefined_table (42P01) 才算"全新库、空账本" ——
// 连接失败、没权限 (42501)、search_path 错 (3F000) 必须显式失败，
// 否则 status 撒谎、Up 重跑全部迁移。
func TestIsUndefinedTableOnlyMatches42P01(t *testing.T) {
	undefined := &pgconn.PgError{Code: "42P01", Message: `relation "schema_migrations" does not exist`}
	if !isUndefinedTable(undefined) {
		t.Fatalf("42P01 must read as fresh DB")
	}
	for _, code := range []string{"42501", "3F000", "08006", "XX000", ""} {
		err := &pgconn.PgError{Code: code, Message: "boom"}
		if isUndefinedTable(err) {
			t.Fatalf("code %q must NOT read as fresh DB", code)
		}
	}
	if isUndefinedTable(nil) {
		t.Fatalf("nil must not read as fresh DB")
	}
	if isUndefinedTable(errors.New("conn refused")) {
		t.Fatalf("non-pg errors must propagate, not read as fresh DB")
	}
}
