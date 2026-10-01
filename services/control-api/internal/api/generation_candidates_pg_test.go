package api

import (
	"context"
	"encoding/json"
	"io"
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

func TestGenerationCandidatesHTTPVisibilityFreshnessAndObservation(t *testing.T) {
	f := discoveryPGFixture(t)
	var calls atomic.Int32
	configs := map[string]discovery.PeerConfig{}
	ids := map[string]string{}
	for _, name := range []string{"ready", "unhealthy", "unknown", "stale", "unreachable", "hidden", "pending", "revoked", "expired"} {
		reg := remoteRegistration(t, name != "hidden")
		ids[name] = reg.Descriptor.NodeID
		nodeID := reg.Descriptor.NodeID
		peer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			if r.URL.Path != "/api/v1/federation/generation-descriptor" || r.URL.RawQuery != "" || r.ContentLength != 0 {
				t.Errorf("discovery leaked a payload or used wrong route: %s", r.URL)
			}
			for _, header := range []string{"Authorization", "Cookie", identity.HeaderUser, identity.HeaderOrganization, identity.HeaderActor} {
				if r.Header.Get(header) != "" {
					t.Errorf("forwarded %s", header)
				}
			}
			claims, err := discovery.VerifyCredential(r.Header.Get(discovery.HeaderNodeCredential), f.server.nodeIdentity.PublicKey())
			if err != nil || claims.AudienceNodeID != nodeID || claims.Operation != "directory_capabilities_read" || claims.Actor.Kind != "service" || claims.Actor.Subject != "control-api" {
				t.Errorf("discovery was not signed for its receiver: %v %+v", err, claims)
			}
			if name == "unreachable" {
				w.WriteHeader(503)
				return
			}
			state, status := "ready", "observed"
			if name == "unhealthy" {
				state = "unhealthy"
			}
			if name == "unknown" {
				status = "unknown"
			}
			valid := time.Now().UTC().Add(time.Minute)
			if name == "stale" {
				valid = time.Now().UTC().Add(-time.Second)
			}
			json.NewEncoder(w).Encode(map[string]any{"node_id": nodeID, "capability_status": status, "profiles": []any{map[string]any{
				"schema": "ddp-discovery/1#CapabilityProfile", "node_id": nodeID,
				"operation": "rag.answer.cited", "readiness": state, "accepting_admissions": true,
				"observed_at": time.Now().UTC().Add(-time.Minute), "valid_until": valid,
			}}})
		}))
		t.Cleanup(peer.Close)
		configs[nodeID] = discovery.PeerConfig{NodeID: nodeID, Endpoint: peer.URL}
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, reg), 201)
		if name != "pending" {
			decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+nodeID+"/approve", f.adminToken, nil), 200)
		}
		if name == "revoked" {
			decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+nodeID+"/revoke", f.adminToken, nil), 200)
		}
		if name == "expired" {
			_, err := f.server.store.Pool().Exec(context.Background(), `UPDATE control.node_members SET descriptor=jsonb_set(descriptor,'{valid_until}',to_jsonb((now()-interval '1 second')::text)) WHERE organization_id=$1 AND node_id=$2`, f.org, nodeID)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	f.server.peers = discovery.NewPeerDirectory(configs, nil, time.Second, f.server.signPeerRead)
	path := "/api/v1/federation/generation-candidates?operation=rag.answer.cited"
	if w := requestDiscovery(t, f.handler, "GET", path, "", nil); w.Code != 401 {
		t.Fatalf("anonymous discovery: %d", w.Code)
	}
	page := decodeDiscovery[struct {
		Items []struct {
			NodeID    string `json:"node_id"`
			Readiness string `json:"readiness"`
			Accepting bool   `json:"accepting_admissions"`
			Observed  string `json:"observed_at"`
		} `json:"items"`
	}](t, requestDiscovery(t, f.handler, "GET", path, f.bobToken, nil), 200)
	if len(page.Items) != 5 || calls.Load() != 5 {
		t.Fatalf("hidden/nonfresh member contacted or exposed: %+v calls=%d", page, calls.Load())
	}
	seen := map[string]string{}
	for _, item := range page.Items {
		seen[item.NodeID] = item.Readiness
		if item.Accepting != (item.NodeID == ids["ready"]) {
			t.Fatalf("dishonest admission: %+v", item)
		}
		if item.Readiness == "unknown" && item.Observed != "" {
			t.Fatalf("invented observation: %+v", item)
		}
	}
	for name, want := range map[string]string{"ready": "ready", "unhealthy": "unhealthy", "unknown": "unknown", "stale": "unknown", "unreachable": "unknown"} {
		if seen[ids[name]] != want {
			t.Fatalf("%s readiness=%q want %q", name, seen[ids[name]], want)
		}
	}
	for _, query := range []string{"", "?operation=other", "?operation=rag.answer.cited&operation=wiki.pages", "?operation=rag.answer.cited&query=private-task"} {
		w := requestDiscovery(t, f.handler, "GET", strings.Split(path, "?")[0]+query, f.bobToken, nil)
		if w.Code != 400 {
			t.Fatalf("invalid discovery query %q: %d", query, w.Code)
		}
	}
}

