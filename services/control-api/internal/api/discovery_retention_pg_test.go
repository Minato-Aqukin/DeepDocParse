package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// Exercises the existing production housekeeping seam, so HEAD fails on retained
// rows rather than merely failing to compile a not-yet-existing sweep method.
func TestDiscoveryRetentionSweepsOldScopesButKeepsLiveAndRecentlyExpired(t *testing.T) {
	f := discoveryPGFixture(t)
	t.Setenv("ALLOW_INSECURE_DEFAULTS", "true")
	t.Setenv("DISCOVERY_METADATA_RETENTION_SECONDS", "86400")
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	f.server.cfg = cfg
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	node := registration.Descriptor.NodeID
	peer := newFakePeerServer(node, nil, []discovery.CollectionRef{{CollectionID: "retention-col", OriginNodeID: node}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{node: peer.serve(t)})
	old := createScope(t, f, map[string]any{"operation": "corpus.retrieve"})
	recent := createScope(t, f, map[string]any{"operation": "corpus.retrieve"})
	live := createScope(t, f, map[string]any{"operation": "corpus.retrieve"})
	ctx := context.Background()
	pool := f.server.store.Pool()
	for _, item := range []struct {
		id  string
		age time.Duration
	}{{old.Manifest.ScopeID, 48 * time.Hour}, {recent.Manifest.ScopeID, time.Second}} {
		if _, err := pool.Exec(ctx, `UPDATE control.member_snapshots SET expires_at=now()-$2*interval '1 second' WHERE id=(SELECT member_snapshot_id FROM control.scope_manifests WHERE id=$1)`, item.id, item.age.Seconds()); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(ctx, `UPDATE control.scope_manifests SET valid_until=now()-$2*interval '1 second' WHERE id=$1`, item.id, item.age.Seconds()); err != nil {
			t.Fatal(err)
		}
	}
	oldSubtree := "subtree-" + old.Manifest.ScopeID
	if _, err := pool.Exec(ctx, `INSERT INTO control.subtree_snapshots(id,organization_id,issuer_node_id,request_binding,page_size,created_at,expires_at,metadata) VALUES($1,$2,$3,'retention-test',50,now()-interval '48 hours',now()-interval '48 hours','{}')`, oldSubtree, f.org, node); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `INSERT INTO control.subtree_snapshot_pages(snapshot_id,cursor,targets,next_cursor) VALUES($1,'only','[]',NULL)`, oldSubtree); err != nil {
		t.Fatal(err)
	}
	var oldSnapshot string
	if err := pool.QueryRow(ctx, `SELECT member_snapshot_id FROM control.scope_manifests WHERE id=$1`, old.Manifest.ScopeID).Scan(&oldSnapshot); err != nil {
		t.Fatal(err)
	}
	before := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+old.Manifest.ScopeID, f.aliceToken, nil), 200)
	if !before.Expired || before.TotalTargets != 1 {
		t.Fatal("expired scope must honestly retain its frozen denominator until purge")
	}
	// No peer traffic is needed to exercise metadata maintenance.
	if _, err := pool.Exec(ctx, `UPDATE control.node_members SET renewal_next_attempt_at=now()+interval '1 day' WHERE organization_id=$1`, f.org); err != nil {
		t.Fatal(err)
	}
	f.server.renewDiscoveryBatch(ctx, &http.Client{}, time.Minute)
	for _, item := range []struct {
		table, column, id string
	}{{"scope_manifests", "id", old.Manifest.ScopeID}, {"scope_target_pages", "scope_id", old.Manifest.ScopeID}, {"scope_remote_sources", "scope_id", old.Manifest.ScopeID}, {"scope_catalog_sources", "scope_id", old.Manifest.ScopeID}, {"scope_catalog_revocations", "scope_id", old.Manifest.ScopeID}, {"member_snapshots", "id", oldSnapshot}, {"member_snapshot_pages", "snapshot_id", oldSnapshot}, {"subtree_snapshots", "id", oldSubtree}, {"subtree_snapshot_pages", "snapshot_id", oldSubtree}} {
		var count int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM control.`+item.table+` WHERE `+item.column+`=$1`, item.id).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 0 {
			t.Fatalf("retention sweep left %d old rows in %s", count, item.table)
		}
	}
	if w := requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+old.Manifest.ScopeID, f.aliceToken, nil); w.Code != 404 {
		t.Fatalf("purged scope must be not-found, not a fabricated empty denominator: %d %s", w.Code, w.Body.String())
	}
	for _, id := range []string{recent.Manifest.ScopeID, live.Manifest.ScopeID} {
		out := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+id, f.aliceToken, nil), 200)
		if out.TotalTargets != 1 || (id == recent.Manifest.ScopeID && !out.Expired) || (id == live.Manifest.ScopeID && out.Expired) {
			t.Fatalf("maintenance changed live/recent scope visibility: %+v", out)
		}
	}
}
