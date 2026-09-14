package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

func requestPeer(t *testing.T, h http.Handler, path, token string) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	if token != "" {
		r.Header.Set(discovery.HeaderPeerToken, token)
	}
	// The entry boundary strips these; a peer must not be able to inject them.
	r.Header.Set(identity.HeaderOrganization, "forged-org")
	r.Header.Set(identity.HeaderActor, "forged-admin")
	r.Header.Set(identity.HeaderRole, "admin")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func registerPeerNode(t *testing.T, f *discoveryFixture, visible bool, approveNode bool) string {
	t.Helper()
	registration := remoteRegistration(t, visible)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration), 201)
	if approveNode {
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+registration.Descriptor.NodeID+"/approve", f.adminToken, nil), 200)
	}
	return registration.Descriptor.NodeID
}

func TestPeerMembersFailsClosedAndServesOnlyVisibleApproved(t *testing.T) {
	f := discoveryPGFixture(t)
	visible := registerPeerNode(t, f, true, true)
	hidden := registerPeerNode(t, f, false, true)
	pending := registerPeerNode(t, f, true, false)

	if w := requestPeer(t, f.handler, "/api/v1/federation/members", ""); w.Code != 401 {
		t.Fatalf("unconfigured token should fail closed: %d", w.Code)
	}
	if w := requestPeer(t, f.handler, "/api/v1/federation/members", "any"); w.Code != 401 {
		t.Fatalf("empty configured peer token must reject anything: %d", w.Code)
	}
	f.server.cfg.FederationPeerToken = "peer-shared-secret"
	if w := requestPeer(t, f.handler, "/api/v1/federation/members", "wrong"); w.Code != 401 {
		t.Fatalf("wrong peer token accepted: %d", w.Code)
	}
	w := requestPeer(t, f.handler, "/api/v1/federation/members", "peer-shared-secret")
	if w.Code != 200 {
		t.Fatalf("peer members read failed: %d %s", w.Code, w.Body.String())
	}
	page := decodeDiscovery[discovery.PeerMemberPage](t, w, 200)
	if page.AuthorityNodeID != f.server.nodeIdentity.NodeID() || page.RegistryRevision < 1 || page.ExpiresAt.Before(page.CreatedAt) {
		t.Fatalf("bad peer page envelope: %+v", page)
	}
	if len(page.Members) != 1 || page.Members[0].NodeID != visible {
		t.Fatalf("hidden/pending members leaked: %+v", page.Members)
	}
	body := w.Body.String()
	if strings.Contains(body, hidden) || strings.Contains(body, pending) || strings.Contains(body, "forged-org") {
		t.Fatalf("peer page disclosed hidden identity or forged context: %s", body)
	}
	terminal := decodeDiscovery[discovery.PeerMemberPage](t, requestPeer(t, f.handler, "/api/v1/federation/members?snapshot_id="+page.SnapshotID+"&cursor="+page.TerminalCursor, "peer-shared-secret"), 200)
	if !terminal.Complete || len(terminal.Members) != 0 || terminal.NextCursor != nil {
		t.Fatalf("terminal page wrong: %+v", terminal)
	}
	// The stable snapshot rejects a changed page size on continuation.
	if w := requestPeer(t, f.handler, "/api/v1/federation/members?snapshot_id="+page.SnapshotID+"&cursor="+page.FirstCursor+"&limit=1", "peer-shared-secret"); w.Code != 400 {
		t.Fatalf("changed continuation limit accepted: %d", w.Code)
	}
	// Target binding: a peer cannot ask this node to serve another node's data.
	request := httptest.NewRequest(http.MethodGet, "/api/v1/federation/members", nil)
	request.Header.Set(discovery.HeaderPeerToken, "peer-shared-secret")
	request.Header.Set(discovery.HeaderPeerTarget, "node-someone-else")
	recorder := httptest.NewRecorder()
	f.handler.ServeHTTP(recorder, request)
	if recorder.Code != 409 {
		t.Fatalf("wrong target accepted: %d", recorder.Code)
	}
}

