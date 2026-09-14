package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// catalogPageSpec is one producer page in the controllable count-mismatch
// producer. A nil Total omits the field entirely (the hostile/older producer
// shape), which is deliberately different from Total=0.
type catalogPageSpec struct {
	collections []string
	next        *string
	complete    bool
	total       any
}

func cursor(name string) *string { return &name }

// collectAgainstProducer exercises collectScopeCatalog with no PostgreSQL at
// all: the collector only speaks HTTP to the configured corpus producer.
func collectAgainstProducer(t *testing.T, pages map[string]catalogPageSpec, firstCursor, terminalCursor string, budget int) discovery.CollectionCatalog {
	t.Helper()
	if firstCursor == "" {
		firstCursor = "first"
	}
	if terminalCursor == "" {
		terminalCursor = "terminal"
	}
	if budget <= 0 {
		budget = 100
	}
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "node"), true)
	if err != nil {
		t.Fatal(err)
	}
	actor := &identity.Actor{Kind: identity.KindUser, ID: "alice", UserID: "alice", OrganizationID: "org-test", Role: rbac.Contributor}
	scopeID := "scope-catalog-guard"
	callerScope := discovery.ScopeHash(actor)
	created := time.Now().UTC().Truncate(time.Second)
	valid := created.Add(time.Hour)
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		spec, ok := pages[r.URL.Query().Get("cursor")]
		if !ok {
			t.Errorf("producer received an unexpected cursor %q", r.URL.Query().Get("cursor"))
			spec = catalogPageSpec{complete: true}
		}
		collections := []map[string]string{}
		for _, id := range spec.collections {
			collections = append(collections, map[string]string{"collection_id": id, "origin_node_id": node.NodeID()})
		}
		body := map[string]any{
			"snapshot_id": "catalog-stable", "scope_id": scopeID, "caller_scope_hash": callerScope,
			"origin_node_id": node.NodeID(), "registry_revision": 7,
			"created_at": created, "valid_until": valid,
			"first_cursor": firstCursor, "terminal_cursor": terminalCursor,
			"collections": collections, "next_cursor": spec.next, "complete": spec.complete,
		}
		if spec.total != nil {
			body["total"] = spec.total
		}
		_ = json.NewEncoder(w).Encode(body)
	}))
	t.Cleanup(producer.Close)
	upstream, err := proxy.New("corpus", producer.URL, "internal-test-service")
	if err != nil {
		t.Fatal(err)
	}
	server := &Server{cfg: &config.Config{CorpusURL: producer.URL, ServiceToken: "internal-test-service"}, corpus: upstream, nodeIdentity: node}
	return server.collectScopeCatalog(context.Background(), actor, scopeID, budget)
}

