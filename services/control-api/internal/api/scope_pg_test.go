package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func scopeCatalogFixture(t *testing.T, f *discoveryFixture, mode *atomic.Value) *atomic.Int32 {
	t.Helper()
	var calls atomic.Int32
	created := time.Now().UTC()
	valid := created.Add(time.Hour)
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/internal/federation/collections" || r.Header.Get("Authorization") != "Bearer internal-test-service" || r.Header.Get(identity.HeaderActor) != "control-api" || r.Header.Get(identity.HeaderActorKind) != "service" || r.Header.Get(identity.HeaderOrganization) != f.org || r.Header.Get(identity.HeaderCallerUser) != f.alice.ID || r.Header.Get(identity.HeaderCallerRole) != "contributor" || r.Header.Get(identity.HeaderAuthorityNode) != f.server.nodeIdentity.NodeID() || !strings.HasPrefix(r.Header.Get(identity.HeaderCallerScope), "sha256:") {
			t.Error("producer got wrong or client-forged authorization")
		}
		current := mode.Load().(string)
		if current == "redirect" {
			w.Header().Set("Location", "/must-not-follow")
			w.WriteHeader(302)
			return
		}
		if current == "revoked" || current == "expired" || current == "changed" {
			code, status := "catalog_snapshot_invalid", 410
			if current == "expired" {
				code = "catalog_snapshot_expired"
			}
			if current == "changed" {
				code = "catalog_snapshot_changed"
				status = 409
			}
			w.WriteHeader(status)
			json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code}, "revoked_collection_ids": []string{"collection-a"}})
			return
		}
		cursor := r.URL.Query().Get("cursor")
		revision := 7
		collections := []map[string]string{}
		var next *string
		complete := false
		if cursor == "" {
			collections = []map[string]string{{"collection_id": "collection-b", "origin_node_id": f.server.nodeIdentity.NodeID()}}
			v := "second"
			next = &v
		} else if cursor == "second" {
			collections = []map[string]string{{"collection_id": "collection-a", "origin_node_id": f.server.nodeIdentity.NodeID()}, {"collection_id": "collection-b", "origin_node_id": f.server.nodeIdentity.NodeID()}}
			v := "terminal"
			next = &v
		} else if cursor == "terminal" {
			complete = true
		} else {
			t.Error("unrecognized producer cursor")
		}
		if current == "no_terminal" && cursor == "second" {
			next = nil
		}
		if current == "mixed_revision" && cursor != "" {
			revision++
		}
		if current == "premature_complete" && cursor == "" {
			complete = true
		}
		if current == "cycle" {
			v := "first"
			next = &v
		}
		json.NewEncoder(w).Encode(map[string]any{"snapshot_id": "catalog-stable", "scope_id": r.URL.Query().Get("scope_id"), "caller_scope_hash": r.Header.Get(identity.HeaderCallerScope), "origin_node_id": f.server.nodeIdentity.NodeID(), "registry_revision": revision, "created_at": created, "valid_until": valid, "first_cursor": "first", "terminal_cursor": "terminal", "collections": collections, "total": 2, "next_cursor": next, "complete": complete})
	}))
	t.Cleanup(producer.Close)
	f.server.cfg.CorpusURL = producer.URL
	f.server.corpus, _ = proxy.New("corpus", producer.URL, f.server.cfg.ServiceToken)
	return &calls
}

func TestScopeHTTPPersistentWirePagingIsolationAndRevocation(t *testing.T) {
	f := discoveryPGFixture(t)
	var mode atomic.Value
	mode.Store("normal")
	calls := scopeCatalogFixture(t, f, &mode)
	out := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search", "page_size": 1}), 201)
	if out.Manifest.EnumerationState != "sealed" || out.TotalTargets != 2 || calls.Load() != 3 || out.ContentSnapshot != "not_frozen" {
		t.Fatalf("collection pages not fully consumed %+v calls=%d", out, calls.Load())
	}
	path := "/api/v1/federation/scopes/" + out.Manifest.ScopeID
	if w := requestDiscovery(t, f.handler, "GET", path, f.bobToken, nil); w.Code != 404 {
		t.Fatalf("scope crossed user: %d", w.Code)
	}
	_, key, err := f.server.store.CreateAPIKey(context.Background(), f.org, f.alice.ID, "separate", []rbac.Scope{rbac.ScopeRead}, nil, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	if w := requestDiscovery(t, f.handler, "GET", path, key, nil); w.Code != 404 {
		t.Fatalf("scope crossed credentials: %d", w.Code)
	}
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search", "caller_scope_hash": "fake", "expanded_members": []any{}}); w.Code != 400 {
		t.Fatal("caller supplied denominator")
	}
	page := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"/targets", f.aliceToken, nil), 200)
	if page.Complete || page.TotalTargets != 2 || len(page.Targets) != 1 || page.Targets[0].TargetKey.CollectionID != "collection-a" || page.Targets[0].State != "not_attempted" {
		t.Fatalf("bad page %+v", page)
	}
	mode.Store("changed")
	changed := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"/targets", f.aliceToken, nil), 200)
	if changed.Targets[0].State != "unreachable" {
		t.Fatal("changed catalog mistaken for revocation or valid current target")
	}
	mode.Store("revoked")
	revoked := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"/targets", f.aliceToken, nil), 200)
	if revoked.Targets[0].State != "revoked" || revoked.TotalTargets != 2 || revoked.ManifestDigest != out.Manifest.ManifestDigest {
		t.Fatalf("revocation dropped target %+v", revoked)
	}
	before := calls.Load()
	mode.Store("normal")
	repeated := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"/targets", f.aliceToken, nil), 200)
	if repeated.Targets[0].State != "revoked" || calls.Load() != before {
		t.Fatal("later response resurrected revoked scope")
	}
	terminal := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"/targets?cursor="+out.TerminalCursor, f.aliceToken, nil), 200)
	if !terminal.Complete || len(terminal.Targets) != 0 || terminal.NextCursor != nil {
		t.Fatal("terminal changed")
	}
	read := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", path, f.aliceToken, nil), 200)
	if read.Manifest.ManifestDigest != out.Manifest.ManifestDigest || len(read.Manifest.ExpandedMembers) != 2 {
		t.Fatal("revocation rewrote original manifest")
	}
}

func TestScopeHTTPPartialForInvalidEnumerationAndBudget(t *testing.T) {
	f := discoveryPGFixture(t)
	var mode atomic.Value
	mode.Store("normal")
	calls := scopeCatalogFixture(t, f, &mode)
	for _, scenario := range []string{"no_terminal", "mixed_revision", "premature_complete", "cycle", "redirect"} {
		t.Run(scenario, func(t *testing.T) {
			mode.Store(scenario)
			before := calls.Load()
			out := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search"}), 201)
			if out.Manifest.EnumerationState != "partial" || len(out.Manifest.UnexpandedSubtrees) == 0 {
				t.Fatalf("invalid enumeration sealed %+v", out)
			}
			if scenario == "mixed_revision" && out.TotalTargets != 1 {
				t.Fatal("joined targets across directory versions")
			}
			if scenario == "redirect" && calls.Load()-before != 1 {
				t.Fatal("producer redirect followed")
			}
		})
	}
	mode.Store("normal")
	out := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search", "max_members": 1}), 201)
	if out.Manifest.EnumerationState != "partial" || out.TotalTargets != 1 || out.Manifest.UnexpandedSubtrees[0].Reason != "budget_exhausted" {
		t.Fatalf("budget incomplete hidden %+v", out)
	}
}
