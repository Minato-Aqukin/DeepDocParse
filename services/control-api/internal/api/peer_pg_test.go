package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
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
		r.Header.Set(discovery.HeaderNodeCredential, token)
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

func approvedReadPeer(t *testing.T, f *discoveryFixture) *discovery.Identity {
	t.Helper()
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "identity"), true)
	if err != nil {
		t.Fatal(err)
	}
	registration := discovery.Registration{PublicKey: node.PublicKey(), VisibleToOrg: false,
		Descriptor: discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: node.NodeID(),
			ProtocolVersions: []string{"ddp-discovery/1"}, AuthMethods: []string{"node_credential"},
			ControlledEndpoints: []discovery.Endpoint{{Purpose: "federation", URL: "https://peer.example/api/v1/federation"}},
			Revision:            1, ValidUntil: time.Now().UTC().Add(time.Hour)}}
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration), 201)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+node.NodeID()+"/approve", f.adminToken, nil), 200)
	return node
}

func signedPeerRead(t *testing.T, f *discoveryFixture, node *discovery.Identity, path string) string {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	op, err := peerReadOperation(r.URL.Path)
	if err != nil {
		t.Fatal(err)
	}
	jti, err := discovery.NewCredentialJTI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	token, err := node.SignCredential(discovery.CredentialClaims{
		Schema: discovery.CredentialSchema, Alg: discovery.CredentialAlg,
		IssuerNodeID: node.NodeID(), AudienceNodeID: f.server.nodeIdentity.NodeID(),
		Actor:     discovery.CredentialActor{OrganizationID: "foreign-org", Subject: "control-api", Kind: "service"},
		Operation: op, Constraints: discovery.CredentialConstraints{RootTaskID: "directory:" + jti, ScopeRef: peerReadDigest(r.URL.Query().Encode())},
		Request:  discovery.CredentialRequest{Method: "GET", Path: r.URL.Path, BodyDigest: peerReadDigest("")},
		IssuedAt: now, ExpiresAt: now + 60, JTI: jti,
	})
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func TestPeerMembersFailsClosedAndServesOnlyVisibleApproved(t *testing.T) {
	f := discoveryPGFixture(t)
	visible := registerPeerNode(t, f, true, true)
	hidden := registerPeerNode(t, f, false, true)
	pending := registerPeerNode(t, f, true, false)
	peer := approvedReadPeer(t, f)

	if w := requestPeer(t, f.handler, "/api/v1/federation/members", ""); w.Code != 401 {
		t.Fatalf("missing credential should fail closed: %d", w.Code)
	}
	if w := requestPeer(t, f.handler, "/api/v1/federation/members", "any"); w.Code != 401 {
		t.Fatalf("malformed credential accepted: %d", w.Code)
	}
	if w := requestPeer(t, f.handler, "/api/v1/federation/members", "wrong"); w.Code != 401 {
		t.Fatalf("invalid credential accepted: %d", w.Code)
	}
	credential := signedPeerRead(t, f, peer, "/api/v1/federation/members")
	w := requestPeer(t, f.handler, "/api/v1/federation/members", credential)
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
	// A separately constructed receiver still refuses the committed nonce.
	restarted := *f.server
	if replay := requestPeer(t, restarted.Routes(), "/api/v1/federation/members", credential); replay.Code != 401 {
		t.Fatalf("credential replay survived receiver restart: %d", replay.Code)
	}
	terminalPath := "/api/v1/federation/members?snapshot_id=" + page.SnapshotID + "&cursor=" + page.TerminalCursor
	terminal := decodeDiscovery[discovery.PeerMemberPage](t, requestPeer(t, f.handler, terminalPath, signedPeerRead(t, f, peer, terminalPath)), 200)
	if !terminal.Complete || len(terminal.Members) != 0 || terminal.NextCursor != nil {
		t.Fatalf("terminal page wrong: %+v", terminal)
	}
	// The stable snapshot rejects a changed page size on continuation.
	changedPath := "/api/v1/federation/members?snapshot_id=" + page.SnapshotID + "&cursor=" + page.FirstCursor + "&limit=1"
	if w := requestPeer(t, f.handler, changedPath, signedPeerRead(t, f, peer, changedPath)); w.Code != 400 {
		t.Fatalf("changed continuation limit accepted: %d", w.Code)
	}
	// Target binding: a peer cannot ask this node to serve another node's data.
	request := httptest.NewRequest(http.MethodGet, "/api/v1/federation/members", nil)
	request.Header.Set(discovery.HeaderNodeCredential, signedPeerRead(t, f, peer, "/api/v1/federation/members"))
	request.Header.Set(discovery.HeaderPeerTarget, "node-someone-else")
	recorder := httptest.NewRecorder()
	f.handler.ServeHTTP(recorder, request)
	if recorder.Code != 409 {
		t.Fatalf("wrong target accepted: %d", recorder.Code)
	}
	fixedQuery := signedPeerRead(t, f, peer, "/api/v1/federation/members")
	if altered := requestPeer(t, f.handler, "/api/v1/federation/members?limit=1", fixedQuery); altered.Code != 403 {
		t.Fatalf("signed directory query was changed: %d", altered.Code)
	}
	if original := requestPeer(t, f.handler, "/api/v1/federation/members", fixedQuery); original.Code != 200 {
		t.Fatalf("rejected query burned the valid request credential: %d", original.Code)
	}
	beforeRevocation := signedPeerRead(t, f, peer, "/api/v1/federation/members")
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+peer.NodeID()+"/revoke", f.adminToken, nil), 200)
	if revoked := requestPeer(t, f.handler, "/api/v1/federation/members", beforeRevocation); revoked.Code != 403 {
		t.Fatalf("unexpired credential bypassed live peer revocation: %d", revoked.Code)
	}
}