func TestPeerCollectionsProxiesCorpusAndNeverLeaksCallerScope(t *testing.T) {
	f := discoveryPGFixture(t)
	f.server.cfg.FederationPeerToken = "peer-shared-secret"
	nodeID := f.server.nodeIdentity.NodeID()
	created := time.Now().UTC()
	var sawService bool
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/federation/published-collections" || r.Header.Get(identity.HeaderActor) != "control-api" ||
			r.Header.Get(identity.HeaderActorKind) != "service" || r.Header.Get(identity.HeaderOrganization) != f.org ||
			r.Header.Get("Authorization") != "Bearer "+f.server.cfg.ServiceToken {
			t.Error("peer catalog proxy did not use the configured service identity")
		}
		sawService = true
		_ = json.NewEncoder(w).Encode(map[string]any{
			"snapshot_id": "peer-catalog", "scope_id": "caller-scope-must-not-leak",
			"caller_scope_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"origin_node_id":    nodeID, "registry_revision": 2,
			"created_at": created, "valid_until": created.Add(time.Hour),
			"first_cursor": "only", "terminal_cursor": "only", "total": 1,
			"collections": []any{map[string]any{"collection_id": "public-collection", "origin_node_id": nodeID,
				"index_revision": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "revision": 2,
				"valid_until": created.Add(time.Hour)}},
			"index_readiness": map[string]string{"public-collection": "ready"},
			"next_cursor":     nil, "complete": false, "content_snapshot_complete": false,
		})
	}))
	defer producer.Close()
	f.server.cfg.CorpusURL = producer.URL
	f.server.corpus, _ = proxy.New("corpus", producer.URL, f.server.cfg.ServiceToken)

	if w := requestPeer(t, f.handler, "/api/v1/federation/collections", "wrong"); w.Code != 401 || sawService {
		t.Fatalf("unauthenticated peer reached corpus: %d", w.Code)
	}
	w := requestPeer(t, f.handler, "/api/v1/federation/collections", "peer-shared-secret")
	if w.Code != 200 {
		t.Fatalf("peer catalog read failed: %d %s", w.Code, w.Body.String())
	}
	body := w.Body.String()
	if strings.Contains(body, "caller-scope-must-not-leak") || strings.Contains(body, "caller_scope_hash") || strings.Contains(body, "index_readiness") {
		t.Fatalf("caller-scope internals leaked to peer: %s", body)
	}
	var page peerCatalogResponse
	if err := json.Unmarshal(w.Body.Bytes(), &page); err != nil {
		t.Fatal(err)
	}
	if page.AuthorityNodeID != nodeID || page.Total != 1 || len(page.Collections) != 1 || page.RegistryRevision != 2 {
		t.Fatalf("bad peer catalog mapping: %+v", page)
	}

	// A corpus response whose envelope identifies this node but contains a
	// descriptor from another origin must never be relayed. The origin check is
	// per descriptor, not only on the page envelope.
	foreign := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"snapshot_id": "peer-catalog", "scope_id": "s", "caller_scope_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"origin_node_id": nodeID, "registry_revision": 2,
			"created_at": created, "valid_until": created.Add(time.Hour),
			"first_cursor": "only", "terminal_cursor": "only", "total": 1,
			"collections": []any{map[string]any{"collection_id": "stolen", "origin_node_id": "node-someone-else"}},
			"next_cursor": nil, "complete": false,
		})
	}))
	defer foreign.Close()
	f.server.cfg.CorpusURL = foreign.URL
	f.server.corpus, _ = proxy.New("corpus", foreign.URL, f.server.cfg.ServiceToken)
	if w := requestPeer(t, f.handler, "/api/v1/federation/collections", "peer-shared-secret"); w.Code != 502 {
		t.Fatalf("foreign-origin catalog descriptor relayed: %d", w.Code)
	}

	// The envelope itself must name this node; a valid-looking descriptor on a
	// foreign envelope is equally untrustworthy.
	wrongEnvelope := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"snapshot_id": "peer-catalog", "scope_id": "s", "caller_scope_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"origin_node_id": "node-someone-else", "registry_revision": 2,
			"created_at": created, "valid_until": created.Add(time.Hour),
			"first_cursor": "only", "terminal_cursor": "only", "total": 1,
			"collections": []any{map[string]any{"collection_id": "stolen", "origin_node_id": nodeID}},
			"next_cursor": nil, "complete": false,
		})
	}))
	defer wrongEnvelope.Close()
	f.server.cfg.CorpusURL = wrongEnvelope.URL
	f.server.corpus, _ = proxy.New("corpus", wrongEnvelope.URL, f.server.cfg.ServiceToken)
	if w := requestPeer(t, f.handler, "/api/v1/federation/collections", "peer-shared-secret"); w.Code != 502 {
		t.Fatalf("foreign-origin catalog envelope relayed: %d", w.Code)
	}
}
