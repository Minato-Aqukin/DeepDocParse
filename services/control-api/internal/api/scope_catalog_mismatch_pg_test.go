package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

// Ported from the independent P4 review's counterexample
// TestIndependentRejectCatalogCountMismatch. A hostile/older producer keeps
// caller, scope, snapshot, revision, timestamps and cursor consistent, so only
// the declared total exposes the truncation. This goes through the real store
// and needs migrated PostgreSQL: it skips loudly without
// CONTROL_TEST_DATABASE_URL (discoveryPGFixture). The producer-only rules are
// covered without PostgreSQL by TestCollectScopeCatalogRejectsProducerCountMismatch.
func TestScopeHTTPRejectsInconsistentCatalogTotals(t *testing.T) {
	for _, scenario := range []string{"empty_data", "omitted_collection", "changing_total", "missing_total"} {
		t.Run(scenario, func(t *testing.T) {
			f := discoveryPGFixture(t)
			created := time.Now().UTC()
			valid := created.Add(time.Minute)
			producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				terminal := r.URL.Query().Get("cursor") == "terminal"
				collections := []map[string]string{}
				var next *string
				total := 2
				if !terminal {
					v := "terminal"
					next = &v
					if scenario != "empty_data" {
						collections = append(collections, map[string]string{"collection_id": "only-observed", "origin_node_id": f.server.nodeIdentity.NodeID()})
					}
				}
				if scenario == "changing_total" {
					total = 1
					if terminal {
						total = 2
					}
				}
				body := map[string]any{"snapshot_id": "stable", "scope_id": r.URL.Query().Get("scope_id"), "caller_scope_hash": r.Header.Get(identity.HeaderCallerScope), "origin_node_id": f.server.nodeIdentity.NodeID(), "registry_revision": 1, "created_at": created, "valid_until": valid, "first_cursor": "first", "terminal_cursor": "terminal", "collections": collections, "next_cursor": next, "complete": terminal}
				if scenario != "missing_total" {
					body["total"] = total
				}
				_ = json.NewEncoder(w).Encode(body)
			}))
			defer producer.Close()
			f.server.cfg.CorpusURL = producer.URL
			f.server.corpus, _ = proxy.New("corpus", producer.URL, f.server.cfg.ServiceToken)
			out := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search"}), 201)
			if out.Manifest.EnumerationState != "partial" {
				t.Errorf("producer total contradicts its observations, yet state=%s targets=%d", out.Manifest.EnumerationState, out.TotalTargets)
			}
			if len(out.Manifest.UnexpandedSubtrees) == 0 {
				t.Fatal("truncated catalog left no unknown subtree")
			}
			want := 1
			if scenario == "empty_data" || scenario == "missing_total" {
				want = 0
			}
			if out.TotalTargets != want {
				t.Errorf("consistently observed targets %d, want %d", out.TotalTargets, want)
			}
		})
	}
}