func TestPeerMemberContinuationKeepsFrozenPageSizeWhenOmitted(t *testing.T) {
	f := discoveryPGFixture(t)
	first := registerPeerNode(t, f, true, true)
	second := registerPeerNode(t, f, true, true)
	peer := approvedReadPeer(t, f)
	path := "/api/v1/federation/members?limit=1"
	page := decodeDiscovery[discovery.PeerMemberPage](t,
		requestPeer(t, f.handler, path, signedPeerRead(t, f, peer, path)), 200)
	snapshotID := page.SnapshotID
	seen := map[string]bool{}
	for n := 0; ; n++ {
		if n > 2 || page.SnapshotID != snapshotID {
			t.Fatal("member pagination did not terminate in its original snapshot")
		}
		for _, member := range page.Members {
			if seen[member.NodeID] {
				t.Fatal("member repeated across frozen pages")
			}
			seen[member.NodeID] = true
		}
		if page.Complete {
			if len(page.Members) != 0 || page.NextCursor != nil || !seen[first] || !seen[second] || len(seen) != 2 {
				t.Fatalf("terminal page lost or invented members: %+v %v", page, seen)
			}
			break
		}
		if page.NextCursor == nil {
			t.Fatal("nonterminal page omitted its continuation")
		}
		path = "/api/v1/federation/members?snapshot_id=" + snapshotID + "&cursor=" + *page.NextCursor
		page = decodeDiscovery[discovery.PeerMemberPage](t,
			requestPeer(t, f.handler, path, signedPeerRead(t, f, peer, path)), 200)
	}
}

