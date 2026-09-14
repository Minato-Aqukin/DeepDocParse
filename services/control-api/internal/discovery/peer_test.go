package discovery

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestParsePeersFailsClosedOnMalformedDirectory(t *testing.T) {
	if peers, err := ParsePeers("", false); err != nil || len(peers) != 0 {
		t.Fatalf("empty directory must be empty, not an error: %v %v", peers, err)
	}
	for name, raw := range map[string]string{
		"invalid_json":       `{`,
		"not_an_object":      `[]`,
		"invalid_node_id":    `{"Node P": {"endpoint": "https://p.example", "service_token": "s", "peer_token": "p"}}`,
		"unknown_field":      `{"node-p": {"endpoint": "https://p.example", "service_token": "s", "peer_token": "p", "admin": true}}`,
		"missing_service":    `{"node-p": {"endpoint": "https://p.example", "peer_token": "p"}}`,
		"missing_peer":       `{"node-p": {"endpoint": "https://p.example", "service_token": "s"}}`,
		"query_string":       `{"node-p": {"endpoint": "https://p.example/?x=1", "service_token": "s", "peer_token": "p"}}`,
		"userinfo":           `{"node-p": {"endpoint": "https://user:pass@p.example", "service_token": "s", "peer_token": "p"}}`,
		"loopback_default":   `{"node-p": {"endpoint": "http://127.0.0.1:9000", "service_token": "s", "peer_token": "p"}}`,
		"localhost_loopback": `{"node-p": {"endpoint": "http://localhost:9000", "service_token": "s", "peer_token": "p"}}`,
	} {
		if _, err := ParsePeers(raw, false); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
	if _, err := ParsePeers(`{"node-p": {"endpoint": "http://localhost:9000", "service_token": "s", "peer_token": "p"}}`, true); err == nil {
		t.Fatal("localhost must never pass the loopback escape hatch; it resolves through DNS")
	}
	peers, err := ParsePeers(`{"node-p": {"endpoint": "http://127.0.0.1:9000/", "service_token": "s", "peer_token": "p"}}`, true)
	if err != nil || peers["node-p"].Endpoint != "http://127.0.0.1:9000" {
		t.Fatalf("explicit loopback escape hatch must keep the literal address: %v %v", peers, err)
	}
}

func TestPeerClientNeverFollowsRedirectOrForwardsCredentials(t *testing.T) {
	var foreignHits atomic.Int32
	foreign := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		foreignHits.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	defer foreign.Close()
	var authHeader, peerHeader string
	registered := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authHeader = r.Header.Get("Authorization")
		peerHeader = r.Header.Get(HeaderPeerToken)
		http.Redirect(w, r, foreign.URL+"/steal", http.StatusFound)
	}))
	defer registered.Close()
	dir := NewPeerDirectory(map[string]PeerConfig{"node-p": {NodeID: "node-p", Endpoint: registered.URL, ServiceToken: "service-secret", PeerToken: "peer-secret"}}, nil, time.Second)
	_, reason := dir.MembersPage(context.Background(), PeerConfig{NodeID: "node-p", Endpoint: registered.URL, ServiceToken: "service-secret", PeerToken: "peer-secret"}, "", "", 50)
	if reason != "unknown" {
		t.Fatalf("redirect must be an honest failure, got %q", reason)
	}
	if foreignHits.Load() != 0 {
		t.Fatal("peer client followed a redirect to an unregistered host")
	}
	if authHeader != "Bearer service-secret" || peerHeader != "peer-secret" {
		t.Fatal("registered endpoint did not receive the configured peer credentials")
	}
}

func TestPeerDirectoryUnknownNodeIsNeverContacted(t *testing.T) {
	dir := NewPeerDirectory(map[string]PeerConfig{}, nil, time.Second)
	if _, ok := dir.Configured("node-x"); ok {
		t.Fatal("unknown node reported configured")
	}
	if dir.Len() != 0 {
		t.Fatal("empty directory reported entries")
	}
	if _, err := ParsePeers(`{"node-p": {"endpoint": "https://p.example", "service_token": "s", "peer_token": "p"}}`, false); err != nil {
		t.Fatalf("valid https peer rejected: %v", err)
	}
}
