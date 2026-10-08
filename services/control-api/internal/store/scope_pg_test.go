package store

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/migrate"
)

func scopeFixture(t *testing.T) (*Store, string, discovery.ScopeOptions) {
	t.Helper()
	s := &Store{pool: testPool(t)}
	if _, err := migrate.Up(context.Background(), s.pool); err != nil {
		t.Fatal(err)
	}
	org := seedOrg(t, s)
	snap, err := s.CreateMemberSnapshot(context.Background(), org, "alice", "scope-alice", "local-node", false, 1, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	return s, org, discovery.ScopeOptions{Operation: "search", MemberSnapshotID: snap.ID, PageSize: 1, MaxMembers: 100, TTLSeconds: 3600}
}
func testScopeCatalog(id string) discovery.CollectionCatalog {
	return discovery.CollectionCatalog{SnapshotID: "catalog-one", ScopeID: id, CallerScopeHash: "scope-alice", NodeID: "local-node", Revision: 7, FetchedAt: time.Now().UTC(), ValidUntil: time.Now().UTC().Add(time.Hour), TerminalCursor: "catalog-terminal", Collections: []string{"collection-b", "collection-a", "collection-a"}, Complete: true}
}
func TestScopePGFrozenDeduplicatedPagesIsolationAndDurableRevocation(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	id := auth.NewID()
	m, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, testScopeCatalog(id))
	if err != nil {
		t.Fatal(err)
	}
	if m.Manifest.EnumerationState != "sealed" || m.TotalTargets != 2 || m.Manifest.ExpandedMembers[0].CollectionID != "collection-a" || len(m.Manifest.RegistryRevisionVector) != 2 {
		t.Fatalf("bad frozen scope %+v", m)
	}
	if _, err = s.ScopeManifest(ctx, org, "scope-bob", id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross caller scope: %v", err)
	}
	if _, err = s.ScopeManifest(ctx, "different-org", "scope-alice", id); !errors.Is(err, ErrNotFound) {
		t.Fatalf("cross org scope: %v", err)
	}
	first, err := s.ScopeTargets(ctx, org, "alice", "scope-alice", id, "", "local-node", false)
	if err != nil {
		t.Fatal(err)
	}
	if first.Complete || len(first.Targets) != 1 || first.NextCursor == nil {
		t.Fatalf("single data page claimed complete %+v", first)
	}
	second, err := s.ScopeTargets(ctx, org, "alice", "scope-alice", id, *first.NextCursor, "local-node", false)
	if err != nil {
		t.Fatal(err)
	}
	if second.Complete || second.NextCursor == nil || *second.NextCursor != m.TerminalCursor {
		t.Fatal("no explicit terminal cursor")
	}
	terminal, err := s.ScopeTargets(ctx, org, "alice", "scope-alice", id, m.TerminalCursor, "local-node", false)
	if err != nil || !terminal.Complete || len(terminal.Targets) != 0 || terminal.NextCursor != nil {
		t.Fatalf("invalid terminal %+v %v", terminal, err)
	}
	if _, err = s.ScopeTargets(ctx, org, "alice", "scope-alice", id, "arbitrary-cursor", "local-node", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("accepted foreign cursor: %v", err)
	}
	registerApproved(t, s, org, testNode("added-after-freeze", true))
	// New process/store object sees the same persisted pages, without enumeration.
	restarted := &Store{pool: testPool(t)}
	again, err := restarted.ScopeManifest(ctx, org, "scope-alice", id)
	if err != nil || again.TotalTargets != 2 || again.Manifest.ManifestDigest != m.Manifest.ManifestDigest {
		t.Fatal("restart or node addition mutated scope")
	}
	if err = s.RevokeScopeCatalog(ctx, org, "scope-alice", id); err != nil {
		t.Fatal(err)
	}
	page, err := restarted.ScopeTargets(ctx, org, "alice", "scope-alice", id, "", "local-node", false)
	if err != nil || page.TotalTargets != 2 || page.Targets[0].State != "revoked" || page.ManifestDigest != m.Manifest.ManifestDigest {
		t.Fatalf("revocation erased denominator %+v %v", page, err)
	}
	if _, err = s.pool.Exec(ctx, `UPDATE control.scope_manifests SET valid_until=now()-interval '1 second' WHERE id=$1`, id); err != nil {
		t.Fatal(err)
	}
	expired, err := s.ScopeManifest(ctx, org, "scope-alice", id)
	if err != nil || !expired.Expired || expired.EffectiveEnumerationState != "expired" || expired.Manifest.ManifestDigest != m.Manifest.ManifestDigest {
		t.Fatalf("history lost at expiry %+v %v", expired, err)
	}
	page, err = restarted.ScopeTargets(ctx, org, "alice", "scope-alice", id, "", "local-node", false)
	if err != nil || page.Targets[0].State != "revoked" {
		t.Fatal("expiry forgot known revocation")
	}
}

