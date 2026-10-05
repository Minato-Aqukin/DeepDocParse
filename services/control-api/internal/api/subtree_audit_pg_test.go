package api

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// The live issuer re-check on frozen subtree snapshot pages (peer credential
// middleware: current approval/revocation state, not the frozen snapshot)
// already denies reads from revoked issuers. The denial must also leave an
// audit record carrying issuer, organization, snapshot id and reason.
// Successes stay unaudited, mirroring peer directory/catalog reads today.
func TestPeerSubtreeSnapshotRevokedIssuerDenialIsAudited(t *testing.T) {
	f := discoveryPGFixture(t)
	issuer := approvedReadPeer(t, f)
	local := f.server.nodeIdentity.NodeID()
	base := "/api/v1/federation/subtree?path=" + local + "," + issuer.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	first := decodeDiscovery[discovery.SubtreePage](t, requestPeer(t, f.handler, base, signedPeerRead(t, f, issuer, base)), 200)
	if first.SnapshotID == "" || first.FirstCursor == "" {
		t.Fatalf("subtree snapshot not frozen: %+v", first)
	}
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+issuer.NodeID()+"/revoke", f.adminToken, nil), 200)

	continuation := base + "&snapshot_id=" + first.SnapshotID + "&cursor=" + first.FirstCursor
	w := requestPeer(t, f.handler, continuation, signedPeerRead(t, f, issuer, continuation))
	if w.Code != 403 {
		t.Fatalf("revoked issuer read frozen page: %d %s", w.Code, w.Body.String())
	}
	var denied struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &denied); err != nil {
		t.Fatal(err)
	}
	if denied.Error.Code != "node_revoked" {
		t.Fatalf("denial code changed: %q (%s)", denied.Error.Code, w.Body.String())
	}

	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("expected exactly one peer denial audit, got %d", len(events))
	}
	got := events[0]
	if got.ActorID == nil || *got.ActorID != issuer.NodeID() {
		t.Fatalf("audit actor %v, want issuer %s", got.ActorID, issuer.NodeID())
	}
	if got.Target == nil || *got.Target != first.SnapshotID {
		t.Fatalf("audit target %v, want snapshot %s", got.Target, first.SnapshotID)
	}
	var detail struct {
		Issuer   string `json:"issuer_node_id"`
		Snapshot string `json:"snapshot_id"`
		Reason   string `json:"reason"`
	}
	if err := json.Unmarshal(got.Detail, &detail); err != nil {
		t.Fatal(err)
	}
	if detail.Issuer != issuer.NodeID() || detail.Snapshot != first.SnapshotID || detail.Reason != "node_revoked" {
		t.Fatalf("audit detail missing issuer/snapshot/reason: %s", got.Detail)
	}
}

// forgedIssuerToken builds a token whose issuer hint names claimedIssuer but
// whose signature cannot verify under that issuer's registered key: the
// stranger signs for its own node id, then the raw payload's issuer field is
// rewritten to claimedIssuer (breaking the signature over those bytes).
func forgedIssuerToken(t *testing.T, stranger *discovery.Identity, claimedIssuer, path, audience string) string {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	jti, err := discovery.NewCredentialJTI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	token, err := stranger.SignCredential(discovery.CredentialClaims{
		Schema: discovery.CredentialSchema, Alg: discovery.CredentialAlg,
		IssuerNodeID: stranger.NodeID(), AudienceNodeID: audience,
		Actor:       discovery.CredentialActor{OrganizationID: "foreign-org", Subject: "control-api", Kind: "service"},
		Operation:   "directory_subtree_read",
		Constraints: discovery.CredentialConstraints{RootTaskID: "directory:" + jti, ScopeRef: peerReadDigest(r.URL.Query().Encode())},
		Request:     discovery.CredentialRequest{Method: "GET", Path: r.URL.Path, BodyDigest: peerReadDigest("")},
		IssuedAt:    now, ExpiresAt: now + 60, JTI: jti,
	})
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.SplitN(token, ".", 2)
	if len(parts) != 2 {
		t.Fatal("signed token has no payload part")
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		t.Fatal(err)
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		t.Fatal(err)
	}
	claims["issuer_node_id"] = claimedIssuer
	// Re-encode without canonicalization: any byte change breaks the
	// signature, and the issuer hint now names the revoked node.
	raw, err := json.Marshal(claims)
	if err != nil {
		t.Fatal(err)
	}
	return base64.RawURLEncoding.EncodeToString(raw) + "." + parts[1]
}

