package migrate

import (
	"context"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestConcurrentClusterRoleCreationCompletesMigration(t *testing.T) {
	dsn := os.Getenv("CONTROL_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("CONTROL_TEST_DATABASE_URL required for the real PostgreSQL catalog race")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	if _, err = pool.Exec(ctx, bootstrapSQL); err != nil {
		t.Fatal(err)
	}
	version := fmt.Sprintf("test_role_race_%d", time.Now().UnixNano())
	controlRole, corpusRole := version+"_control", version+"_corpus"
	t.Cleanup(func() {
		cleanup, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		if _, err := pool.Exec(cleanup, `DELETE FROM control.schema_migrations WHERE version=$1`, version); err != nil {
			t.Error(err)
		}
		for _, role := range []string{controlRole, corpusRole} {
			if _, err := pool.Exec(cleanup, "DROP ROLE IF EXISTS "+role); err != nil {
				t.Error(err)
			}
		}
	})
	body, err := files.ReadFile("sql/0002_roles.sql")
	if err != nil {
		t.Fatal(err)
	}
	statement := string(body)
	statement = statement[strings.Index(statement, "DO $$") : strings.Index(statement, "$$;")+3]
	statement = strings.ReplaceAll(statement, "ddp_control", controlRole)
	statement = strings.ReplaceAll(statement, "ddp_corpus", corpusRole)

	// Hold the first role uncommitted. The competing real migration cannot see
	// it in pg_roles, but its catalog INSERT must wait on the unique index.
	owner, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = owner.Rollback(context.Background()) })
	if _, err = owner.Exec(ctx, "CREATE ROLE "+controlRole+" LOGIN"); err != nil {
		t.Fatal(err)
	}
	finished := make(chan error, 1)
	go func() {
		finished <- applyMigration(ctx, pool, Migration{Version: version, SQL: statement, Checksum: version})
	}()
	blocked := false
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		if err = pool.QueryRow(ctx, `SELECT EXISTS (
			SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
			AND query=$1 AND wait_event_type='Lock')`, statement).Scan(&blocked); err != nil {
			t.Fatal(err)
		}
		if blocked {
			break
		}
		select {
		case err := <-finished:
			t.Fatalf("migration finished before the competing role committed: %v", err)
		default:
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !blocked {
		t.Fatal("competing catalog INSERT did not reach the real unique-index wait")
	}
	if err = owner.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if err = <-finished; err != nil {
		t.Fatalf("concurrent role creation prevented startup: %v", err)
	}
	var roles int
	if err = pool.QueryRow(ctx, `SELECT count(*) FROM pg_roles WHERE rolname IN ($1,$2)`, controlRole, corpusRole).Scan(&roles); err != nil || roles != 2 {
		t.Fatalf("migration did not finish both role creations: roles=%d err=%v", roles, err)
	}
	var checksum string
	if err = pool.QueryRow(ctx, `SELECT checksum FROM control.schema_migrations WHERE version=$1`, version).Scan(&checksum); err != nil || checksum != version {
		t.Fatalf("successful migration was not durably checkpointed: checksum=%q err=%v", checksum, err)
	}
}
