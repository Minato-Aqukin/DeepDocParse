package store

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

// makeVerifyingSession builds a verifying session the same way production does:
// ClaimUpload (quota hold) + FinalizeUpload (status verifying).
func makeVerifyingSession(t *testing.T, s *Store, org string) *UploadSession {
	t.Helper()
	sha := strings.Repeat("d", 64)
	part := int64(5 << 20)
	u, created, err := s.ClaimUpload(context.Background(), &UploadSession{
		OrganizationID: org, ActorID: "verifier", ActorKind: "user",
		ObjectKey: "uploads/" + org + "/" + auth.NewID(),
		Filename:  "input.pdf", MIME: "application/pdf",
		DeclaredSize: 20, DeclaredSHA256: &sha,
		ExpiresAt:            time.Now().Add(time.Hour),
		CreateIdempotencyKey: &[]string{auth.NewID()}[0], RequestDigest: &[]string{"dg"}[0], PartSize: &part,
	}, 1)
	if err != nil || !created {
		t.Fatalf("claim: %+v %v", u, err)
	}
	if _, _, err := s.FinalizeUpload(context.Background(), org, u.ID, "fin-"+u.ID, 20, "", json.RawMessage(`{}`)); err != nil {
		t.Fatal(err)
	}
	return u
}

// Single-winner claim — two concurrent PendingVerification calls
// never return the same session (no duplicate full-object digests).
func TestPendingVerificationClaimsSingleWinner(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := makeVerifyingSession(t, s, org)
	var g sync.WaitGroup
	gots := make(chan string, 8)
	for range 4 {
		g.Go(func() {
			rows, err := s.PendingVerification(ctx, 4)
			if err != nil {
				t.Error(err)
				return
			}
			for _, r := range rows {
				gots <- r.ID
			}
		})
	}
	g.Wait()
	close(gots)
	seen := map[string]int{}
	for id := range gots {
		seen[id]++
	}
	if seen[u.ID] != 1 {
		t.Fatalf("session claimed %d times, want exactly 1: %v", seen[u.ID], seen)
	}
	// Lease holds: immediate re-claim returns nothing for the same row.
	again, err := s.PendingVerification(ctx, 4)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range again {
		if r.ID == u.ID {
			t.Fatalf("claimed row re-issued inside lease")
		}
	}
}

// Fence: MarkUploadVerified without a lease must fail; with a lease it wins once.
func TestMarkUploadVerifiedFencedOnClaim(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	u := makeVerifyingSession(t, s, org)
	if err := s.MarkUploadVerified(ctx, org, u.ID, strings.Repeat("d", 64)); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unclaimed verify should 404, got: %v", err)
	}
	claimed, err := s.PendingVerification(ctx, 4)
	if err != nil || len(claimed) == 0 {
		t.Fatalf("claim: %v %+v", err, claimed)
	}
	if err := s.MarkUploadVerified(ctx, org, u.ID, strings.Repeat("d", 64)); err != nil {
		t.Fatalf("claimed verify should win: %v", err)
	}
	if err := s.MarkUploadVerified(ctx, org, u.ID, strings.Repeat("d", 64)); !errors.Is(err, ErrNotFound) {
		t.Fatalf("second verify must fail (no longer verifying): %v", err)
	}
}

// Verifying expiry + stall fail — expired verifying sessions expire
// (releasing the reserved_pages hold), and unreadable ones fail after
// maxVerifyAttempts claims.
func TestVerifyingExpiryAndStallReleaseQuotaHold(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	if _, err := s.Quota(ctx, org); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.quotas SET pages_limit=100 WHERE organization_id=$1`, org); err != nil {
		t.Fatal(err)
	}
	u := makeVerifyingSession(t, s, org)
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET expires_at=now()-interval '1 second' WHERE id=$1`, u.ID); err != nil {
		t.Fatal(err)
	}
	n, err := s.ExpireStaleUploads(ctx)
	if err != nil || n != 1 {
		t.Fatalf("expired verifying should expire: n=%d err=%v", n, err)
	}
	var status string
	if err := s.pool.QueryRow(ctx, `SELECT status FROM control.upload_sessions WHERE id=$1`, u.ID).Scan(&status); err != nil || status != "expired" {
		t.Fatalf("status=%q err=%v", status, err)
	}

	stalled := makeVerifyingSession(t, s, org)
	// Simulate maxVerifyAttempts failed digest cycles: claim until attempts hit cap.
	for range 25 {
		if _, err := s.PendingVerification(ctx, 1); err != nil {
			t.Fatal(err)
		}
		if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET verify_claimed_at=NULL WHERE id=$1`, stalled.ID); err != nil {
			t.Fatal(err)
		}
	}
	n, err = s.FailStalledVerifications(ctx, 8)
	if err != nil || n < 1 {
		t.Fatalf("stalled verifying should fail: n=%d err=%v", n, err)
	}
	if err := s.pool.QueryRow(ctx, `SELECT status FROM control.upload_sessions WHERE id=$1`, stalled.ID).Scan(&status); err != nil || status != "failed" {
		t.Fatalf("stalled status=%q err=%v", status, err)
	}
}

// limit 是单次最多判的行数：3 行卡住、limit=2，第一轮恰判 2 行，
// 剩 1 行 verifying —— 下轮继续，而不是一次扫全表。
func TestFailStalledVerificationsHonorsLimit(t *testing.T) {
	s, org := handoffStore(t)
	ctx := context.Background()
	if _, err := s.Quota(ctx, org); err != nil {
		t.Fatal(err)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.quotas SET pages_limit=100 WHERE organization_id=$1`, org); err != nil {
		t.Fatal(err)
	}
	ids := make([]string, 0, 3)
	for range 3 {
		u := makeVerifyingSession(t, s, org)
		ids = append(ids, u.ID)
	}
	if _, err := s.pool.Exec(ctx, `UPDATE control.upload_sessions SET verify_attempts=$2, verify_claimed_at=NULL WHERE id = ANY($1)`, ids, maxVerifyAttempts); err != nil {
		t.Fatal(err)
	}
	n, err := s.FailStalledVerifications(ctx, 2)
	if err != nil || n != 2 {
		t.Fatalf("limit=2 should fail exactly 2: n=%d err=%v", n, err)
	}
	var failed, verifying int
	if err := s.pool.QueryRow(ctx, `SELECT count(*) FILTER (WHERE status='failed'), count(*) FILTER (WHERE status='verifying') FROM control.upload_sessions WHERE id = ANY($1)`, ids).Scan(&failed, &verifying); err != nil {
		t.Fatal(err)
	}
	if failed != 2 || verifying != 1 {
		t.Fatalf("failed=%d verifying=%d, want 2/1", failed, verifying)
	}
	rest, err := s.FailStalledVerifications(ctx, 2)
	if err != nil || rest != 1 {
		t.Fatalf("second round should take the remainder: n=%d err=%v", rest, err)
	}
}
