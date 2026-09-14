package store

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// Ported from the independent P4 review's counterexample
// TestIndependentSnapshotPermissionEpochDoesNotRevive, plus the "nobody read
// during the private interval" variant the review asked for.
//
// The binding lives entirely in SQL: a frozen member page stores the member's
// authorization epoch (node_members.public_revision) and every read compares
// it with the current row, so withdraw-then-regrant cannot revive an old
// snapshot even when no read happened while it was private. Requires migrated
// PostgreSQL; testPool skips loudly without CONTROL_TEST_DATABASE_URL.
func TestSnapshotAuthorizationEpochDoesNotRevive(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	member := testNode("visibility-epoch", true)
	registerApproved(t, s, org, member)
	snap, err := s.CreateMemberSnapshot(ctx, org, "alice", "alice-scope", "local-node", false, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	frozen, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", snap.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if len(frozen.Members) != 1 || frozen.Members[0].State != discovery.MemberApproved || frozen.Members[0].Descriptor == nil {
		t.Fatalf("fixture snapshot not approved %+v", frozen)
	}

	registerAndApprove := func(descriptorRevision int64, visible bool) {
		t.Helper()
		member.Descriptor.Revision = descriptorRevision
		member.VisibleToOrg = visible
		if _, err := s.RegisterNode(ctx, org, member); err != nil {
			t.Fatal(err)
		}
		if _, err := s.SetNodeState(ctx, org, member.Descriptor.NodeID, discovery.MemberApproved); err != nil {
			t.Fatal(err)
		}
	}
	// Step 1: withdraw visibility, then grant it back. Nobody reads the frozen
	// snapshot during the private interval; the old generation must stay dead.
	registerAndApprove(2, false)
	registerAndApprove(3, true)
	revived, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", snap.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	old := revived.Members[0]
	if old.State != discovery.MemberRevoked || old.Descriptor != nil || old.Route != nil || old.Configured || old.ExpansionState != discovery.ExpansionRevoked {
		t.Fatalf("old snapshot revived after withdraw/regrant: state=%s descriptor=%v expansion=%s", old.State, old.Descriptor, old.ExpansionState)
	}

	// Step 2: new authorization is expressed only through a NEW snapshot.
	fresh, err := s.CreateMemberSnapshot(ctx, org, "alice", "alice-scope", "local-node", false, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	freshPage, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", fresh.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if len(freshPage.Members) != 1 || freshPage.Members[0].State != discovery.MemberApproved || freshPage.Members[0].Descriptor == nil || freshPage.Members[0].Descriptor.Revision != 3 {
		t.Fatalf("new snapshot did not carry the new authorization %+v", freshPage)
	}

	// Step 3: even a plain descriptor re-registration invalidates older
	// snapshots; the generation, not only visibility, is the binding.
	registerAndApprove(4, true)
	after, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", fresh.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if after.Members[0].State != discovery.MemberRevoked || after.Members[0].Descriptor != nil {
		t.Fatalf("descriptor re-registration revived an old snapshot %+v", after.Members[0])
	}

	// Step 4: the explicit permanent revocation path still refuses to revive.
	if _, err = s.SetNodeState(ctx, org, member.Descriptor.NodeID, discovery.MemberRevoked); err != nil {
		t.Fatal(err)
	}
	permanent, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", snap.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if permanent.Members[0].State != discovery.MemberRevoked || permanent.Members[0].Descriptor != nil {
		t.Fatalf("explicit revocation did not stick %+v", permanent.Members[0])
	}
	member.Descriptor.Revision = 5
	if _, err = s.RegisterNode(ctx, org, member); !errors.Is(err, ErrDiscoveryConflict) {
		t.Fatalf("revoked member resurrected by re-registration: %v", err)
	}
}
