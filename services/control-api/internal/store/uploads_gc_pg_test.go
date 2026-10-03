package store

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

func TestTerminalUploadReclamationGraceReferencesAndRetry(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	objects := map[string]bool{}
	makeUpload := func(status string, old bool) *UploadSession {
		u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+auth.NewID(), nil)
		stamp := time.Now().Add(-3 * time.Hour)
		if !old {
			stamp = time.Now()
		}
		if status == "rejected" {
			handoffReady(t, s, org, u)
			_, err := s.pool.Exec(ctx, `UPDATE control.control_outbox SET rejected_at=$2,last_error='invalid_upload_target' WHERE organization_id=$1 AND payload->>'upload_id'=$3`, org, stamp, u.ID)
			if err != nil {
				t.Fatal(err)
			}
		} else {
			_, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET status=$2 WHERE id=$1`, u.ID, status)
			if err != nil {
				t.Fatal(err)
			}
		}
		_, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET updated_at=$2,expires_at=$2,allocation_state='ready' WHERE id=$1`, u.ID, stamp)
		if err != nil {
			t.Fatal(err)
		}
		objects[u.ObjectKey] = true
		return u
	}
	failed := makeUpload("failed", true)
	expired := makeUpload("expired", true)
	rejected := makeUpload("rejected", true)
	fresh := makeUpload("failed", false)
	pending := makeUpload("ready", true)
	shared := makeUpload("failed", true)
	unknown := makeUpload("failed", true)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET allocation_state='unknown' WHERE id=$1`, unknown.ID); err != nil {
		t.Fatal(err)
	}
	protector := makeUpload("created", false)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET object_key=$2 WHERE id=$1`, protector.ID, shared.ObjectKey); err != nil {
		t.Fatal(err)
	}
	retry := true
	collect := func(u UploadReclamation) (bool, error) {
		if u.ObjectKey == failed.ObjectKey && retry {
			return false, errors.New("storage unavailable")
		}
		delete(objects, u.ObjectKey)
		return true, nil
	}
	n, err := s.ReclaimTerminalUploads(ctx, time.Hour, 20, collect)
	if err != nil || n != 2 {
		t.Fatalf("first sweep %d %v", n, err)
	}
	for _, u := range []*UploadSession{expired, rejected} {
		if objects[u.ObjectKey] {
			t.Fatalf("terminal bytes retained: %s", u.ID)
		}
	}
	for _, u := range []*UploadSession{failed, fresh, pending, shared, unknown} {
		if !objects[u.ObjectKey] {
			t.Fatalf("protected or retry bytes lost: %s", u.ID)
		}
	}
	var diagnostic *string
	if err := s.pool.QueryRow(ctx, `SELECT reclaim_error FROM control.upload_sessions WHERE id=$1`, failed.ID).Scan(&diagnostic); err != nil || diagnostic == nil {
		t.Fatalf("failure not durable: %v", err)
	}
	retry = false
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET reclaim_attempted_at=now()-interval '6 minutes' WHERE id=$1`, failed.ID); err != nil {
		t.Fatal(err)
	}
	n, err = s.ReclaimTerminalUploads(ctx, time.Hour, 20, collect)
	if err != nil || n != 1 || objects[failed.ObjectKey] {
		t.Fatalf("retry %d %v", n, err)
	}
	n, err = s.ReclaimTerminalUploads(ctx, time.Hour, 20, collect)
	if err != nil || n != 0 {
		t.Fatalf("repeat sweep %d %v", n, err)
	}
}

func TestTerminalUploadReclamationClaimExcludesConcurrentCollector(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+auth.NewID(), nil)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET status='failed',allocation_state='ready',expires_at=now()-interval '3 hours',updated_at=now()-interval '3 hours' WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	claimed := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, err := s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
			close(claimed)
			<-release
			return true, nil
		})
		done <- err
	}()
	select {
	case <-claimed:
	case <-time.After(3 * time.Second):
		t.Fatal("collector never claimed terminal original")
	}
	defer close(release)
	n, err := s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
		t.Error("concurrent collector entered the destructive callback")
		return false, nil
	})
	if err != nil || n != 0 {
		t.Fatalf("concurrent sweep %d %v", n, err)
	}
	// Release the first claim before fixture teardown.
	release <- struct{}{}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestTerminalUploadReclamationCallbackHasNoTransaction(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+auth.NewID(), nil)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET status='failed',allocation_state='ready',expires_at=now()-interval '3 hours',updated_at=now()-interval '3 hours' WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	n, err := s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
		if acquired := s.pool.Stat().AcquiredConns(); acquired != 0 {
			t.Errorf("cleanup callback holds %d pooled connections; want no open transaction", acquired)
		}
		var idle int
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='idle in transaction' AND query LIKE '%control.upload_sessions%'`).Scan(&idle); err != nil {
			return false, err
		}
		if idle != 0 {
			t.Errorf("cleanup callback has %d idle in transaction upload sessions", idle)
		}
		return true, nil
	})
	if err != nil || n != 1 {
		t.Fatalf("sweep %d %v", n, err)
	}
}