// The producer's own declared total must match the final unique observed count.
// Any contradiction is partial, keeps the consistently observed targets, and
// fails with an honest reason instead of sealing a truncated scope.
func TestCollectScopeCatalogRejectsProducerCountMismatch(t *testing.T) {
	for _, tc := range []struct {
		name        string
		pages       map[string]catalogPageSpec
		first       string
		terminal    string
		budget      int
		wantSealed  bool
		wantTargets []string
	}{
		{
			// Reviewer counterexample 1: an empty data page that points straight
			// at the terminal page proves nothing when total=2.
			name: "empty_data_page_declares_two",
			pages: map[string]catalogPageSpec{
				"":         {next: cursor("terminal"), total: 2},
				"terminal": {complete: true, total: 2},
			},
		},
		{
			// Reviewer counterexample 2: the terminal page cannot excuse an
			// omitted collection. The observed one is retained.
			name: "omitted_collection",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("terminal"), total: 2},
				"terminal": {complete: true, total: 2},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			// Reviewer counterexample 3: total changing across pages.
			name: "changing_total",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("terminal"), total: 1},
				"terminal": {complete: true, total: 2},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			// A missing total is not the same contract value as total=0.
			name: "missing_total",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("terminal")},
			},
		},
		{
			// A missing total on a single empty page must not be read as a
			// verified total=0 complete catalog.
			name: "missing_total_on_empty_catalog",
			pages: map[string]catalogPageSpec{
				"": {complete: true},
			},
			first:    "only",
			terminal: "only",
		},
		{
			name: "negative_total",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("terminal"), total: -1},
			},
		},
		{
			name: "unbounded_total",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("terminal"), total: 10001},
			},
		},
		{
			// More unique collections than declared: the surplus is not kept.
			name: "more_unique_than_declared",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a", "collection-b"}, next: cursor("terminal"), total: 1},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			// Deduplication must not mask an inconsistent denominator.
			name: "duplicate_masking",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("middle"), total: 2},
				"middle":   {collections: []string{"collection-a"}, next: cursor("terminal"), total: 2},
				"terminal": {complete: true, total: 2},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			name: "complete_on_data_page",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("terminal"), complete: true, total: 1},
			},
		},
		{
			name: "cursor_cycle",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("first"), total: 2},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			name: "budget_exhausted",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a", "collection-b", "collection-c"}, next: cursor("terminal"), total: 3},
			},
			budget:      2,
			wantTargets: []string{"collection-a", "collection-b"},
		},
		{
			// A consistent duplicate-free two-page catalog still seals.
			name: "consistent_catalog_seals",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("middle"), total: 2},
				"middle":   {collections: []string{"collection-b"}, next: cursor("terminal"), total: 2},
				"terminal": {complete: true, total: 2},
			},
			wantSealed:  true,
			wantTargets: []string{"collection-a", "collection-b"},
		},
		{
			// An empty page may only step to the directly following terminal page
			// once every declared collection was observed.
			name: "consistent_empty_middle_page_seals",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("middle"), total: 1},
				"middle":   {next: cursor("terminal"), total: 1},
				"terminal": {complete: true, total: 1},
			},
			wantSealed:  true,
			wantTargets: []string{"collection-a"},
		},
		{
			// The same empty page without the terminal link is not a valid step.
			name: "empty_page_not_pointing_at_terminal",
			pages: map[string]catalogPageSpec{
				"":         {collections: []string{"collection-a"}, next: cursor("middle"), total: 1},
				"middle":   {next: cursor("other"), total: 1},
				"other":    {complete: true, total: 1},
				"terminal": {complete: true, total: 1},
			},
			wantTargets: []string{"collection-a"},
		},
		{
			// An empty catalog must still supply its separate terminal proof.
			// (A single page where first_cursor==terminal_cursor is that proof.)
			name: "verified_empty_catalog_seals",
			pages: map[string]catalogPageSpec{
				"": {complete: true, total: 0},
			},
			first:      "only",
			terminal:   "only",
			wantSealed: true,
		},
		{
			// total=0 with data present contradicts the declaration.
			name: "zero_total_with_data",
			pages: map[string]catalogPageSpec{
				"": {collections: []string{"collection-a"}, next: cursor("terminal"), total: 0},
			},
			wantTargets: nil,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out := collectAgainstProducer(t, tc.pages, tc.first, tc.terminal, tc.budget)
			if out.Complete != tc.wantSealed {
				t.Fatalf("sealed=%v, want %v (targets=%v reason=%q)", out.Complete, tc.wantSealed, out.Collections, out.FailureReason)
			}
			if !slices.Equal(out.Collections, tc.wantTargets) {
				t.Fatalf("observed targets %v, want %v", out.Collections, tc.wantTargets)
			}
			if out.Complete && out.FailureReason != "" {
				t.Fatalf("sealed catalog kept failure reason %q", out.FailureReason)
			}
			if !out.Complete && out.FailureReason == "" {
				t.Fatal("unsealed catalog did not report an honest failure reason")
			}
		})
	}
}