func TestPeerCollectionsProxiesCorpusAndNeverLeaksCallerScope(t *testing.T) {
	f := discoveryPGFixture(t)
	peer := approvedReadPeer(t, f)
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
	w := requestPeer(t, f.handler, "/api/v1/federation/collections", signedPeerRead(t, f, peer, "/api/v1/federation/collections"))
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
	if w := requestPeer(t, f.handler, "/api/v1/federation/collections", signedPeerRead(t, f, peer, "/api/v1/federation/collections")); w.Code != 502 {
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
	if w := requestPeer(t, f.handler, "/api/v1/federation/collections", signedPeerRead(t, f, peer, "/api/v1/federation/collections")); w.Code != 502 {
		t.Fatalf("foreign-origin catalog envelope relayed: %d", w.Code)
	}
}

// Unverified peer reads reveal nothing about membership on any peer route:
// an unknown-issuer self-signed token, a revoked-issuer forged token, and an
// approved-issuer token with a corrupted signature all get the identical
// neutral 401 credential_invalid with no audit row.
func TestPeerUnverifiedReadsShareNeutralDenial(t *testing.T) {
	f := discoveryPGFixture(t)
	victim := approvedReadPeer(t, f)
	membersPath := "/api/v1/federation/members?limit=1"
	local := f.server.nodeIdentity.NodeID()
	subtreePath := "/api/v1/federation/subtree?path=" + local + "," + victim.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+victim.NodeID()+"/revoke", f.adminToken, nil), 200)

	stranger, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "stranger"), true)
	if err != nil {
		t.Fatal(err)
	}
	outsider, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "outsider"), true)
	if err != nil {
		t.Fatal(err)
	}
	forgedMembers := forgedIssuerToken(t, stranger, victim.NodeID(), membersPath, f.server.nodeIdentity.NodeID())

	// Approved issuer, corrupted signature: the issuer hint still parses to a
	// registered key, but verification against that key fails.
	holder := approvedReadPeer(t, f)
	valid := signedPeerRead(t, f, holder, membersPath)
	parts := strings.SplitN(valid, ".", 2)
	if len(parts) != 2 || len(parts[1]) == 0 {
		t.Fatal("signed token has no signature part")
	}
	sig := parts[1]
	// Flip the first signature character (not the last: trailing base64 bits
	// may be padding): the issuer hint still parses against the registered
	// key, but verification must fail.
	first := byte('A')
	if sig[0] == 'A' {
		first = 'B'
	}
	corrupted := parts[0] + "." + string([]byte{first}) + sig[1:]

	decode := func(t *testing.T, w *httptest.ResponseRecorder) (int, string) {
		t.Helper()
		var denied struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &denied); err != nil {
			t.Fatal(err)
		}
		return w.Code, denied.Error.Code
	}
	type probe struct {
		name string
		path string
		make func(path string) string
	}
	probes := []probe{
		{"unknown-issuer", membersPath, func(path string) string { return signedPeerRead(t, f, outsider, path) }},
		{"revoked-forged", membersPath, func(string) string { return forgedMembers }},
		{"bad-signature", membersPath, func(string) string { return corrupted }},
	}
	var wantCode int
	var wantErr string
	for i, p := range probes {
		status, code := decode(t, requestPeer(t, f.handler, p.path, p.make(p.path)))
		if status != 401 || code != "credential_invalid" {
			t.Fatalf("%s members probe: got %d %q, want 401 credential_invalid", p.name, status, code)
		}
		if i == 0 {
			wantCode, wantErr = status, code
		} else if status != wantCode || code != wantErr {
			t.Fatalf("%s denial differs: got %d %q, want %d %q", p.name, status, code, wantCode, wantErr)
		}
	}
	// A second peer route shows the same neutral denial for the forged token.
	subtreeForged := forgedIssuerToken(t, stranger, victim.NodeID(), subtreePath, f.server.nodeIdentity.NodeID())
	status, code := decode(t, requestPeer(t, f.handler, subtreePath, subtreeForged))
	if status != wantCode || code != wantErr {
		t.Fatalf("revoked-forged subtree probe: got %d %q, want %d %q", status, code, wantCode, wantErr)
	}
	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Fatalf("unverified probes minted %d audit rows", len(events))
	}
}
