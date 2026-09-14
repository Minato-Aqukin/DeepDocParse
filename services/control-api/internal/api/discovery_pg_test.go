package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

type discoveryFixture struct {
	server                           *Server
	handler                          http.Handler
	org                              string
	admin, alice, bob                *store.User
	adminToken, aliceToken, bobToken string
}

func discoveryPGFixture(t *testing.T) *discoveryFixture {
	t.Helper()
	dsn := os.Getenv("CONTROL_TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("CONTROL_TEST_DATABASE_URL is required for discovery HTTP permission and snapshot tests")
	}
	ctx := context.Background()
	db, err := store.Open(ctx, dsn, 8, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	org := auth.NewID()
	_, err = db.Pool().Exec(ctx, `INSERT INTO control.organizations(id,name,slug) VALUES($1,$1,$1)`, org)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		db.Pool().Exec(context.Background(), `DELETE FROM control.audit_events WHERE organization_id=$1`, org)
		db.Pool().Exec(context.Background(), `DELETE FROM control.organizations WHERE id=$1`, org)
	})
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "node"), true)
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{cfg: &config.Config{JWTSecret: strings.Repeat("test", 10), PublicBaseURL: "https://center.example", ServiceToken: "internal-test-service"}, store: db, nodeIdentity: node, nodeRevision: 1, defaultOrg: org, sessions: auth.NewSessions(strings.Repeat("test", 10), time.Hour), limiter: ratelimit.NewMemory()}
	fixture := &discoveryFixture{server: s, org: org}
	for _, target := range []struct {
		u     **store.User
		token *string
		role  rbac.Role
	}{{&fixture.admin, &fixture.adminToken, rbac.Admin}, {&fixture.alice, &fixture.aliceToken, rbac.Contributor}, {&fixture.bob, &fixture.bobToken, rbac.Viewer}} {
		u, err := db.CreateUser(ctx, org, auth.NewID(), "", "unused-test-hash", target.role)
		if err != nil {
			t.Fatal(err)
		}
		*target.u = u
		t.Cleanup(func() { db.Pool().Exec(context.Background(), `DELETE FROM control.users WHERE id=$1`, u.ID) })
		token, _, err := s.sessions.Issue(u.ID, org, string(u.Role))
		if err != nil {
			t.Fatal(err)
		}
		*target.token = token
	}
	fixture.handler = s.Routes()
	return fixture
}
func requestDiscovery(t *testing.T, h http.Handler, method, path, token string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var raw []byte
	if body != nil {
		var err error
		raw, err = json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
	}
	r := httptest.NewRequest(method, path, bytes.NewReader(raw))
	r.Header.Set("Content-Type", "application/json")
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	// Exercise the production identity stripping boundary on every test request.
	r.Header.Set(identity.HeaderUser, "forged-admin")
	r.Header.Set(identity.HeaderRole, "admin")
	r.Header.Set(identity.HeaderOrganization, "forged-org")
	r.Header.Set("X-DDP-Client-Scope", "forged-scope")
	r.Header.Set("X-DDP-Authority-Node", "forged-node")
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func decodeDiscovery[T any](t *testing.T, w *httptest.ResponseRecorder, status int) T {
	t.Helper()
	if w.Code != status {
		t.Fatalf("status %d: %s", w.Code, w.Body.String())
	}
	var out T
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}
func remoteRegistration(t *testing.T, visible bool) discovery.Registration {
	t.Helper()
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "remote"), true)
	if err != nil {
		t.Fatal(err)
	}
	return discovery.Registration{PublicKey: node.PublicKey(), VisibleToOrg: visible, Descriptor: discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: node.NodeID(), ProtocolVersions: []string{"ddp-discovery/1"}, ControlledEndpoints: []discovery.Endpoint{{Purpose: "federation", URL: "https://remote.example/api/v1/federation"}}, AuthMethods: []string{"future_node_credentials"}, Revision: 1, ValidUntil: time.Now().UTC().Add(time.Hour)}}
}
func TestDiscoveryHTTPAdminApprovalIsolationAndRevocation(t *testing.T) {
	f := discoveryPGFixture(t)
	bobBefore := decodeDiscovery[discovery.Snapshot](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/member-snapshots", f.bobToken, map[string]any{"page_size": 1}), 201)
	registration := remoteRegistration(t, false)
	registration.AllowedSubjects = []string{f.alice.ID}
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.aliceToken, registration); w.Code != 403 {
		t.Fatalf("ordinary user registered endpoint: %d", w.Code)
	}
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration); w.Code != 201 {
		t.Fatalf("admin register: %s", w.Body)
	}
	create := func(token string) discovery.Snapshot {
		return decodeDiscovery[discovery.Snapshot](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/member-snapshots", token, map[string]any{"page_size": 1}), 201)
	}
	pending := create(f.aliceToken)
	if pending.FirstCursor != pending.TerminalCursor {
		t.Fatal("pending member discoverable")
	}
	nodePath := "/api/v1/federation/nodes/" + registration.Descriptor.NodeID
	if w := requestDiscovery(t, f.handler, "POST", nodePath+"/approve", f.bobToken, nil); w.Code != 403 {
		t.Fatalf("viewer approved: %d", w.Code)
	}
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", nodePath+"/approve", f.adminToken, nil), 200)
	alice := create(f.aliceToken)
	bob := create(f.bobToken)
	if bob.FirstCursor != bob.TerminalCursor || bob.RegistryRevision != bobBefore.RegistryRevision {
		t.Fatal("hidden member page/count or mutation count revealed")
	}
	if alice.RegistryRevision != pending.RegistryRevision+1 {
		t.Fatal("visible approval did not advance the caller's revision")
	}
	membersPath := "/api/v1/federation/member-snapshots/" + alice.ID + "/members"
	if w := requestDiscovery(t, f.handler, "GET", membersPath, f.bobToken, nil); w.Code != 404 {
		t.Fatalf("cross-user snapshot: %d", w.Code)
	}
	page := decodeDiscovery[discovery.Page](t, requestDiscovery(t, f.handler, "GET", membersPath, f.aliceToken, nil), 200)
	if len(page.Members) != 1 || page.Members[0].Health != "unknown" || page.Members[0].AcceptingAdmissions {
		t.Fatalf("fake readiness: %+v", page)
	}
	if page.Members[0].Route.Path[0] != f.server.nodeIdentity.NodeID() || page.Members[0].Route.NextHopNodeID != registration.Descriptor.NodeID {
		t.Fatal("unmanaged route")
	}
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", nodePath+"/revoke", f.adminToken, nil), 200)
	if after := create(f.bobToken); after.RegistryRevision != bobBefore.RegistryRevision {
		t.Fatal("hidden revocation leaked through caller-visible revision")
	}
	revoked := decodeDiscovery[discovery.Page](t, requestDiscovery(t, f.handler, "GET", membersPath, f.aliceToken, nil), 200)
	if len(revoked.Members) != 1 || revoked.Members[0].State != "revoked" || revoked.Members[0].Descriptor != nil {
		t.Fatalf("revocation dropped denominator or disclosed endpoint %+v", revoked)
	}
	_, err := f.server.store.Pool().Exec(context.Background(), `UPDATE control.member_snapshots SET expires_at=now()-interval '1 second' WHERE id=$1`, alice.ID)
	if err != nil {
		t.Fatal(err)
	}
	if w := requestDiscovery(t, f.handler, "GET", membersPath, f.aliceToken, nil); w.Code != 410 {
		t.Fatalf("expiry did not fail explicitly: %d", w.Code)
	}
}

