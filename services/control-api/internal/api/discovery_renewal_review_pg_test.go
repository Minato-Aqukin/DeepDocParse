package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

func registerRenewalReviewPeer(t *testing.T, f *discoveryFixture, peer *Server) discovery.NodeDescriptor {
	t.Helper()
	record := httptest.NewRecorder()
	if err := peer.handleFederationNode(record, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
		t.Fatal(err)
	}
	body := decodeDiscovery[struct {
		Descriptor discovery.NodeDescriptor `json:"descriptor"`
	}](t, record, 200)
	body.Descriptor.ValidUntil = time.Now().Add(time.Minute)
	body.Descriptor.PublisherSignature = ""
	ctx := context.Background()
	if _, err := f.server.store.RegisterNode(ctx, f.org, discovery.Registration{Descriptor: body.Descriptor, PublicKey: peer.nodeIdentity.PublicKey(), VisibleToOrg: true}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.server.store.SetNodeState(ctx, f.org, peer.nodeIdentity.NodeID(), discovery.MemberApproved); err != nil {
		t.Fatal(err)
	}
	return body.Descriptor
}

func TestDiscoveryRenewalCadenceFollowsConfiguredInterval(t *testing.T) {
	f := discoveryPGFixture(t)
	peer := proofServer(t)
	observations := make(chan time.Time, 8)
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(40 * time.Millisecond)
		observations <- time.Now()
		httpx.Wrap(peer.handleFederationNode).ServeHTTP(w, r)
	}))
	defer endpoint.Close()
	peer.cfg.PublicBaseURL = endpoint.URL
	registerRenewalReviewPeer(t, f, peer)
	f.server.cfg.DiscoveryRenewalInterval = time.Second
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); f.server.renewDiscoveryLeases(ctx) }()
	defer func() { cancel(); <-done }()
	var first time.Time
	select {
	case first = <-observations:
	case <-time.After(time.Second):
		t.Fatal("initial renewal did not start")
	}
	select {
	case second := <-observations:
		elapsed := second.Sub(first)
		if elapsed < time.Second || elapsed > 1800*time.Millisecond {
			t.Fatalf("configured one-second renewal took %s", elapsed)
		}
	case <-time.After(1800 * time.Millisecond):
		t.Fatal("one-second renewal interval became two seconds because due time followed the fetch")
	}
}

func TestDiscoveryRenewalDrainsFullBatchesBeforeSleeping(t *testing.T) {
	f := discoveryPGFixture(t)
	for range 33 {
		peer := proofServer(t)
		endpoint := httptest.NewServer(httpx.Wrap(peer.handleFederationNode))
		t.Cleanup(endpoint.Close)
		peer.cfg.PublicBaseURL = endpoint.URL
		registerRenewalReviewPeer(t, f, peer)
	}
	f.server.cfg.DiscoveryRenewalInterval = time.Minute
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); f.server.renewDiscoveryLeases(ctx) }()
	defer func() { cancel(); <-done }()
	deadline := time.Now().Add(2 * time.Second)
	var successes int
	for time.Now().Before(deadline) {
		if err := f.server.store.Pool().QueryRow(ctx, `SELECT count(*) FROM control.node_members WHERE organization_id=$1 AND renewal_last_success_at IS NOT NULL`, f.org).Scan(&successes); err != nil {
			t.Fatal(err)
		}
		if successes == 33 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("only %d of 33 due members renewed before sleeping for the configured interval", successes)
}

func TestDiscoveryRenewalRejectsFarFutureLeaseBeforePersistence(t *testing.T) {
	f := discoveryPGFixture(t)
	peer := proofServer(t)
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		record := httptest.NewRecorder()
		httpx.Wrap(peer.handleFederationNode).ServeHTTP(record, r)
		var body struct {
			AuthorityNodeID string                   `json:"authority_node_id"`
			PublicKey       string                   `json:"public_key"`
			Descriptor      discovery.NodeDescriptor `json:"descriptor"`
		}
		if err := json.Unmarshal(record.Body.Bytes(), &body); err != nil {
			t.Error(err)
			return
		}
		body.Descriptor.ValidUntil = time.Now().Add(24 * time.Hour)
		if err := peer.nodeIdentity.SignDescriptor(&body.Descriptor); err != nil {
			t.Error(err)
			return
		}
		if err := json.NewEncoder(w).Encode(body); err != nil {
			t.Error(err)
		}
	}))
	defer endpoint.Close()
	peer.cfg.PublicBaseURL = endpoint.URL
	old := registerRenewalReviewPeer(t, f, peer)
	ctx := context.Background()
	client := discovery.NewLeaseClient()
	defer client.CloseIdleConnections()
	if _, err := discovery.FetchLease(ctx, client, old, peer.nodeIdentity.PublicKey(), f.server.nodeIdentity.NodeID()); err == nil {
		t.Error("far-future signed lease accepted by fetch")
	}
	claims, err := f.server.store.ClaimDiscoveryRenewals(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var own *store.DiscoveryRenewal
	for i := range claims {
		if claims[i].OrganizationID == f.org {
			own = &claims[i]
			break
		}
	}
	if own == nil {
		t.Fatal("own renewal was not claimed")
	}
	future := old
	future.ValidUntil = time.Now().Add(24 * time.Hour)
	if err := peer.nodeIdentity.SignDescriptor(&future); err != nil {
		t.Fatal(err)
	}
	if applied, err := f.server.store.CompleteDiscoveryRenewal(ctx, *own, future, nil, time.Minute); err == nil || applied {
		t.Fatalf("far-future lease persisted: applied=%v err=%v", applied, err)
	}
	members, err := f.server.store.ListNodes(ctx, f.org, f.server.nodeIdentity.NodeID())
	if err != nil {
		t.Fatal(err)
	}
	if !members[0].Descriptor.ValidUntil.Equal(old.ValidUntil) {
		t.Fatal("far-future lease changed the approved expiry")
	}
}

func TestDiscoveryRegistrationRejectsMalformedPublisherSignature(t *testing.T) {
	f := discoveryPGFixture(t)
	peer := proofServer(t)
	record := httptest.NewRecorder()
	if err := peer.handleFederationNode(record, httptest.NewRequest("GET", "/api/v1/federation/node", nil)); err != nil {
		t.Fatal(err)
	}
	body := decodeDiscovery[struct {
		Descriptor discovery.NodeDescriptor `json:"descriptor"`
	}](t, record, 200)
	body.Descriptor.PublisherSignature = "not-base64"
	response := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, discovery.Registration{Descriptor: body.Descriptor, PublicKey: peer.nodeIdentity.PublicKey()})
	if response.Code != 400 {
		t.Fatalf("malformed publisher_signature accepted: HTTP %d", response.Code)
	}
}
