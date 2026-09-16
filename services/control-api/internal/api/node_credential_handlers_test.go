package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

const credentialTestServiceToken = "node-credential-test-service-token"

// fakeTrust is an in-memory membership directory with the store's semantics:
// rows are organization scoped, unknown is ErrNotFound, state is returned as is.
type fakeTrust struct {
	rows  map[string]store.PeerTrustRecord // key: org + "/" + node
	calls int
}

func (f *fakeTrust) PeerTrust(_ context.Context, org, node string) (*store.PeerTrustRecord, error) {
	f.calls++
	row, ok := f.rows[org+"/"+node]
	if !ok {
		return nil, store.ErrNotFound
	}
	return &row, nil
}

type credentialFixture struct {
	server   *Server
	mux      *http.ServeMux
	trust    *fakeTrust
	remote   *discovery.Identity
	org      string
	issuedAt time.Time
}

func newCredentialFixture(t *testing.T) *credentialFixture {
	t.Helper()
	local, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "local"), true)
	if err != nil {
		t.Fatal(err)
	}
	remote, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "remote"), true)
	if err != nil {
		t.Fatal(err)
	}
	org := "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
	trust := &fakeTrust{rows: map[string]store.PeerTrustRecord{
		org + "/" + remote.NodeID(): {NodeID: remote.NodeID(), PublicKey: remote.PublicKey(), State: "approved", Revision: 4},
	}}
	issued := time.Date(2026, 9, 15, 8, 0, 0, 0, time.UTC)
	s := &Server{cfg: &config.Config{ServiceToken: credentialTestServiceToken}, nodeIdentity: local,
		defaultOrg: org, trust: trust, now: func() time.Time { return issued }}
	mux := http.NewServeMux()
	s.mountNodeCredentials(mux)
	return &credentialFixture{server: s, mux: mux, trust: trust, remote: remote, org: org, issuedAt: issued}
}

func (f *credentialFixture) signBody(over func(map[string]any)) map[string]any {
	body := map[string]any{
		"audience_node_id": f.remote.NodeID(),
		"actor":            map[string]any{"organization_id": f.org, "subject": "user-1", "kind": "user"},
		"operation":        "execution_read",
		"constraints":      map[string]any{"root_task_id": "root-1"},
		"request": map[string]any{"method": "GET", "path": "/api/v1/federation/tasks/exec-1",
			"body_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
		"ttl_seconds": 60,
	}
	if over != nil {
		over(body)
	}
	return body
}

func (f *credentialFixture) do(t *testing.T, method, path string, body any, token string) *httptest.ResponseRecorder {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatal(err)
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	r := httptest.NewRequest(method, path, reader)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	f.mux.ServeHTTP(w, r)
	return w
}

func errorCode(t *testing.T, w *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	return body.Error.Code
}

func TestNodeCredentialIssuanceSignsOnlyForApprovedAudiencesWithControlChosenFields(t *testing.T) {
	f := newCredentialFixture(t)
	w := f.do(t, "POST", "/internal/federation/node-credentials", f.signBody(nil), credentialTestServiceToken)
	out := decodeDiscovery[struct {
		Credential   string `json:"credential"`
		IssuerNodeID string `json:"issuer_node_id"`
		JTI          string `json:"jti"`
		ExpiresAt    int64  `json:"expires_at"`
	}](t, w, 200)
	if w.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("a credential response must not be cacheable")
	}
	claims, err := discovery.VerifyCredential(out.Credential, f.server.nodeIdentity.PublicKey())
	if err != nil {
		t.Fatalf("issued credential does not verify with this node's key: %v", err)
	}
	if claims.IssuerNodeID != f.server.nodeIdentity.NodeID() || out.IssuerNodeID != claims.IssuerNodeID ||
		claims.AudienceNodeID != f.remote.NodeID() || claims.JTI != out.JTI || claims.ExpiresAt != out.ExpiresAt ||
		claims.IssuedAt != f.issuedAt.Unix() || claims.ExpiresAt-claims.IssuedAt != 60 {
		t.Fatalf("issuer/time/jti must be chosen by control: %+v", claims)
	}
	again := decodeDiscovery[struct {
		JTI string `json:"jti"`
	}](t, f.do(t, "POST", "/internal/federation/node-credentials", f.signBody(nil), credentialTestServiceToken), 200)
	if again.JTI == out.JTI {
		t.Fatal("two issuances reused a jti; single-use credentials need fresh ids")
	}
	// The signed credential is for the remote node only: it does not verify as
	// the remote node's own signature.
	if _, err := discovery.VerifyCredential(out.Credential, f.remote.PublicKey()); err == nil {
		t.Fatal("credential verified under the audience key")
	}
}