func TestDiscoveryHTTPSnapshotBindsCredentialAndFreshScope(t *testing.T) {
	f := discoveryPGFixture(t)
	ctx := context.Background()
	key, plain, err := f.server.store.CreateAPIKey(ctx, f.org, f.alice.ID, "first", []rbac.Scope{rbac.ScopeRead}, nil, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	_, otherKey, err := f.server.store.CreateAPIKey(ctx, f.org, f.alice.ID, "second", []rbac.Scope{rbac.ScopeRead}, nil, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	snap := decodeDiscovery[discovery.Snapshot](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/member-snapshots", plain, map[string]any{"page_size": 1}), 201)
	path := "/api/v1/federation/member-snapshots/" + snap.ID + "/members"
	for _, token := range []string{f.aliceToken, otherKey, f.bobToken} {
		if w := requestDiscovery(t, f.handler, "GET", path, token, nil); w.Code != 404 {
			t.Fatalf("snapshot crossed credential or principal boundary: %d", w.Code)
		}
	}
	decodeDiscovery[discovery.Page](t, requestDiscovery(t, f.handler, "GET", path, plain, nil), 200)
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/member-snapshots", plain, map[string]any{"caller_scope_hash": snap.CallerScopeHash}); w.Code != 400 {
		t.Fatalf("caller supplied its own scope hash: %d", w.Code)
	}
	// Authentication rereads key scopes, invalidating old snapshots even when read remains.
	if _, err = f.server.store.Pool().Exec(ctx, `UPDATE control.api_keys SET scopes=$2 WHERE id=$1`, key.ID, []string{"read", "parse"}); err != nil {
		t.Fatal(err)
	}
	if w := requestDiscovery(t, f.handler, "GET", path, plain, nil); w.Code != 404 {
		t.Fatalf("changed credential scope retained old snapshot: %d", w.Code)
	}
	if _, err = f.server.store.Pool().Exec(ctx, `UPDATE control.api_keys SET revoked_at=now() WHERE id=$1`, key.ID); err != nil {
		t.Fatal(err)
	}
	if w := requestDiscovery(t, f.handler, "GET", path, plain, nil); w.Code != 401 {
		t.Fatalf("revoked credential retained snapshot access: %d", w.Code)
	}
}
func TestDiscoveryHTTPHandshakeIdentityAndCapabilitiesAreAuthenticated(t *testing.T) {
	f := discoveryPGFixture(t)
	public := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/node", "", nil), 200)
	if public["authority_node_id"] != f.server.nodeIdentity.NodeID() || strings.Contains(fmt.Sprint(public), f.org) || strings.Contains(fmt.Sprint(public), f.alice.ID) {
		t.Fatal("public handshake exposes workspace/principal or wrong authority")
	}
	for _, path := range []string{"/api/v1/capabilities", "/api/v1/client/handshake", "/api/v1/federation/capabilities"} {
		if w := requestDiscovery(t, f.handler, "GET", path, "", nil); w.Code != 401 {
			t.Fatalf("unauthenticated handshake %s: %d", path, w.Code)
		}
		body := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", path, f.aliceToken, nil), 200)
		if body["capability_status"] != "unknown" || len(body["capabilities"].([]any)) != 0 || body["accepting_admissions"] != false {
			t.Fatalf("unimplemented producer advertised ready: %+v", body)
		}
		id := body["identity"].(map[string]any)
		profile := body["profile"].(map[string]any)
		if id["workspace_id"] != f.org || id["authority_node_id"] != f.server.nodeIdentity.NodeID() || profile["subject"] != f.alice.ID {
			t.Fatalf("forged header changed authenticated identity: %+v", body)
		}
	}
	key, plain, err := f.server.store.CreateAPIKey(context.Background(), f.org, f.alice.ID, "read", []rbac.Scope{rbac.ScopeRead}, nil, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	body := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", "/api/v1/capabilities", plain, nil), 200)
	if body["profile"].(map[string]any)["subject"] != f.alice.ID {
		t.Fatal("API key ID substituted for user principal")
	}
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", plain, remoteRegistration(t, true)); w.Code != 401 {
		t.Fatalf("key managed directory: %d", w.Code)
	}
	_, err = f.server.store.Pool().Exec(context.Background(), `UPDATE control.api_keys SET revoked_at=now() WHERE id=$1`, key.ID)
	if err != nil {
		t.Fatal(err)
	}
	if w := requestDiscovery(t, f.handler, "GET", "/api/v1/capabilities", plain, nil); w.Code != 401 {
		t.Fatalf("revoked key handshake: %d", w.Code)
	}
}
func TestDiscoveryHTTPProducerNoRedirectAndUnknownWhenUnavailable(t *testing.T) {
	f := discoveryPGFixture(t)
	var status atomic.Int32
	status.Store(200)
	var calls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/internal/client/protocol" {
			json.NewEncoder(w).Encode(map[string]any{"protocol_version": "ddp-client/1", "capabilities": []string{}})
			return
		}
		calls.Add(1)
		if r.URL.Path != "/internal/capabilities" || r.Header.Get(identity.HeaderActorKind) != "service" || r.Header.Get(identity.HeaderActor) != "control-api" || r.Header.Get(identity.HeaderUser) != "" || r.Header.Get("Authorization") != "Bearer internal-test-service" {
			t.Error("wrong fixed endpoint or authority forwarded")
		}
		if status.Load() == 302 {
			w.Header().Set("Location", "/must-not-follow")
		}
		w.WriteHeader(int(status.Load()))
		body := map[string]any{"capability_status": "observed", "profiles": []any{map[string]any{"schema": "ddp-discovery/1#CapabilityProfile", "operation": "doc.parse", "configured": true, "readiness": "ready", "accepting_admissions": true, "observed_at": time.Now().UTC(), "valid_until": time.Now().UTC().Add(time.Minute), "internal_secret": "must-not-echo"}}}
		json.NewEncoder(w).Encode(body)
	}))
	defer target.Close()
	f.server.cfg.CorpusURL = target.URL
	f.server.corpus, _ = proxy.New("corpus", target.URL, f.server.cfg.ServiceToken)
	good := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", "/api/v1/capabilities", f.aliceToken, nil), 200)
	if good["capability_status"] != "observed" || strings.Contains(fmt.Sprint(good), "must-not-echo") {
		t.Fatalf("bad capability projection %+v", good)
	}
	profiles := good["profiles"].([]any)
	p := profiles[0].(map[string]any)
	if p["readiness"] != "ready" || p["accepting_admissions"] != true || p["node_id"] != f.server.nodeIdentity.NodeID() {
		t.Fatalf("configuration, health, admission conflated %+v", p)
	}
	if good["accepting_admissions"] != true {
		t.Fatalf("observed producer acceptance was not rolled up: %+v", good)
	}
	for _, code := range []int32{302, 404, 500} {
		status.Store(code)
		bad := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", "/api/v1/capabilities", f.aliceToken, nil), 200)
		if bad["capability_status"] != "unknown" || len(bad["profiles"].([]any)) != 0 {
			t.Fatalf("unavailable producer advertises ready %+v", bad)
		}
	}
	if calls.Load() != 4 {
		t.Fatal("followed redirect or reused stale capability")
	}
}

