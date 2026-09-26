package discovery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

// The collector tests use a real signer but isolate membership policy from the
// HTTP traversal. Receiver approval/revocation/replay are exercised against PG.
func peerTestSigner(t *testing.T) (PeerSigner, *Identity) {
	t.Helper()
	issuer, err := LoadIdentity(filepath.Join(t.TempDir(), "identity"), true)
	if err != nil {
		t.Fatal(err)
	}
	digest := func(value string) string {
		sum := sha256.Sum256([]byte(value))
		return "sha256:" + hex.EncodeToString(sum[:])
	}
	return func(_ context.Context, cfg PeerConfig, request *http.Request) error {
		jti, err := NewCredentialJTI()
		if err != nil {
			return err
		}
		op := "directory_members_read"
		if request.URL.Path == "/api/v1/federation/collections" {
			op = "directory_collections_read"
		}
		now := time.Now().Unix()
		token, err := issuer.SignCredential(CredentialClaims{
			Schema: CredentialSchema, Alg: CredentialAlg, IssuerNodeID: issuer.NodeID(), AudienceNodeID: cfg.NodeID,
			Actor:     CredentialActor{OrganizationID: "test-org", Subject: "control-api", Kind: "service"},
			Operation: op, Constraints: CredentialConstraints{RootTaskID: "directory:" + jti, ScopeRef: digest(request.URL.Query().Encode())},
			Request:  CredentialRequest{Method: request.Method, Path: request.URL.Path, BodyDigest: digest("")},
			IssuedAt: now, ExpiresAt: now + 60, JTI: jti,
		})
		if err == nil {
			request.Header.Set(HeaderNodeCredential, token)
		}
		return err
	}, issuer
}

func TestParsePeersFailsClosedOnMalformedDirectory(t *testing.T) {
	if peers, err := ParsePeers("", false); err != nil || len(peers) != 0 {
		t.Fatalf("empty directory must be empty, not an error: %v %v", peers, err)
	}
	for name, raw := range map[string]string{
		"invalid_json": `{`, "not_an_object": `[]`,
		"invalid_node_id":       `{"Node P": {"endpoint": "https://p.example"}}`,
		"unknown_field":         `{"node-p": {"endpoint": "https://p.example", "admin": true}}`,
		"legacy_service_secret": `{"node-p": {"endpoint": "https://p.example", "service_token": "s"}}`,
		"legacy_peer_secret":    `{"node-p": {"endpoint": "https://p.example", "peer_token": "p"}}`,
		"query_string":          `{"node-p": {"endpoint": "https://p.example/?x=1"}}`,
		"userinfo":              `{"node-p": {"endpoint": "https://user:pass@p.example"}}`,
		"loopback_default":      `{"node-p": {"endpoint": "http://127.0.0.1:9000"}}`,
		"localhost_loopback":    `{"node-p": {"endpoint": "http://localhost:9000"}}`,
	} {
		if _, err := ParsePeers(raw, false); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
	if _, err := ParsePeers(`{"node-p": {"endpoint": "http://localhost:9000"}}`, true); err == nil {
		t.Fatal("localhost must never pass the literal loopback boundary")
	}
	peers, err := ParsePeers(`{"node-p": {"endpoint": "http://127.0.0.1:9000/"}}`, true)
	if err != nil || peers["node-p"].Endpoint != "http://127.0.0.1:9000" {
		t.Fatalf("literal loopback rejected: %v %v", peers, err)
	}
}

func TestPeerClientNeverFollowsRedirectOrForwardsCredentials(t *testing.T) {
	var foreignHits atomic.Int32
	foreign := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { foreignHits.Add(1); w.WriteHeader(200) }))
	defer foreign.Close()
	sign, issuer := peerTestSigner(t)
	nonces := make(chan string, 2)
	registered := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("X-DDP-Peer-Token") != "" {
			t.Error("shared credential leaked")
		}
		claims, err := VerifyCredential(r.Header.Get(HeaderNodeCredential), issuer.PublicKey())
		if err != nil || claims.AudienceNodeID != "node-p" || claims.Request.Path != r.URL.Path {
			t.Error("request signature invalid")
		}
		nonces <- claims.JTI
		http.Redirect(w, r, foreign.URL+"/steal", http.StatusFound)
	}))
	defer registered.Close()
	cfg := PeerConfig{NodeID: "node-p", Endpoint: registered.URL}
	dir := NewPeerDirectory(map[string]PeerConfig{"node-p": cfg}, nil, time.Second, sign)
	for range 2 {
		if _, reason := dir.MembersPage(context.Background(), cfg, "", "", 50); reason != "unknown" {
			t.Fatalf("redirect failure: %s", reason)
		}
	}
	if foreignHits.Load() != 0 {
		t.Fatal("redirect sent credentials to an unregistered endpoint")
	}
	if len(nonces) != 2 {
		t.Fatal("the registered endpoint did not receive both reads")
	}
	if (<-nonces) == (<-nonces) {
		t.Fatal("read credentials must not be reused")
	}
}

func TestPeerDirectoryUnknownNodeIsNeverContacted(t *testing.T) {
	var hits atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer server.Close()
	sign, _ := peerTestSigner(t)
	dir := NewPeerDirectory(map[string]PeerConfig{}, nil, time.Second, sign)
	if _, reason := dir.MembersPage(context.Background(), PeerConfig{NodeID: "node-x", Endpoint: server.URL}, "", "", 50); reason != "denied" || hits.Load() != 0 {
		t.Fatal("unregistered node reached the network")
	}
	if _, err := ParsePeers(`{"node-p": {"endpoint": "https://p.example"}}`, false); err != nil {
		t.Fatalf("valid endpoint rejected: %v", err)
	}
}