func TestNodeCredentialIssuanceRefusals(t *testing.T) {
	f := newCredentialFixture(t)
	pending, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "pending"), true)
	if err != nil {
		t.Fatal(err)
	}
	revoked, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "revoked"), true)
	if err != nil {
		t.Fatal(err)
	}
	f.trust.rows[f.org+"/"+pending.NodeID()] = store.PeerTrustRecord{NodeID: pending.NodeID(), PublicKey: pending.PublicKey(), State: "pending", Revision: 1}
	f.trust.rows[f.org+"/"+revoked.NodeID()] = store.PeerTrustRecord{NodeID: revoked.NodeID(), PublicKey: revoked.PublicKey(), State: "revoked", Revision: 2}
	f.trust.rows["other-org/"+pending.NodeID()] = store.PeerTrustRecord{NodeID: pending.NodeID(), PublicKey: pending.PublicKey(), State: "approved", Revision: 1}

	cases := []struct {
		name   string
		token  string
		mutate func(map[string]any)
		status int
		code   string
	}{
		{"no_service_token", "", nil, 401, "invalid_service_token"},
		{"wrong_service_token", "wrong-service-token-000", nil, 401, "invalid_service_token"},
		{"unknown_audience", credentialTestServiceToken, func(b map[string]any) { b["audience_node_id"] = "node-" + strings.Repeat("e", 48) }, 403, "node_unknown"},
		{"pending_audience_even_if_approved_elsewhere", credentialTestServiceToken, func(b map[string]any) { b["audience_node_id"] = pending.NodeID() }, 403, "node_unknown"},
		{"revoked_audience", credentialTestServiceToken, func(b map[string]any) { b["audience_node_id"] = revoked.NodeID() }, 403, "node_revoked"},
		{"self_audience", credentialTestServiceToken, func(b map[string]any) { b["audience_node_id"] = f.server.nodeIdentity.NodeID() }, 400, "credential_invalid"},
		{"foreign_actor_org", credentialTestServiceToken, func(b map[string]any) {
			b["actor"] = map[string]any{"organization_id": "other-org", "subject": "user-1", "kind": "user"}
		}, 403, "credential_scope_denied"},
		{"ttl_over_limit", credentialTestServiceToken, func(b map[string]any) { b["ttl_seconds"] = 121 }, 400, "credential_invalid"},
		{"ttl_zero", credentialTestServiceToken, func(b map[string]any) { b["ttl_seconds"] = 0 }, 400, "credential_invalid"},
		{"caller_supplied_issuer", credentialTestServiceToken, func(b map[string]any) { b["issuer_node_id"] = f.remote.NodeID() }, 400, "credential_invalid"},
		{"caller_supplied_jti", credentialTestServiceToken, func(b map[string]any) { b["jti"] = strings.Repeat("A", 22) }, 400, "credential_invalid"},
		{"unknown_operation", credentialTestServiceToken, func(b map[string]any) { b["operation"] = "admin" }, 400, "credential_invalid"},
		{"forwarded_role", credentialTestServiceToken, func(b map[string]any) {
			b["actor"] = map[string]any{"organization_id": f.org, "subject": "user-1", "kind": "user", "role": "admin"}
		}, 400, "credential_invalid"},
		{"admission_without_step", credentialTestServiceToken, func(b map[string]any) {
			b["operation"] = "admission_create"
			b["request"] = map[string]any{"method": "POST", "path": "/api/v1/federation/admissions", "body_digest": "sha256:" + strings.Repeat("a", 64)}
		}, 400, "credential_invalid"},
		{"internal_path", credentialTestServiceToken, func(b map[string]any) {
			b["request"] = map[string]any{"method": "GET", "path": "/internal/capabilities", "body_digest": "sha256:" + strings.Repeat("a", 64)}
		}, 400, "credential_invalid"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			before := f.trust.calls
			w := f.do(t, "POST", "/internal/federation/node-credentials", f.signBody(c.mutate), c.token)
			if w.Code != c.status || errorCode(t, w) != c.code {
				t.Fatalf("got %d %s, want %d %s: %s", w.Code, errorCode(t, w), c.status, c.code, w.Body.String())
			}
			if strings.Contains(w.Body.String(), "credential\":") {
				t.Fatal("a refusal leaked a credential")
			}
			if c.status == 401 && f.trust.calls != before {
				t.Fatal("membership was consulted before service authentication")
			}
		})
	}
}