func TestDiscoveryRegistrationNeverConnectsToDeclaredEndpoint(t *testing.T) {
	f := discoveryPGFixture(t)
	var calls atomic.Int32
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(200) }))
	defer endpoint.Close()
	registration := remoteRegistration(t, true)
	registration.Descriptor.ControlledEndpoints[0].URL = endpoint.URL
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration), 201)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+registration.Descriptor.NodeID+"/approve", f.adminToken, nil), 200)
	if calls.Load() != 0 {
		t.Fatal("registration or approval sent an unauthorized probe")
	}
	// A descriptor for this authority cannot be registered as its own child.
	registration.PublicKey = f.server.nodeIdentity.PublicKey()
	registration.Descriptor.NodeID = f.server.nodeIdentity.NodeID()
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration); w.Code != 400 {
		t.Fatalf("self cycle accepted: %d", w.Code)
	}
}

func TestDiscoveryRegistrationRequiresExplicitEnumerationContract(t *testing.T) {
	f := discoveryPGFixture(t)
	registration := remoteRegistration(t, true)
	raw, _ := json.Marshal(registration)
	var body map[string]any
	json.Unmarshal(raw, &body)
	descriptor := body["descriptor"].(map[string]any)
	for _, capabilities := range []any{map[string]any{}, map[string]any{"enumerate_members": nil}, nil} {
		descriptor["discovery_capabilities"] = capabilities
		if w := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, body); w.Code != 400 {
			t.Fatalf("undeclared subtree accepted: %d", w.Code)
		}
	}
}