func TestGenerationCandidatesSignedTwoControlHTTPServers(t *testing.T) {
	a, b := discoveryPGFixture(t), discoveryPGFixture(t)
	register := func(f *discoveryFixture, node *discovery.Identity) {
		reg := remoteRegistration(t, true)
		reg.PublicKey, reg.Descriptor.NodeID = node.PublicKey(), node.NodeID()
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, reg), 201)
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+node.NodeID()+"/approve", f.adminToken, nil), 200)
	}
	register(a, b.server.nodeIdentity)
	register(b, a.server.nodeIdentity)
	var producerCalls atomic.Int32
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		producerCalls.Add(1)
		if r.URL.Path != "/internal/capabilities" || r.Header.Get("Authorization") != "Bearer internal-test-service" ||
			r.Header.Get(identity.HeaderActor) != "control-api" || r.Header.Get(identity.HeaderUser) != "" {
			t.Error("producer received user identity or wrong local service credential")
		}
		json.NewEncoder(w).Encode(map[string]any{"capability_status": "observed", "profiles": []any{
			map[string]any{"schema": "ddp-discovery/1#CapabilityProfile", "operation": "rag.answer.cited", "readiness": "ready", "accepting_admissions": true, "observed_at": time.Now().UTC(), "valid_until": time.Now().UTC().Add(time.Minute)},
			map[string]any{"schema": "ddp-discovery/1#CapabilityProfile", "operation": "wiki.pages", "readiness": "draining", "accepting_admissions": false, "observed_at": time.Now().UTC(), "valid_until": time.Now().UTC().Add(time.Minute)},
		}})
	}))
	t.Cleanup(producer.Close)
	b.server.cfg.CorpusURL = producer.URL
	b.server.corpus, _ = proxy.New("corpus", producer.URL, b.server.cfg.ServiceToken)
	receiver := httptest.NewServer(b.handler)
	t.Cleanup(receiver.Close)
	a.server.peers = discovery.NewPeerDirectory(map[string]discovery.PeerConfig{
		b.server.nodeIdentity.NodeID(): {NodeID: b.server.nodeIdentity.NodeID(), Endpoint: receiver.URL},
	}, nil, time.Second, a.server.signPeerRead)
	origin := httptest.NewServer(a.handler)
	t.Cleanup(origin.Close)
	_, key, err := a.server.store.CreateAPIKey(context.Background(), a.org, a.bob.ID, "read-discovery", []rbac.Scope{rbac.ScopeRead}, nil, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		operation, token, readiness string
		accepting                   bool
	}{
		{"rag.answer.cited", a.bobToken, "ready", true},
		{"wiki.pages", key, "draining", false},
	} {
		req, err := http.NewRequest("GET", origin.URL+"/api/v1/federation/generation-candidates?operation="+tc.operation, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("Authorization", "Bearer "+tc.token)
		resp, err := origin.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		var page struct {
			Items []generationCandidate `json:"items"`
		}
		if resp.StatusCode != 200 || json.Unmarshal(body, &page) != nil || len(page.Items) != 1 ||
			page.Items[0].NodeID != b.server.nodeIdentity.NodeID() || string(page.Items[0].Readiness) != tc.readiness ||
			page.Items[0].AcceptingAdmissions != tc.accepting || page.Items[0].ObservedAt == nil {
			t.Fatalf("real HTTP observation failed: %d %s", resp.StatusCode, body)
		}
		t.Logf("in-process HTTP smoke: GET generation-candidates operation=%s HTTP %d readiness=%s accepting=%t", tc.operation, resp.StatusCode, page.Items[0].Readiness, page.Items[0].AcceptingAdmissions)
	}
	path := "/api/v1/federation/generation-descriptor"
	token := signedPeerRead(t, b, a.server.nodeIdentity, path)
	decodeDiscovery[map[string]any](t, requestPeer(t, b.handler, path, token), 200)
	if w := requestPeer(t, b.handler, path, token); w.Code != 401 {
		t.Fatalf("descriptor replay accepted: %d", w.Code)
	}
	if w := requestDiscovery(t, b.handler, "GET", path, b.bobToken, nil); w.Code != 401 {
		t.Fatalf("user bearer authenticated as peer: %d", w.Code)
	}
	if producerCalls.Load() != 3 {
		t.Fatalf("replay or user bearer reached producer: %d calls", producerCalls.Load())
	}
}