func TestTerminalUploadReclamationStaleFinalizePreservesNewerAttempt(t *testing.T) {
	for _, newerSuccess := range []bool{true, false} {
		t.Run(map[bool]string{true: "newer_success", false: "newer_failure"}[newerSuccess], func(t *testing.T) {
			s, org := handoffStore(t)
			ctx := context.Background()
			u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+auth.NewID(), nil)
			if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET status='failed',allocation_state='ready',expires_at=now()-interval '3 hours',updated_at=now()-interval '3 hours' WHERE id=$1`, u.ID); err != nil {
				t.Fatal(err)
			}
			var newerAttempt time.Time
			n, err := s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
				// Advance the persisted lease rather than sleeping five minutes.
				expireCtx, cancel := context.WithTimeout(ctx, time.Second)
				defer cancel()
				if _, err := s.pool.Exec(expireCtx, `UPDATE control.upload_sessions SET reclaim_attempted_at=now()-interval '6 minutes' WHERE id=$1`, u.ID); err != nil {
					t.Errorf("cannot expire the committed claim while cleanup runs: %v", err)
					return false, err
				}
				newerN, err := s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
					if newerSuccess {
						return true, nil
					}
					return false, errors.New("newer storage failure")
				})
				want := 0
				if newerSuccess {
					want = 1
				}
				if err != nil || newerN != want {
					t.Errorf("newer sweep %d %v; want %d", newerN, err, want)
				}
				if err := s.pool.QueryRow(ctx, `SELECT reclaim_attempted_at FROM control.upload_sessions WHERE id=$1`, u.ID).Scan(&newerAttempt); err != nil {
					return false, err
				}
				if newerSuccess {
					return false, errors.New("stale storage failure")
				}
				return true, nil
			})
			if err != nil || n != 0 {
				t.Fatalf("stale sweep %d %v; want no finalized reclamations", n, err)
			}
			var reclaimed bool
			var diagnostic *string
			var attempted time.Time
			if err := s.pool.QueryRow(ctx, `SELECT reclaimed_at IS NOT NULL,reclaim_error,reclaim_attempted_at FROM control.upload_sessions WHERE id=$1`, u.ID).Scan(&reclaimed, &diagnostic, &attempted); err != nil {
				t.Fatal(err)
			}
			if reclaimed != newerSuccess || !attempted.Equal(newerAttempt) {
				t.Fatalf("stale finalize changed newer receipt: reclaimed=%v attempted=%v; want %v %v", reclaimed, attempted, newerSuccess, newerAttempt)
			}
			if newerSuccess && diagnostic != nil {
				t.Fatalf("stale failure overwrote newer success: %s", *diagnostic)
			}
			if !newerSuccess && (diagnostic == nil || *diagnostic != "reclaim_failed:*errors.errorString:newer storage failure") {
				t.Fatalf("stale success overwrote newer failure: %v", diagnostic)
			}
		})
	}
}

func TestTerminalUploadReclamationCrashedClaimRetriesAfterLease(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := handoffClaim(t, s, org, "alice", auth.NewID(), "sha256:"+auth.NewID(), nil)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET status='failed',allocation_state='ready',expires_at=now()-interval '3 hours',updated_at=now()-interval '3 hours' WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	collectorCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	n, err := s.ReclaimTerminalUploads(collectorCtx, time.Hour, 1, func(UploadReclamation) (bool, error) {
		// Simulate a collector stopping after claim commit, before finalization.
		cancel()
		return false, collectorCtx.Err()
	})
	if !errors.Is(err, context.Canceled) || n != 0 {
		t.Fatalf("stopped collector %d %v", n, err)
	}
	var attempted *time.Time
	var diagnostic *string
	if err := s.pool.QueryRow(ctx, `SELECT reclaim_attempted_at,reclaim_error FROM control.upload_sessions WHERE id=$1`, u.ID).Scan(&attempted, &diagnostic); err != nil {
		t.Fatal(err)
	}
	if attempted == nil || diagnostic != nil {
		t.Fatalf("crashed claim was not durably leased: attempted=%v diagnostic=%v", attempted, diagnostic)
	}
	n, err = s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(UploadReclamation) (bool, error) {
		t.Error("unexpired crashed claim entered cleanup")
		return true, nil
	})
	if err != nil || n != 0 {
		t.Fatalf("unexpired sweep %d %v", n, err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET reclaim_attempted_at=now()-interval '6 minutes' WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	callbacks := 0
	n, err = s.ReclaimTerminalUploads(ctx, time.Hour, 1, func(claim UploadReclamation) (bool, error) {
		callbacks++
		if claim.ID != u.ID || claim.ObjectKey != u.ObjectKey {
			t.Errorf("retry changed immutable claim: %+v", claim)
		}
		return true, nil
	})
	if err != nil || n != 1 || callbacks != 1 {
		t.Fatalf("expired lease retry %d %v callbacks=%d", n, err, callbacks)
	}
}