func TestScopePGUnknownEmptyPartialBudgetAndSnapshotLoss(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	for _, tc := range []struct {
		name    string
		modify  func(*discovery.CollectionCatalog)
		want    string
		targets int
	}{
		{"verified_empty", func(c *discovery.CollectionCatalog) { c.Collections = []string{} }, "sealed", 0},
		{"missing_catalog", func(c *discovery.CollectionCatalog) { *c = discovery.CollectionCatalog{} }, "partial", 0},
		{"single_page", func(c *discovery.CollectionCatalog) { c.Complete = false }, "partial", 2},
		{"expired_catalog", func(c *discovery.CollectionCatalog) { c.ValidUntil = time.Now().Add(-time.Second) }, "partial", 2},
		{"wrong_caller", func(c *discovery.CollectionCatalog) { c.CallerScopeHash = "scope-bob" }, "partial", 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			id := auth.NewID()
			c := testScopeCatalog(id)
			tc.modify(&c)
			out, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, c)
			if err != nil {
				t.Fatal(err)
			}
			if out.Manifest.EnumerationState != tc.want || out.TotalTargets != tc.targets {
				t.Fatalf("wrong completeness %+v", out)
			}
		})
	}
	id := auth.NewID()
	small := opts
	small.MaxMembers = 1
	limited, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, small, testScopeCatalog(id))
	if err != nil || limited.Manifest.EnumerationState != "partial" || limited.TotalTargets != 1 || limited.Manifest.UnexpandedSubtrees[0].Reason != "budget_exhausted" {
		t.Fatalf("budget hidden %+v %v", limited, err)
	}
	// A missing terminal cannot be mistaken for the complete empty member set.
	if _, err = s.pool.Exec(ctx, `DELETE FROM control.member_snapshot_pages WHERE snapshot_id=$1`, opts.MemberSnapshotID); err != nil {
		t.Fatal(err)
	}
	id = auth.NewID()
	broken, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, testScopeCatalog(id))
	if err != nil || broken.Manifest.EnumerationState != "partial" || broken.TotalTargets != 2 {
		t.Fatalf("lost page fabricated sealed scope %+v %v", broken, err)
	}
}

func TestScopePGDirectMembersAreUnknownNotInventedCollections(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	registerApproved(t, s, org, testNode("remote-leaf", true))
	r := testNode("remote-directory", true)
	r.Descriptor.DiscoveryCapabilities.EnumerateMembers = true
	registerApproved(t, s, org, r)
	registerApproved(t, s, org, testNode("private-node", false))
	snap, err := s.CreateMemberSnapshot(ctx, org, "alice", "scope-alice", "local-node", false, 1, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	opts.MemberSnapshotID = snap.ID
	id := auth.NewID()
	out, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, testScopeCatalog(id))
	if err != nil {
		t.Fatal(err)
	}
	if out.Manifest.EnumerationState != "partial" || len(out.Manifest.UnexpandedSubtrees) != 2 || out.TotalTargets != 2 {
		t.Fatalf("unknown node discarded or private node leaked %+v", out)
	}
	for _, u := range out.Manifest.UnexpandedSubtrees {
		if u.NodeID == "private-node" {
			t.Fatal("private topology leaked")
		}
	}
}