func TestGenerationCandidatesHangingPeerDoesNotHideReadyPeer(t *testing.T) {
	f := discoveryPGFixture(t)
	first, second := remoteRegistration(t, true), remoteRegistration(t, true)
	if first.Descriptor.NodeID > second.Descriptor.NodeID {
		first, second = second, first
	}
	configs := make(map[string]discovery.PeerConfig, 2)
	for _, reg := range []discovery.Registration{first, second} {
		reg := reg
		peer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if reg.Descriptor.NodeID == first.Descriptor.NodeID {
				<-r.Context().Done()
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"node_id": reg.Descriptor.NodeID,
				"capability_status": "observed", "profiles": []any{map[string]any{
					"schema": "ddp-discovery/1#CapabilityProfile", "node_id": reg.Descriptor.NodeID,
					"operation": "rag.answer.cited", "readiness": "ready", "accepting_admissions": true,
					"observed_at": time.Now().UTC(), "valid_until": time.Now().UTC().Add(time.Minute),
				}}})
		}))
		t.Cleanup(peer.Close)
		configs[reg.Descriptor.NodeID] = discovery.PeerConfig{NodeID: reg.Descriptor.NodeID, Endpoint: peer.URL}
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, reg), 201)
		decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+reg.Descriptor.NodeID+"/approve", f.adminToken, nil), 200)
	}
	f.server.peers = discovery.NewPeerDirectory(configs, nil, 10*time.Second, f.server.signPeerRead)
	page := decodeDiscovery[struct {
		Items []generationCandidate `json:"items"`
	}](t, requestDiscovery(t, f.handler, "GET",
		"/api/v1/federation/generation-candidates?operation=rag.answer.cited", f.bobToken, nil), 200)
	if len(page.Items) != 2 {
		t.Fatalf("unexpected approved candidates: %+v", page.Items)
	}
	if page.Items[0].NodeID != first.Descriptor.NodeID || page.Items[0].Readiness != "unknown" {
		t.Fatalf("hanging peer was not honestly unknown: %+v", page.Items[0])
	}
	if page.Items[1].NodeID != second.Descriptor.NodeID || page.Items[1].Readiness != "ready" || !page.Items[1].AcceptingAdmissions {
		t.Fatalf("hanging peer hid a later ready peer: %+v", page.Items[1])
	}
}
