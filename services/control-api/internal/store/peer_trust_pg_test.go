package store

import (
	"context"
	"errors"
	"testing"
)

// TestPeerTrustReadsTheApprovalStateAndNeverInventsMembers pins the one query
// node-credential verification trusts: the registered key and the current
// approval state of a direct member, scoped to one organization. Needs a real
// PostgreSQL (CONTROL_TEST_DATABASE_URL); skips otherwise.
func TestPeerTrustReadsTheApprovalStateAndNeverInventsMembers(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	other := seedOrg(t, s)
	reg := testNode("node-trust-a", true)
	if _, err := s.RegisterNode(ctx, org, reg); err != nil {
		t.Fatal(err)
	}
	pending, err := s.PeerTrust(ctx, org, "node-trust-a")
	if err != nil || pending.State != "pending" || pending.PublicKey != reg.PublicKey || pending.Revision < 1 {
		t.Fatalf("pending member must be returned with its state: %+v %v", pending, err)
	}
	if _, err := s.SetNodeState(ctx, org, "node-trust-a", "approved"); err != nil {
		t.Fatal(err)
	}
	approved, err := s.PeerTrust(ctx, org, "node-trust-a")
	if err != nil || approved.State != "approved" {
		t.Fatalf("approval not visible: %+v %v", approved, err)
	}
	// Another organization's directory does not know this member.
	if _, err := s.PeerTrust(ctx, other, "node-trust-a"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("trust leaked across organizations: %v", err)
	}
	if _, err := s.PeerTrust(ctx, org, "node-never-registered"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown member must be ErrNotFound: %v", err)
	}
	if _, err := s.SetNodeState(ctx, org, "node-trust-a", "revoked"); err != nil {
		t.Fatal(err)
	}
	revoked, err := s.PeerTrust(ctx, org, "node-trust-a")
	if err != nil || revoked.State != "revoked" || revoked.Revision <= approved.Revision {
		t.Fatalf("revocation must be immediate and advance the revision: %+v %v", revoked, err)
	}
}