func TestScopePGRevocationPairsOriginAndCollection(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	registerApproved(t, s, org, testNode("remote-x", true))
	id := auth.NewID()
	remote := discovery.RemoteExpansion{
		Targets: []discovery.TargetKey{{OriginNodeID: "remote-x", CollectionID: "docs", Operation: "search"}},
	}
	if _, err := s.CreateExpandedScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, testScopeCatalog(id), remote); err != nil {
		t.Fatal(err)
	}
	if err := s.RevokeScopeCollections(ctx, org, "scope-alice", id, "local-node", []string{"docs"}); err != nil {
		t.Fatal(err)
	}
	collect := func() map[string]string {
		t.Helper()
		states := map[string]string{}
		cursor := ""
		for {
			page, err := s.ScopeTargets(ctx, org, "alice", "scope-alice", id, cursor, "local-node", false)
			if err != nil {
				t.Fatal(err)
			}
			for _, target := range page.Targets {
				states[target.TargetKey.OriginNodeID+"\x00"+target.TargetKey.CollectionID] = target.State
			}
			if page.Complete || page.NextCursor == nil {
				break
			}
			cursor = *page.NextCursor
		}
		return states
	}
	paired := collect()
	if len(paired) != 3 {
		t.Fatalf("expected 2 local + 1 remote targets, got %+v", paired)
	}
	for key, state := range paired {
		want := "not_attempted"
		if strings.HasPrefix(key, "local-node\x00") {
			// The named ("docs") collection is not in this origin's catalog,
			// so nothing is revoked — but the origin's snapshot is still
			// invalid, and its unrevoked targets read unreachable.
			want = "unreachable"
		}
		if state != want {
			t.Fatalf("unpaired revocation leaked: %s=%s want %s", key, state, want)
		}
	}
	if err := s.RevokeScopeCollections(ctx, org, "scope-alice", id, "local-node", []string{"collection-a"}); err != nil {
		t.Fatal(err)
	}
	after := collect()
	if after["local-node\x00collection-a"] != "revoked" {
		t.Fatalf("paired local target not revoked: %+v", after)
	}
	if after["local-node\x00collection-b"] != "unreachable" || after["remote-x\x00docs"] != "not_attempted" {
		t.Fatalf("revocation crossed its origin pair: %+v", after)
	}
	if err := s.RevokeScopeCollections(ctx, org, "scope-alice", id, "local-node", nil); err != nil {
		t.Fatal(err)
	}
	cleared := collect()
	for key, state := range cleared {
		want := "revoked"
		if key == "remote-x\x00docs" {
			want = "not_attempted"
		}
		if state != want {
			t.Fatalf("nil-IDs revoke crossed origins: %s=%s", key, state)
		}
	}
	if err := s.RevokeScopeCollections(ctx, org, "scope-alice", id, "", []string{"docs"}); !errors.Is(err, ErrDiscoveryConflict) {
		t.Fatalf("empty origin must fail loud, got %v", err)
	}
}

func TestScopePGUnboundCatalogLeavesRemoteTargetsAttemptable(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	registerApproved(t, s, org, testNode("remote-x", true))
	id := auth.NewID()
	remote := discovery.RemoteExpansion{
		Targets: []discovery.TargetKey{{OriginNodeID: "remote-x", CollectionID: "docs", Operation: "search"}},
	}
	if _, err := s.CreateExpandedScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, discovery.CollectionCatalog{}, remote); err != nil {
		t.Fatal(err)
	}
	cursor := ""
	seenRemote := false
	for {
		page, err := s.ScopeTargets(ctx, org, "alice", "scope-alice", id, cursor, "local-node", false)
		if err != nil {
			t.Fatal(err)
		}
		for _, target := range page.Targets {
			if target.TargetKey.OriginNodeID == "remote-x" {
				seenRemote = true
				if target.State != "not_attempted" {
					t.Fatalf("unbound catalog poisoned remote target: %+v", target)
				}
			}
		}
		if page.Complete || page.NextCursor == nil {
			break
		}
		cursor = *page.NextCursor
	}
	if !seenRemote {
		t.Fatal("remote target missing from frozen scope")
	}
}

func TestScopePGBoundaryExcludedMembersReportDeniedPartial(t *testing.T) {
	s, org, opts := scopeFixture(t)
	ctx := context.Background()
	registerApproved(t, s, org, testNode("excluded-leaf", true))
	snap, err := s.CreateMemberSnapshot(ctx, org, "alice", "scope-alice", "local-node", false, 100, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	opts.MemberSnapshotID = snap.ID
	opts.AllowedNodeIDs = []string{"local-node"}
	id := auth.NewID()
	out, err := s.CreateScope(ctx, org, "alice", "scope-alice", "local-node", id, false, opts, testScopeCatalog(id))
	if err != nil {
		t.Fatal(err)
	}
	if out.Manifest.EnumerationState != "partial" {
		t.Fatalf("narrowed scope claimed sealed: %+v", out.Manifest)
	}
	found := false
	for _, unknown := range out.Manifest.UnexpandedSubtrees {
		if unknown.NodeID == "excluded-leaf" && unknown.Reason == "denied" {
			found = true
		}
	}
	if !found {
		t.Fatalf("boundary exclusion not recorded as denied: %+v", out.Manifest.UnexpandedSubtrees)
	}
}
