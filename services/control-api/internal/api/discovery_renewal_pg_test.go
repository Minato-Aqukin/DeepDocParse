package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func TestPublicDescriptorHasLeaseSignature(t *testing.T) {
	id, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "peer"), true)
	if err != nil {
		t.Fatal(err)
	}
	f := &Server{cfg: &config.Config{PublicBaseURL: "https://peer.example"}, nodeIdentity: id, nodeRevision: 1}
	w := httptest.NewRecorder()
	if err := f.handleFederationNode(w, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
		t.Fatal(err)
	}
	var body struct {
		Descriptor discovery.NodeDescriptor `json:"descriptor"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Descriptor.PublisherSignature == "" {
		t.Fatal("public descriptor has no signed lease; directory cannot authenticate renewal")
	}
}

func TestDiscoveryBackgroundRenewsApprovedLease(t *testing.T) {
	f := discoveryPGFixture(t)
	id, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "peer"), true)
	if err != nil {
		t.Fatal(err)
	}
	peer := &Server{cfg: &config.Config{PublicBaseURL: "https://peer.example"}, nodeIdentity: id, nodeRevision: 1}
	endpoint := httptest.NewServer(httpx.Wrap(peer.handleFederationNode))
	defer endpoint.Close()
	peer.cfg.PublicBaseURL = endpoint.URL
	w := httptest.NewRecorder()
	if err := peer.handleFederationNode(w, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
		t.Fatal(err)
	}
	var response struct {
		Descriptor discovery.NodeDescriptor `json:"descriptor"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	old := response.Descriptor
	old.ValidUntil = time.Now().Add(time.Minute)
	old.PublisherSignature = ""
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := f.server.store.RegisterNode(ctx, f.org, discovery.Registration{Descriptor: old, PublicKey: id.PublicKey(), VisibleToOrg: true}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.server.store.SetNodeState(ctx, f.org, id.NodeID(), discovery.MemberApproved); err != nil {
		t.Fatal(err)
	}
	snap, err := f.server.store.CreateMemberSnapshot(ctx, f.org, f.admin.ID, "renewal-smoke", f.server.nodeIdentity.NodeID(), true, 10, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	f.server.cfg.OutboxInterval = time.Hour
	f.server.RunBackground(ctx)
	deadline := time.Now().Add(time.Second)
	renewed := false
	for time.Now().Before(deadline) {
		members, err := f.server.store.ListNodes(ctx, f.org, f.server.nodeIdentity.NodeID())
		if err != nil {
			t.Fatal(err)
		}
		if len(members) == 1 && members[0].Descriptor.ValidUntil.After(old.ValidUntil) {
			renewed = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !renewed {
		t.Fatal("approved member lease was not renewed automatically; valid_until still equals registered expiry")
	}
	page, err := f.server.store.MemberSnapshotPage(ctx, f.org, f.admin.ID, "renewal-smoke", snap.ID, snap.FirstCursor, true)
	if err != nil {
		t.Fatal(err)
	}
	if !page.Members[0].Descriptor.ValidUntil.Equal(old.ValidUntil) {
		t.Fatal("automatic renewal extended a frozen member snapshot")
	}
	var success *time.Time
	var failures int
	if err := f.server.store.Pool().QueryRow(ctx, `SELECT renewal_last_success_at,renewal_attempts FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, f.org, id.NodeID()).Scan(&success, &failures); err != nil {
		t.Fatal(err)
	}
	if success == nil || failures != 0 {
		t.Fatalf("successful lease renewal was not recorded: success=%v attempts=%d", success, failures)
	}
}

func TestDiscoveryRenewalRefusesUntrustedAndInactivePeers(t *testing.T) {
	for _, mode := range []string{"signature", "key", "configuration", "revoked", "pending", "down", "redirect", "timeout"} {
		t.Run(mode, func(t *testing.T) {
			f := discoveryPGFixture(t)
			ctx := context.Background()
			id, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "peer"), true)
			if err != nil {
				t.Fatal(err)
			}
			other, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "other"), true)
			if err != nil {
				t.Fatal(err)
			}
			peer := &Server{cfg: &config.Config{}, nodeIdentity: id, nodeRevision: 1}
			var requests, redirected atomic.Int32
			redirectTarget := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { redirected.Add(1); w.WriteHeader(500) }))
			defer redirectTarget.Close()
			endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests.Add(1)
				if r.URL.Path != "/api/v1/federation/node" {
					t.Errorf("renewal used unapproved route %s", r.URL.Path)
				}
				switch mode {
				case "down":
					w.WriteHeader(503)
					return
				case "redirect":
					http.Redirect(w, r, redirectTarget.URL, 302)
					return
				case "timeout":
					<-r.Context().Done()
					return
				}
				record := httptest.NewRecorder()
				if err := peer.handleFederationNode(record, r); err != nil {
					t.Error(err)
					w.WriteHeader(500)
					return
				}
				var body struct {
					AuthorityNodeID string                   `json:"authority_node_id"`
					PublicKey       string                   `json:"public_key"`
					Descriptor      discovery.NodeDescriptor `json:"descriptor"`
				}
				if err := json.Unmarshal(record.Body.Bytes(), &body); err != nil {
					t.Error(err)
					return
				}
				switch mode {
				case "signature":
					body.Descriptor.PublisherSignature = strings.Repeat("A", 86) + "=="
				case "key":
					body.PublicKey = other.PublicKey()
				case "configuration":
					body.Descriptor.ControlledEndpoints[0].URL = redirectTarget.URL
					if err := id.SignDescriptor(&body.Descriptor); err != nil {
						t.Error(err)
					}
				}
				if err := json.NewEncoder(w).Encode(body); err != nil {
					t.Error(err)
				}
			}))
			defer endpoint.Close()
			peer.cfg.PublicBaseURL = endpoint.URL
			old := discovery.NodeDescriptor{Schema: "ddp-discovery/1#NodeDescriptor", NodeID: id.NodeID(), ProtocolVersions: []string{"ddp-discovery/1", "ddp-client/1"}, ControlledEndpoints: []discovery.Endpoint{{Purpose: "federation", URL: endpoint.URL + "/api/v1/federation"}}, AuthMethods: []string{"session", "user_api_key"}, DiscoveryCapabilities: discovery.DiscoveryCapabilities{EnumerateMembers: true}, Revision: 1, ValidUntil: time.Now().Add(200 * time.Millisecond)}
			if _, err := f.server.store.RegisterNode(ctx, f.org, discovery.Registration{Descriptor: old, PublicKey: id.PublicKey(), VisibleToOrg: true}); err != nil {
				t.Fatal(err)
			}
			if mode != "pending" {
				if _, err := f.server.store.SetNodeState(ctx, f.org, id.NodeID(), discovery.MemberApproved); err != nil {
					t.Fatal(err)
				}
			}
			if mode == "revoked" {
				if _, err := f.server.store.SetNodeState(ctx, f.org, id.NodeID(), discovery.MemberRevoked); err != nil {
					t.Fatal(err)
				}
			}
			client := discovery.NewLeaseClient()
			defer client.CloseIdleConnections()
			f.server.renewDiscoveryBatch(ctx, client, time.Second)
			var attempts int
			var lastError *string
			var success *time.Time
			var next time.Time
			if err := f.server.store.Pool().QueryRow(ctx, `SELECT renewal_attempts,renewal_last_error,renewal_last_success_at,renewal_next_attempt_at FROM control.node_members WHERE organization_id=$1 AND node_id=$2`, f.org, id.NodeID()).Scan(&attempts, &lastError, &success, &next); err != nil {
				t.Fatal(err)
			}
			if success != nil {
				t.Fatal("refused peer acquired successful renewal")
			}
			if mode == "pending" || mode == "revoked" {
				if requests.Load() != 0 || attempts != 0 || lastError != nil {
					t.Fatalf("inactive peer was probed: requests=%d attempts=%d error=%v", requests.Load(), attempts, lastError)
				}
			} else {
				if requests.Load() != 1 || attempts != 1 || lastError == nil || *lastError == "" || !next.After(time.Now()) {
					t.Fatalf("failed renewal not persistently backed off: requests=%d attempts=%d error=%v next=%v", requests.Load(), attempts, lastError, next)
				}
				f.server.renewDiscoveryBatch(ctx, client, time.Second)
				if requests.Load() != 1 {
					t.Fatal("failed peer retried before persistent backoff")
				}
			}
			if redirected.Load() != 0 {
				t.Fatal("renewal followed redirect or fetched response-provided endpoint")
			}
			if wait := time.Until(old.ValidUntil); wait > 0 {
				time.Sleep(wait + time.Millisecond)
			}
			members, err := f.server.store.ListNodes(ctx, f.org, f.server.nodeIdentity.NodeID())
			if err != nil {
				t.Fatal(err)
			}
			if !members[0].Descriptor.ValidUntil.Equal(old.ValidUntil) || members[0].Route.ValidUntil.After(time.Now()) {
				t.Fatal("failed renewal kept an expired lease fresh")
			}
			response := requestDiscovery(t, f.handler, "GET", "/api/v1/federation/nodes", f.adminToken, nil)
			body := decodeDiscovery[struct {
				Members []discovery.Member `json:"members"`
			}](t, response, 200)
			if body.Members[0].Descriptor.ValidUntil.After(time.Now()) {
				t.Fatal("admin API hid expired lease")
			}
		})
	}
}

func TestDiscoveryRenewalCannotUndoConcurrentRevocation(t *testing.T) {
	f := discoveryPGFixture(t)
	ctx := context.Background()
	peer := proofServer(t)
	w := httptest.NewRecorder()
	if err := peer.handleFederationNode(w, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
		t.Fatal(err)
	}
	body := decodeDiscovery[struct {
		Descriptor discovery.NodeDescriptor `json:"descriptor"`
	}](t, w, 200)
	old := body.Descriptor
	old.ValidUntil = time.Now().Add(time.Minute)
	old.PublisherSignature = ""
	if _, err := f.server.store.RegisterNode(ctx, f.org, discovery.Registration{Descriptor: old, PublicKey: peer.nodeIdentity.PublicKey(), VisibleToOrg: true}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.server.store.SetNodeState(ctx, f.org, old.NodeID, discovery.MemberApproved); err != nil {
		t.Fatal(err)
	}
	claims, err := f.server.store.ClaimDiscoveryRenewals(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var own []store.DiscoveryRenewal
	for _, claim := range claims {
		if claim.OrganizationID == f.org {
			own = append(own, claim)
		}
	}
	if len(own) != 1 {
		t.Fatalf("own organization claims %v", own)
	}
	if _, err := f.server.store.SetNodeState(ctx, f.org, old.NodeID, discovery.MemberRevoked); err != nil {
		t.Fatal(err)
	}
	applied, err := f.server.store.CompleteDiscoveryRenewal(ctx, own[0], body.Descriptor, nil, time.Minute)
	if err != nil || applied {
		t.Fatalf("revoked node refreshed: %v %v", applied, err)
	}
	members, err := f.server.store.ListNodes(ctx, f.org, f.server.nodeIdentity.NodeID())
	if err != nil {
		t.Fatal(err)
	}
	if members[0].State != discovery.MemberRevoked || !members[0].Descriptor.ValidUntil.Equal(old.ValidUntil) {
		t.Fatal("revocation or lease changed by in-flight renewal")
	}
}

func TestDiscoveryRenewalBoundsConcurrency(t *testing.T) {
	f := discoveryPGFixture(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	release := make(chan struct{})
	entered := make(chan struct{}, 8)
	var once sync.Once
	releaseNow := func() { once.Do(func() { close(release) }) }
	defer releaseNow()
	var active, maximum atomic.Int32
	for range 8 {
		peer := proofServer(t)
		endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			current := active.Add(1)
			defer active.Add(-1)
			for {
				old := maximum.Load()
				if current <= old || maximum.CompareAndSwap(old, current) {
					break
				}
			}
			entered <- struct{}{}
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			httpx.Wrap(peer.handleFederationNode).ServeHTTP(w, r)
		}))
		t.Cleanup(endpoint.Close)
		peer.cfg.PublicBaseURL = endpoint.URL
		w := httptest.NewRecorder()
		if err := peer.handleFederationNode(w, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
			t.Fatal(err)
		}
		body := decodeDiscovery[struct {
			Descriptor discovery.NodeDescriptor `json:"descriptor"`
		}](t, w, 200)
		body.Descriptor.ValidUntil = time.Now().Add(time.Minute)
		body.Descriptor.PublisherSignature = ""
		if _, err := f.server.store.RegisterNode(ctx, f.org, discovery.Registration{Descriptor: body.Descriptor, PublicKey: peer.nodeIdentity.PublicKey(), VisibleToOrg: true}); err != nil {
			t.Fatal(err)
		}
		if _, err := f.server.store.SetNodeState(ctx, f.org, peer.nodeIdentity.NodeID(), discovery.MemberApproved); err != nil {
			t.Fatal(err)
		}
	}
	client := discovery.NewLeaseClient()
	defer client.CloseIdleConnections()
	done := make(chan struct{})
	go func() { defer close(done); f.server.renewDiscoveryBatch(ctx, client, time.Minute) }()
	for range 4 {
		select {
		case <-entered:
		case <-ctx.Done():
			t.Fatal("four workers did not start")
		}
	}
	releaseNow()
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("bounded renewal did not finish")
	}
	if maximum.Load() != 4 {
		t.Fatalf("expected four concurrent peers, observed %d", maximum.Load())
	}
	var successes int
	if err := f.server.store.Pool().QueryRow(ctx, `SELECT count(*) FROM control.node_members WHERE organization_id=$1 AND renewal_last_success_at IS NOT NULL`, f.org).Scan(&successes); err != nil {
		t.Fatal(err)
	}
	if successes != 8 {
		t.Fatalf("bounded workers refreshed %d of eight approved peers", successes)
	}
}