func TestPeerKeyLookupReturnsStateAndLocalAuthority(t *testing.T) {
	f := newCredentialFixture(t)
	f.trust.rows[f.org+"/"+"node-revoked-peer"] = store.PeerTrustRecord{NodeID: "node-revoked-peer", PublicKey: f.remote.PublicKey(), State: "revoked", Revision: 9}
	out := decodeDiscovery[map[string]any](t, f.do(t, "GET", "/internal/federation/peer-keys/"+f.remote.NodeID(), nil, credentialTestServiceToken), 200)
	want := map[string]any{"node_id": f.remote.NodeID(), "state": "approved", "public_key": f.remote.PublicKey(),
		"key_fingerprint": f.remote.Fingerprint(), "organization_id": f.org,
		"authority_node_id": f.server.nodeIdentity.NodeID(), "revision": float64(4)}
	for key, value := range want {
		if out[key] != value {
			t.Fatalf("%s = %v, want %v (%v)", key, out[key], value, out)
		}
	}
	if len(out) != len(want) {
		t.Fatalf("peer trust record grew fields outside the contract: %v", out)
	}
	revoked := decodeDiscovery[map[string]any](t, f.do(t, "GET", "/internal/federation/peer-keys/node-revoked-peer", nil, credentialTestServiceToken), 200)
	if revoked["state"] != "revoked" {
		t.Fatal("revoked member must be reported with its state, not hidden")
	}
	if w := f.do(t, "GET", "/internal/federation/peer-keys/node-"+strings.Repeat("7", 48), nil, credentialTestServiceToken); w.Code != 404 || errorCode(t, w) != "node_unknown" {
		t.Fatalf("unknown member: %d %s", w.Code, w.Body.String())
	}
	if w := f.do(t, "GET", "/internal/federation/peer-keys/"+f.remote.NodeID(), nil, ""); w.Code != 401 {
		t.Fatalf("peer keys readable without service credentials: %d", w.Code)
	}
	identity := decodeDiscovery[map[string]string](t, f.do(t, "GET", "/internal/federation/identity", nil, credentialTestServiceToken), 200)
	if identity["node_id"] != f.server.nodeIdentity.NodeID() || identity["public_key"] != f.server.nodeIdentity.PublicKey() || len(identity) != 3 {
		t.Fatalf("identity response: %v", identity)
	}
	if w := f.do(t, "GET", "/internal/federation/identity", nil, ""); w.Code != 401 {
		t.Fatalf("identity readable without service credentials: %d", w.Code)
	}
}