// An unauthenticated caller must not be able to mint audit rows attributed to
// a revoked node: a forged credential naming the revoked issuer, and a
// credential naming an unknown issuer, both keep today's denial (status +
// code) and write ZERO audit rows. Only a signature-verified credential from
// the claimed issuer may attribute an audit row to it.
func TestPeerTrustRefusalWithoutValidSignatureWritesNoAudit(t *testing.T) {
	f := discoveryPGFixture(t)
	issuer := approvedReadPeer(t, f)
	local := f.server.nodeIdentity.NodeID()
	base := "/api/v1/federation/subtree?path=" + local + "," + issuer.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	first := decodeDiscovery[discovery.SubtreePage](t, requestPeer(t, f.handler, base, signedPeerRead(t, f, issuer, base)), 200)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+issuer.NodeID()+"/revoke", f.adminToken, nil), 200)
	continuation := base + "&snapshot_id=" + first.SnapshotID + "&cursor=" + first.FirstCursor

	stranger, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "stranger"), true)
	if err != nil {
		t.Fatal(err)
	}
	w := requestPeer(t, f.handler, continuation, forgedIssuerToken(t, stranger, issuer.NodeID(), continuation, f.server.nodeIdentity.NodeID()))
	if w.Code != 403 {
		t.Fatalf("forged revoked-issuer read: %d %s", w.Code, w.Body.String())
	}
	var denied struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &denied); err != nil {
		t.Fatal(err)
	}
	if denied.Error.Code != "node_revoked" {
		t.Fatalf("forgery denial code changed: %q (%s)", denied.Error.Code, w.Body.String())
	}
	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Fatalf("forged credential minted %d audit rows attributed to the revoked issuer", len(events))
	}

	// Unknown issuer: no trust record at all → same denial, still no audit.
	outsider, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "outsider"), true)
	if err != nil {
		t.Fatal(err)
	}
	unknownPath := "/api/v1/federation/subtree?path=" + local + "," + outsider.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	u := requestPeer(t, f.handler, unknownPath, signedPeerRead(t, f, outsider, unknownPath))
	if u.Code != 403 {
		t.Fatalf("unknown issuer read: %d %s", u.Code, u.Body.String())
	}
	events, err = f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Fatalf("unknown-issuer credential minted %d audit rows", len(events))
	}
}

// signedPeerReadExpired signs a well-formed subtree credential whose validity
// window has already passed. It verifies under the issuer's registered key
// (provenance is fine) but fails the expiry check the success path applies.
func signedPeerReadExpired(t *testing.T, f *discoveryFixture, node *discovery.Identity, path string) string {
	t.Helper()
	r := httptest.NewRequest(http.MethodGet, path, nil)
	jti, err := discovery.NewCredentialJTI()
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().Unix()
	token, err := node.SignCredential(discovery.CredentialClaims{
		Schema: discovery.CredentialSchema, Alg: discovery.CredentialAlg,
		IssuerNodeID: node.NodeID(), AudienceNodeID: f.server.nodeIdentity.NodeID(),
		Actor:       discovery.CredentialActor{OrganizationID: "foreign-org", Subject: "control-api", Kind: "service"},
		Operation:   "directory_subtree_read",
		Constraints: discovery.CredentialConstraints{RootTaskID: "directory:" + jti, ScopeRef: peerReadDigest(r.URL.Query().Encode())},
		Request:     discovery.CredentialRequest{Method: "GET", Path: r.URL.Path, BodyDigest: peerReadDigest("")},
		IssuedAt:    now - 120, ExpiresAt: now - 60, JTI: jti,
	})
	if err != nil {
		t.Fatal(err)
	}
	return token
}

