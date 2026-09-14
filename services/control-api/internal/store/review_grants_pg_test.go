package store

import (
	"context"
	"testing"
)

// Independent acceptance: requesting a fresh stable capability after expiry
// cannot report success with a token that redemption immediately rejects.
func TestReviewExpiredStableGrantCanBeIssuedAgain(t *testing.T) {
	s := &Store{pool: testPool(t)}
	org := seedOrg(t, s)
	ctx := context.Background()
	grant, err := s.StableGrantFor(ctx, org, "doc", "alice", "resource", "object", "application/pdf", "manual.pdf")
	if err != nil {
		t.Fatal(err)
	}
	_, err = s.pool.Exec(ctx, "UPDATE control.file_grants SET expires_at=now()-interval '1 second' WHERE token=$1", grant.Token)
	if err != nil {
		t.Fatal(err)
	}
	fresh, err := s.StableGrantFor(ctx, org, "doc", "alice", "resource", "object", "application/pdf", "manual.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.FileGrantByToken(ctx, fresh.Token); err != nil {
		t.Fatalf("issuer returned an already unusable grant: %v", err)
	}
}
