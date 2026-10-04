package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

type recursiveCenter struct {
	f     *discoveryFixture
	http  *httptest.Server
	reads atomic.Int32
}

func newRecursiveCenter(t *testing.T, leaf bool) *recursiveCenter {
	t.Helper()
	c := &recursiveCenter{f: discoveryPGFixture(t)}
	created := time.Now().UTC().Truncate(time.Second)
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/internal/federation/collections" {
			json.NewEncoder(w).Encode(map[string]any{"snapshot_id": "local-empty", "scope_id": r.URL.Query().Get("scope_id"), "caller_scope_hash": r.Header.Get("X-DDP-Caller-Scope"), "origin_node_id": c.f.server.nodeIdentity.NodeID(), "registry_revision": 1, "created_at": created, "valid_until": created.Add(time.Hour), "first_cursor": "end", "terminal_cursor": "end", "collections": []any{}, "total": 0, "next_cursor": nil, "complete": true})
			return
		}
		if r.URL.Path != "/internal/federation/published-collections" {
			http.NotFound(w, r)
			return
		}
		collections := []map[string]string{}
		next := any(nil)
		complete := true
		first := "end"
		total := 0
		if leaf {
			first = "first"
			total = 1
			if r.URL.Query().Get("cursor") != "end" {
				complete = false
				next = "end"
				collections = append(collections, map[string]string{"origin_node_id": c.f.server.nodeIdentity.NodeID(), "collection_id": "leaf"})
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"snapshot_id": "published", "origin_node_id": c.f.server.nodeIdentity.NodeID(), "registry_revision": 1, "created_at": created, "valid_until": created.Add(time.Hour), "first_cursor": first, "terminal_cursor": "end", "collections": collections, "total": total, "next_cursor": next, "complete": complete})
	}))
	t.Cleanup(producer.Close)
	c.f.server.cfg.CorpusURL = producer.URL
	c.f.server.corpus, _ = proxy.New("corpus", producer.URL, c.f.server.cfg.ServiceToken)
	c.http = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/api/v1/federation/") {
			c.reads.Add(1)
		}
		c.f.handler.ServeHTTP(w, r)
	}))
	t.Cleanup(c.http.Close)
	return c
}
func linkRecursiveCenters(t *testing.T, from, to *recursiveCenter, visible bool) {
	t.Helper()
	node := to.f.server.nodeIdentity
	registration := discovery.Registration{PublicKey: node.PublicKey(), VisibleToOrg: visible, Descriptor: discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: node.NodeID(), ProtocolVersions: []string{"ddp-discovery/1"}, AuthMethods: []string{"node_credential"}, ControlledEndpoints: []discovery.Endpoint{{Purpose: "federation", URL: to.http.URL + "/api/v1/federation"}}, DiscoveryCapabilities: discovery.DiscoveryCapabilities{EnumerateMembers: true}, Revision: 1, ValidUntil: time.Now().UTC().Add(time.Hour)}}
	approveEnumerableNode(t, from.f, registration)
}
func configureRecursiveCenters(centers map[*recursiveCenter][]*recursiveCenter) {
	for from, targets := range centers {
		peers := map[string]discovery.PeerConfig{}
		for _, to := range targets {
			node := to.f.server.nodeIdentity.NodeID()
			peers[node] = discovery.PeerConfig{NodeID: node, Endpoint: to.http.URL}
		}
		from.f.server.peers = discovery.NewPeerDirectory(peers, nil, 2*time.Second, from.f.server.signPeerRead)
	}
}
func manifestRoutes(t *testing.T, m discovery.ScopeManifest) map[string][]string {
	t.Helper()
	body, _ := json.Marshal(m)
	var wire struct {
		Routes []struct {
			NodeID string   `json:"node_id"`
			Via    []string `json:"via_node_ids"`
		} `json:"node_routes"`
	}
	if err := json.Unmarshal(body, &wire); err != nil {
		t.Fatal(err)
	}
	out := map[string][]string{}
	for _, r := range wire.Routes {
		out[r.NodeID] = r.Via
	}
	return out
}
func TestRecursiveScopePersistsRoutesAndRevokesWholeSubtree(t *testing.T) {
	for _, deep := range []bool{false, true} {
		t.Run(fmt.Sprint("deep=", deep), func(t *testing.T) {
			a, p, r := newRecursiveCenter(t, false), newRecursiveCenter(t, false), newRecursiveCenter(t, true)
			linkRecursiveCenters(t, a, p, true)
			linkRecursiveCenters(t, p, a, false)
			linkRecursiveCenters(t, p, r, true)
			linkRecursiveCenters(t, r, p, false)
			links := map[*recursiveCenter][]*recursiveCenter{a: {p}, p: {a, r}, r: {p}}
			origin := r.f.server.nodeIdentity.NodeID()
			via := []string{p.f.server.nodeIdentity.NodeID()}
			want := 1
			var s *recursiveCenter
			if deep {
				s = newRecursiveCenter(t, true)
				linkRecursiveCenters(t, r, s, true)
				linkRecursiveCenters(t, s, r, false)
				links[r] = append(links[r], s)
				links[s] = []*recursiveCenter{r}
				origin = s.f.server.nodeIdentity.NodeID()
				via = append(via, r.f.server.nodeIdentity.NodeID())
				want = 2
			}
			configureRecursiveCenters(links)
			out := createScope(t, a.f, map[string]any{"operation": "search"})
			if out.TotalTargets != want || out.Manifest.EnumerationState != "sealed" {
				t.Fatalf("recursive scope incomplete: %+v", out.Manifest)
			}
			if !reflect.DeepEqual(manifestRoutes(t, out.Manifest)[origin], via) {
				t.Fatalf("wrong recursive route: %+v", manifestRoutes(t, out.Manifest))
			}
			if r.reads.Load() == 0 || (deep && s.reads.Load() == 0) {
				t.Fatal("subtree never contacted its approved leaf")
			}
			read := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, a.f.handler, "GET", "/api/v1/federation/scopes/"+out.Manifest.ScopeID, a.f.aliceToken, nil), 200)
			if read.Manifest.ManifestDigest != out.Manifest.ManifestDigest || !reflect.DeepEqual(manifestRoutes(t, read.Manifest), manifestRoutes(t, out.Manifest)) {
				t.Fatal("routes or digest lost after persistence")
			}
			path := "/api/v1/federation/scopes/" + out.Manifest.ScopeID + "/targets"
			page := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, a.f.handler, "GET", path, a.f.aliceToken, nil), 200)
			for _, target := range page.Targets {
				if target.State != "not_attempted" {
					t.Fatalf("unregistered descendant falsely revoked: %+v", target)
				}
			}
			decodeDiscovery[map[string]any](t, requestDiscovery(t, a.f.handler, "POST", "/api/v1/federation/nodes/"+p.f.server.nodeIdentity.NodeID()+"/revoke", a.f.adminToken, nil), 200)
			page = decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, a.f.handler, "GET", path, a.f.aliceToken, nil), 200)
			for _, target := range page.Targets {
				if target.State != "revoked" {
					t.Fatalf("revoking parent failed to revoke leaf: %+v", target)
				}
			}
			if page.TotalTargets != want || page.ManifestDigest != out.Manifest.ManifestDigest {
				t.Fatal("revocation rewrote denominator")
			}
		})
	}
}
func TestRecursiveScopeBudgetHonestyAndDirectRoute(t *testing.T) {
	a, p, r := newRecursiveCenter(t, false), newRecursiveCenter(t, false), newRecursiveCenter(t, true)
	linkRecursiveCenters(t, a, p, true)
	linkRecursiveCenters(t, p, a, false)
	linkRecursiveCenters(t, p, r, true)
	linkRecursiveCenters(t, r, p, false)
	configureRecursiveCenters(map[*recursiveCenter][]*recursiveCenter{a: {p}, p: {a, r}, r: {p}})
	// P costs three physical reads of R. A costs three P directory reads plus
	// two subtree pages: exact total eight. Seven retains the leaf, not sealing.
	out := createScope(t, a.f, map[string]any{"operation": "search", "max_discovery_requests": 7})
	if out.TotalTargets != 1 || out.Manifest.EnumerationState != "partial" || r.reads.Load() != 3 {
		t.Fatalf("nested consumption not charged, or observed leaf lost: %+v reads=%d", out.Manifest, r.reads.Load())
	}
	budget := false
	for _, u := range out.Manifest.UnexpandedSubtrees {
		if u.Reason == "budget_exhausted" {
			budget = true
		}
	}
	if !budget {
		t.Fatal("exhaustion not visible")
	}
	out = createScope(t, a.f, map[string]any{"operation": "search", "max_discovery_requests": 8, "max_remote_members": 2})
	if out.TotalTargets != 1 || out.Manifest.EnumerationState != "sealed" {
		t.Fatalf("exact recursive budget failed: %+v", out.Manifest)
	}
	linkRecursiveCenters(t, a, r, true)
	linkRecursiveCenters(t, r, a, false)
	configureRecursiveCenters(map[*recursiveCenter][]*recursiveCenter{a: {p, r}, p: {a, r}, r: {p, a}})
	before := r.reads.Load()
	out = createScope(t, a.f, map[string]any{"operation": "search", "max_remote_members": 2})
	if out.TotalTargets != 1 || out.Manifest.EnumerationState != "sealed" || len(manifestRoutes(t, out.Manifest)) != 0 || r.reads.Load()-before != 3 {
		t.Fatalf("direct route did not win or node contacted twice: %+v reads=%d", out.Manifest, r.reads.Load()-before)
	}
}
func TestPeerSubtreeLoopVisibilityPagingAndIssuerApproval(t *testing.T) {
	a, p, r := newRecursiveCenter(t, false), newRecursiveCenter(t, false), newRecursiveCenter(t, true)
	linkRecursiveCenters(t, a, p, true)
	linkRecursiveCenters(t, p, a, false)
	linkRecursiveCenters(t, p, r, true)
	linkRecursiveCenters(t, r, p, true)
	configureRecursiveCenters(map[*recursiveCenter][]*recursiveCenter{a: {p}, p: {a, r}, r: {p}})
	base := "/api/v1/federation/subtree?path=" + a.f.server.nodeIdentity.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	type pageWire struct {
		SnapshotID string  `json:"snapshot_id"`
		First      string  `json:"first_cursor"`
		Terminal   string  `json:"terminal_cursor"`
		Next       *string `json:"next_cursor"`
		Complete   bool    `json:"complete"`
		Targets    []struct {
			Key discovery.TargetKey `json:"target_key"`
			Via []string            `json:"via_node_ids"`
		} `json:"targets"`
		State       string `json:"enumeration_state"`
		Consumption struct {
			Requests int `json:"requests"`
			Nodes    int `json:"nodes"`
		} `json:"consumption"`
	}
	token := signedPeerRead(t, p.f, a.f.server.nodeIdentity, base)
	page := decodeDiscovery[pageWire](t, requestPeer(t, p.f.handler, base, token), 200)
	if len(page.Targets) != 1 || page.Targets[0].Key.OriginNodeID != r.f.server.nodeIdentity.NodeID() || len(page.Targets[0].Via) != 0 || page.State != "sealed" || page.Consumption.Requests != 4 || page.Consumption.Nodes != 1 || r.reads.Load() != 4 {
		t.Fatalf("loop or consumption incorrect: %+v", page)
	}
	if replay := requestPeer(t, p.f.handler, base, token); replay.Code != 401 {
		t.Fatalf("subtree nonce replay accepted: %d", replay.Code)
	}
	continuation := base + "&snapshot_id=" + page.SnapshotID + "&cursor=" + page.Terminal
	restarted := *p.f.server
	terminal := decodeDiscovery[pageWire](t, requestPeer(t, restarted.Routes(), continuation, signedPeerRead(t, p.f, a.f.server.nodeIdentity, continuation)), 200)
	if !terminal.Complete || len(terminal.Targets) != 0 || terminal.Consumption != page.Consumption || r.reads.Load() != 4 {
		t.Fatal("continuation recomputed expansion or lost frozen consumption")
	}
	changed := strings.Replace(continuation, "max_nodes=2", "max_nodes=1", 1)
	if w := requestPeer(t, p.f.handler, changed, signedPeerRead(t, p.f, a.f.server.nodeIdentity, changed)); w.Code != 404 {
		t.Fatalf("snapshot budget binding changed: %d", w.Code)
	}
	selfPath := "/api/v1/federation/subtree?path=" + p.f.server.nodeIdentity.NodeID() + "," + a.f.server.nodeIdentity.NodeID() + "&max_requests=10&max_nodes=2"
	self := decodeDiscovery[pageWire](t, requestPeer(t, p.f.handler, selfPath, signedPeerRead(t, p.f, a.f.server.nodeIdentity, selfPath)), 200)
	if len(self.Targets) != 0 || self.Consumption.Requests != 0 || r.reads.Load() != 4 {
		t.Fatal("responder in caller path still expanded")
	}
	boundary := base + "&allowed_node_ids=" + url.QueryEscape(a.f.server.nodeIdentity.NodeID()+","+p.f.server.nodeIdentity.NodeID())
	excluded := decodeDiscovery[pageWire](t, requestPeer(t, p.f.handler, boundary, signedPeerRead(t, p.f, a.f.server.nodeIdentity, boundary)), 200)
	if len(excluded.Targets) != 0 || excluded.Consumption.Requests != 0 || r.reads.Load() != 4 {
		t.Fatal("recipient boundary crossed at responder")
	}
	outsider := newRecursiveCenter(t, false)
	denied := signedPeerRead(t, p.f, outsider.f.server.nodeIdentity, strings.Replace(base, a.f.server.nodeIdentity.NodeID(), outsider.f.server.nodeIdentity.NodeID(), 1))
	if w := requestPeer(t, p.f.handler, strings.Replace(base, a.f.server.nodeIdentity.NodeID(), outsider.f.server.nodeIdentity.NodeID(), 1), denied); w.Code != 403 {
		t.Fatalf("unapproved subtree issuer accepted: %d", w.Code)
	}
}