// Three rapid snapshot-page reads with fresh JTIs from a validly-signed
// revoked issuer are each refused as today but write exactly ONE audit row:
// the per-(organization, issuer) 60s window bounds persistent write
// amplification from captured or re-minted tokens.
func TestPeerDenialAuditBurstsWriteOneRow(t *testing.T) {
	f := discoveryPGFixture(t)
	issuer := approvedReadPeer(t, f)
	local := f.server.nodeIdentity.NodeID()
	base := "/api/v1/federation/subtree?path=" + local + "," + issuer.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	first := decodeDiscovery[discovery.SubtreePage](t, requestPeer(t, f.handler, base, signedPeerRead(t, f, issuer, base)), 200)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+issuer.NodeID()+"/revoke", f.adminToken, nil), 200)
	continuation := base + "&snapshot_id=" + first.SnapshotID + "&cursor=" + first.FirstCursor
	for i := range 3 {
		w := requestPeer(t, f.handler, continuation, signedPeerRead(t, f, issuer, continuation))
		if w.Code != 403 {
			t.Fatalf("burst read %d: %d %s", i, w.Code, w.Body.String())
		}
	}
	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("burst of 3 refused reads wrote %d audit rows, want exactly 1", len(events))
	}
}

// Eight concurrent snapshot-page reads with fresh JTIs from a validly-signed
// revoked issuer are each refused as today but write exactly ONE audit row:
// the advisory-lock serialization makes the per-(organization, issuer) 60s
// bound exact under READ COMMITTED instead of merely approximate.
func TestPeerDenialAuditConcurrentBurstsWriteOneRow(t *testing.T) {
	f := discoveryPGFixture(t)
	issuer := approvedReadPeer(t, f)
	local := f.server.nodeIdentity.NodeID()
	base := "/api/v1/federation/subtree?path=" + local + "," + issuer.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	first := decodeDiscovery[discovery.SubtreePage](t, requestPeer(t, f.handler, base, signedPeerRead(t, f, issuer, base)), 200)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+issuer.NodeID()+"/revoke", f.adminToken, nil), 200)
	continuation := base + "&snapshot_id=" + first.SnapshotID + "&cursor=" + first.FirstCursor
	tokens := make([]string, 8)
	for i := range tokens {
		tokens[i] = signedPeerRead(t, f, issuer, continuation)
	}
	start := make(chan struct{})
	type outcome struct {
		code int
		body string
	}
	results := make([]outcome, len(tokens))
	var wg sync.WaitGroup
	for i := range tokens {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			w := requestPeer(t, f.handler, continuation, tokens[i])
			results[i] = outcome{code: w.Code, body: w.Body.String()}
		}(i)
	}
	close(start)
	wg.Wait()
	for i, res := range results {
		if res.code != 403 {
			t.Fatalf("concurrent read %d: %d %s", i, res.code, res.body)
		}
	}
	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 {
		t.Fatalf("8 concurrent refused reads wrote %d audit rows, want exactly 1", len(events))
	}
}

// A validly-signed but expired token from the revoked issuer keeps today's
// refusal and writes ZERO audit rows: expiry failure must not mint persistent
// rows, or a captured token could be replayed forever.
func TestPeerDenialAuditExpiredTokenWritesNoRow(t *testing.T) {
	f := discoveryPGFixture(t)
	issuer := approvedReadPeer(t, f)
	local := f.server.nodeIdentity.NodeID()
	base := "/api/v1/federation/subtree?path=" + local + "," + issuer.NodeID() + "&max_requests=10&max_nodes=2&limit=1"
	first := decodeDiscovery[discovery.SubtreePage](t, requestPeer(t, f.handler, base, signedPeerRead(t, f, issuer, base)), 200)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+issuer.NodeID()+"/revoke", f.adminToken, nil), 200)
	continuation := base + "&snapshot_id=" + first.SnapshotID + "&cursor=" + first.FirstCursor
	w := requestPeer(t, f.handler, continuation, signedPeerReadExpired(t, f, issuer, continuation))
	if w.Code == 200 {
		t.Fatalf("expired revoked-issuer token accepted: %s", w.Body.String())
	}
	events, err := f.server.store.AuditEvents(context.Background(), f.org, "peer.credential_denied", nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 0 {
		t.Fatalf("expired token minted %d audit rows", len(events))
	}
}
