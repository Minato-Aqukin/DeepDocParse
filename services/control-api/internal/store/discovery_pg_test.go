package store

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

func testNode(id string, public bool) discovery.Registration {
	return discovery.Registration{Descriptor: discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: id, Revision: 1, ValidUntil: time.Now().UTC().Add(time.Hour), DiscoveryCapabilities: discovery.DiscoveryCapabilities{EnumerateMembers: false}}, PublicKey: "test-public-key", VisibleToOrg: public}
}
func registerApproved(t *testing.T, s *Store, org string, r discovery.Registration) {
	t.Helper()
	if _, err := s.RegisterNode(context.Background(), org, r); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SetNodeState(context.Background(), org, r.Descriptor.NodeID, "approved"); err != nil {
		t.Fatal(err)
	}
}

func TestDiscoverySnapshotsFreezeVisibleMembershipAndRetainRevocations(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	registerApproved(t, s, org, testNode("node-b", true))
	registerApproved(t, s, org, testNode("node-a", true))
	registerApproved(t, s, org, testNode("hidden-node", false))
	snap, err := s.CreateMemberSnapshot(ctx, org, "alice", "scope-alice", "local-node", false, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	page, err := s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", snap.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Members) != 1 || page.Members[0].NodeID != "node-a" || page.Members[0].Health != "unknown" || page.Members[0].AcceptingAdmissions || page.Members[0].ExpansionState != "unexpanded_subtree" {
		t.Fatalf("incorrect first page %+v", page)
	}
	cursor := *page.NextCursor
	// A new approved node never enters an already frozen directory scope.
	registerApproved(t, s, org, testNode("node-c", true))
	if _, err = s.SetNodeState(ctx, org, "node-b", "revoked"); err != nil {
		t.Fatal(err)
	}
	page, err = s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", snap.ID, cursor, false)
	if err != nil {
		t.Fatal(err)
	}
	if page.RegistryRevision != snap.RegistryRevision || len(page.Members) != 1 || page.Members[0].NodeID != "node-b" || page.Members[0].State != "revoked" || page.Members[0].Descriptor != nil || page.Members[0].Route != nil {
		t.Fatalf("mixed revision or dropped revoked member %+v", page)
	}
	if page.NextCursor == nil || *page.NextCursor != snap.TerminalCursor {
		t.Fatal("missing stable terminal cursor")
	}
	for range 2 {
		end, err := s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", snap.ID, snap.TerminalCursor, false)
		if err != nil || !end.Complete || end.NextCursor != nil || len(end.Members) != 0 {
			t.Fatalf("terminal cursor not stable: %+v %v", end, err)
		}
	}
	fresh, err := s.CreateMemberSnapshot(ctx, org, "alice", "scope-alice", "local-node", false, 100, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	freshPage, err := s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", fresh.ID, "", false)
	if err != nil {
		t.Fatal(err)
	}
	if len(freshPage.Members) != 2 || freshPage.Members[0].NodeID != "node-a" || freshPage.Members[1].NodeID != "node-c" || fresh.RegistryRevision <= snap.RegistryRevision {
		t.Fatalf("new scope incorrect %+v", freshPage)
	}
	// Hidden members cannot be counted by paging another caller's/admin snapshot.
	if _, err = s.MemberSnapshotPage(ctx, org, "bob", "scope-bob", snap.ID, "", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross subject: %v", err)
	}
	if _, err = s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", fresh.ID, snap.FirstCursor, false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross snapshot cursor: %v", err)
	}
	if _, err = s.MemberSnapshotPage(ctx, "other-org", "alice", "scope-alice", snap.ID, "", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross org: %v", err)
	}
	_, err = s.pool.Exec(ctx, `UPDATE control.member_snapshots SET expires_at=now()-interval '1 second' WHERE id=$1`, snap.ID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.MemberSnapshotPage(ctx, org, "alice", "scope-alice", snap.ID, snap.FirstCursor, false); !errors.Is(err, ErrSnapshotExpired) {
		t.Fatalf("expired snapshot: %v", err)
	}
	if _, err = s.MemberSnapshotPage(ctx, org, "bob", "scope-bob", snap.ID, "", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("expiry reveals another caller's scope: %v", err)
	}
}
func TestDiscoveryStateRevisionRollbackAndEmptySnapshot(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	registration := testNode("node-hidden", false)
	registerApproved(t, s, org, registration)
	snap, err := s.CreateMemberSnapshot(ctx, org, "viewer", "viewer-scope", "local-node", false, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if snap.FirstCursor != snap.TerminalCursor {
		t.Fatal("private directory revealed page count")
	}
	page, err := s.MemberSnapshotPage(ctx, org, "viewer", "viewer-scope", snap.ID, "", false)
	if err != nil || !page.Complete || len(page.Members) != 0 {
		t.Fatalf("empty authorized scope: %+v %v", page, err)
	}
	if _, err = s.RegisterNode(ctx, org, registration); !errors.Is(err, ErrDiscoveryConflict) {
		t.Fatalf("old revision applied: %v", err)
	}
	unchanged, err := s.CreateMemberSnapshot(ctx, org, "viewer", "viewer-scope", "local-node", false, 1, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if unchanged.RegistryRevision != snap.RegistryRevision {
		t.Fatal("failed transaction changed revision")
	}
	if _, err = s.SetNodeState(ctx, org, "node-hidden", "revoked"); err != nil {
		t.Fatal(err)
	}
	registration.Descriptor.Revision = 2
	if _, err = s.RegisterNode(ctx, org, registration); !errors.Is(err, ErrDiscoveryConflict) {
		t.Fatalf("revoked identity resurrected: %v", err)
	}
	if _, err = s.SetNodeState(ctx, org, "node-hidden", "approved"); !errors.Is(err, ErrDiscoveryConflict) {
		t.Fatalf("revoked approval resurrected: %v", err)
	}
}

func TestDiscoveryScopedRevisionDoesNotCountHiddenChanges(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	hidden := testNode("node-hidden", false)
	registerApproved(t, s, org, hidden)
	visible := testNode("node-visible", true)
	registerApproved(t, s, org, visible)
	create := func(subject, scope string, admin bool) *discovery.Snapshot {
		t.Helper()
		snap, err := s.CreateMemberSnapshot(ctx, org, subject, scope, "local-node", admin, 100, time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		return snap
	}
	page := func(snap *discovery.Snapshot) *discovery.Page {
		t.Helper()
		p, err := s.MemberSnapshotPage(ctx, org, "alice", "alice-scope", snap.ID, "", false)
		if err != nil {
			t.Fatal(err)
		}
		return p
	}
	initial := create("alice", "alice-scope", false)
	if initial.RegistryRevision != 1 || len(page(initial).Members) != 1 || page(initial).Members[0].Revision != 2 {
		t.Fatal("initial snapshot or visible member exposes the organization's prior hidden mutations")
	}
	admin := create("admin", "admin-scope", true)
	assertHidden := func() {
		t.Helper()
		current := create("alice", "alice-scope", false)
		p := page(current)
		if current.RegistryRevision != initial.RegistryRevision || len(p.Members) != 1 || p.Members[0].Revision != 2 {
			t.Fatalf("hidden mutation changed a visible revision: %+v", p)
		}
	}
	// Pending, approved, descriptor changes and revocation are all hidden from Alice.
	hidden.Descriptor.Revision = 2
	if _, err := s.RegisterNode(ctx, org, hidden); err != nil {
		t.Fatal(err)
	}
	assertHidden()
	if _, err := s.SetNodeState(ctx, org, hidden.Descriptor.NodeID, discovery.MemberApproved); err != nil {
		t.Fatal(err)
	}
	assertHidden()
	if observed := create("admin", "admin-scope", true); observed.RegistryRevision != admin.RegistryRevision+1 {
		t.Fatal("admin's visible descriptor change did not advance its own scoped revision")
	}
	if _, err := s.SetNodeState(ctx, org, hidden.Descriptor.NodeID, discovery.MemberRevoked); err != nil {
		t.Fatal(err)
	}
	assertHidden()
	registerApproved(t, s, org, testNode("node-hidden-new", false))
	assertHidden()
	// One newly observed visible view advances once, regardless of intervening writes.
	visible.Descriptor.Revision = 2
	registerApproved(t, s, org, visible)
	changed := create("alice", "alice-scope", false)
	if changed.RegistryRevision != initial.RegistryRevision+1 || page(changed).Members[0].Revision != 4 {
		t.Fatal("visible mutation failed to advance scoped/per-member revisions independently")
	}
	if other := create("bob", "bob-scope", false); other.RegistryRevision != 1 {
		t.Fatal("new caller inherited another scope's revision history")
	}
	// Pagination settings, elapsed time, and a new process cannot change a view revision.
	restarted := &Store{pool: testPool(t)}
	unchanged, err := restarted.CreateMemberSnapshot(ctx, org, "alice", "alice-scope", "local-node", false, 1, 2*time.Minute)
	if err != nil || unchanged.RegistryRevision != changed.RegistryRevision {
		t.Fatalf("identical view changed across restart/page size/TTL: %+v %v", unchanged, err)
	}
}

func TestDiscoveryConcurrentSnapshotsShareScopedRevisionDuringHiddenWrites(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	registerApproved(t, s, org, testNode("node-visible", true))
	const readers = 12
	var group sync.WaitGroup
	start := make(chan struct{})
	results := make(chan error, readers+1)
	for range readers {
		group.Add(1)
		go func() {
			defer group.Done()
			<-start
			snap, err := s.CreateMemberSnapshot(ctx, org, "alice", "scope", "local-node", false, 1, time.Minute)
			if err == nil && snap.RegistryRevision != 1 {
				err = fmt.Errorf("unchanged visible set got revision %d", snap.RegistryRevision)
			}
			results <- err
		}()
	}
	group.Add(1)
	go func() {
		defer group.Done()
		<-start
		for n := range 8 {
			node := testNode(fmt.Sprintf("hidden-%d", n), false)
			if _, err := s.RegisterNode(ctx, org, node); err != nil {
				results <- err
				return
			}
			if _, err := s.SetNodeState(ctx, org, node.Descriptor.NodeID, discovery.MemberApproved); err != nil {
				results <- err
				return
			}
		}
		results <- nil
	}()
	close(start)
	group.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
	var count, revision int
	if err := s.pool.QueryRow(ctx, `SELECT count(*),max(revision) FROM control.node_directory_views WHERE organization_id=$1 AND caller_scope_hash='scope'`, org).Scan(&count, &revision); err != nil || count != 1 || revision != 1 {
		t.Fatalf("scoped revision not persisted atomically: count=%d revision=%d err=%v", count, revision, err)
	}
}
func TestDiscoverySnapshotSurvivesPoolRestartAndConcurrentDirectoryChange(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	org := seedOrg(t, s)
	registerApproved(t, s, org, testNode("node-a", true))
	var group sync.WaitGroup
	group.Add(2)
	var snapshot *discovery.Snapshot
	var snapshotErr, writeErr error
	go func() {
		defer group.Done()
		snapshot, snapshotErr = s.CreateMemberSnapshot(ctx, org, "alice", "scope", "local-node", false, 1, time.Minute)
	}()
	go func() {
		defer group.Done()
		_, writeErr = s.RegisterNode(ctx, org, testNode("node-b", true))
		if writeErr == nil {
			_, writeErr = s.SetNodeState(ctx, org, "node-b", "approved")
		}
	}()
	group.Wait()
	if snapshotErr != nil || writeErr != nil {
		t.Fatalf("concurrent operations: %v %v", snapshotErr, writeErr)
	}
	// A separate connection pool reads the same persisted pages, not a process cache.
	restarted := &Store{pool: testPool(t)}
	cursor := snapshot.FirstCursor
	count := 0
	for {
		p, err := restarted.MemberSnapshotPage(ctx, org, "alice", "scope", snapshot.ID, cursor, false)
		if err != nil {
			t.Fatal(err)
		}
		count += len(p.Members)
		if p.Complete {
			break
		}
		cursor = *p.NextCursor
		if count > 2 {
			t.Fatal("pagination did not terminate")
		}
	}
	if count != 1 && count != 2 {
		t.Fatalf("inconsistent frozen membership: %d", count)
	}
}
func TestPersistentNodeIdentityRejectsReplacementAndAdvancesDescriptor(t *testing.T) {
	s := &Store{pool: testPool(t)}
	ctx := context.Background()
	have, err := s.NodeIdentity(ctx)
	if err != nil && !errors.Is(err, ErrNotFound) {
		t.Fatal(err)
	}
	created := errors.Is(err, ErrNotFound)
	if created {
		have = &PublicNodeIdentity{NodeID: "test-local-node", PublicKey: "public-only", Fingerprint: "test-fingerprint"}
		t.Cleanup(func() {
			s.pool.Exec(context.Background(), `DELETE FROM control.node_identity WHERE node_id=$1`, have.NodeID)
		})
	}
	if err = s.BindNodeIdentity(ctx, *have); err != nil {
		t.Fatal(err)
	}
	if err = s.BindNodeIdentity(ctx, *have); err != nil {
		t.Fatal(err)
	}
	replacement := *have
	replacement.PublicKey = "different"
	if err = s.BindNodeIdentity(ctx, replacement); !errors.Is(err, ErrNodeIdentityMismatch) {
		t.Fatalf("identity changed: %v", err)
	}
	rev, err := s.NodeDescriptorRevision(ctx, "endpoint-a")
	if err != nil {
		t.Fatal(err)
	}
	same, err := s.NodeDescriptorRevision(ctx, "endpoint-a")
	if err != nil || same != rev {
		t.Fatal("restart changed descriptor revision")
	}
	next, err := s.NodeDescriptorRevision(ctx, "endpoint-b")
	if err != nil || next != rev+1 {
		t.Fatal("endpoint change did not increment descriptor revision")
	}
	current, err := s.NodeIdentity(ctx)
	if err != nil || current.NodeID != have.NodeID {
		t.Fatal("endpoint changed authority identity")
	}
}

func TestDiscoveryGovernanceTablesDenyCorpusDatabaseRole(t *testing.T) {
	s := &Store{pool: testPool(t)}
	for _, table := range []string{"node_identity", "node_directories", "node_directory_views", "node_members", "member_snapshots", "member_snapshot_pages"} {
		var controlWrites, corpusReads, corpusWrites bool
		err := s.pool.QueryRow(context.Background(), `SELECT has_table_privilege('ddp_control',$1,'INSERT,UPDATE,DELETE'),has_table_privilege('ddp_corpus',$1,'SELECT'),has_table_privilege('ddp_corpus',$1,'INSERT,UPDATE,DELETE')`, "control."+table).Scan(&controlWrites, &corpusReads, &corpusWrites)
		if err != nil {
			t.Fatal(err)
		}
		if !controlWrites || corpusReads || corpusWrites {
			t.Fatalf("governance boundary broken for %s: controlWrites=%v corpusReads=%v corpusWrites=%v", table, controlWrites, corpusReads, corpusWrites)
		}
	}
}
