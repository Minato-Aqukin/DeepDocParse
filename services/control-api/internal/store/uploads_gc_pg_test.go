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
