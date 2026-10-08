package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

// verifiedEmptyCatalog makes the local corpus producer answer a complete empty
// catalog so a scope's only unknown subtrees are the remote ones under test.
func verifiedEmptyCatalog(t *testing.T, f *discoveryFixture) {
	t.Helper()
	created := time.Now().UTC()
	producer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/federation/collections" || r.Header.Get(identity.HeaderActor) != "control-api" {
			t.Error("empty catalog producer received an unexpected request")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"snapshot_id": "empty-catalog", "scope_id": r.URL.Query().Get("scope_id"),
			"caller_scope_hash": r.Header.Get(identity.HeaderCallerScope),
			"origin_node_id":    f.server.nodeIdentity.NodeID(), "registry_revision": 1,
			"created_at": created, "valid_until": created.Add(time.Hour),
			"first_cursor": "only", "terminal_cursor": "only",
			"collections": []any{}, "total": 0, "next_cursor": nil, "complete": true,
		})
	}))
	t.Cleanup(producer.Close)
	f.server.cfg.CorpusURL = producer.URL
	f.server.corpus, _ = proxy.New("corpus", producer.URL, f.server.cfg.ServiceToken)
}

func freshNodeID(t *testing.T) string {
	t.Helper()
	node, err := discovery.LoadIdentity(filepath.Join(t.TempDir(), "peer"), true)
	if err != nil {
		t.Fatal(err)
	}
	return node.NodeID()
}

func approveEnumerableNode(t *testing.T, f *discoveryFixture, registration discovery.Registration) {
	t.Helper()
	registration.Descriptor.DiscoveryCapabilities.EnumerateMembers = true
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes", f.adminToken, registration), 201)
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+registration.Descriptor.NodeID+"/approve", f.adminToken, nil), 200)
}

func createScope(t *testing.T, f *discoveryFixture, body map[string]any) discovery.ScopeEnvelope {
	t.Helper()
	return decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, body), 201)
}

func TestScopeCreateExpandsRemoteDirectoryAndPersistsChildManifests(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	pNode := registration.Descriptor.NodeID
	childRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, childRegistration)
	bNode := childRegistration.Descriptor.NodeID

	p := newFakePeerServer(pNode,
		[]discovery.PeerMember{enumerablePeerMember(bNode)},
		[]discovery.CollectionRef{{CollectionID: "p-col", OriginNodeID: pNode}})
	b := newFakePeerServer(bNode, nil,
		[]discovery.CollectionRef{{CollectionID: "b-col", OriginNodeID: bNode}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{pNode: p.serve(t), bNode: b.serve(t)})

	out := createScope(t, f, map[string]any{"operation": "search"})
	if out.Manifest.EnumerationState != "sealed" || out.TotalTargets != 2 {
		t.Fatalf("expanded scope not sealed: %+v", out.Manifest)
	}
	targets := map[string]string{}
	for _, target := range out.Manifest.ExpandedMembers {
		targets[target.CollectionID] = target.OriginNodeID
	}
	if targets["p-col"] != pNode || targets["b-col"] != bNode {
		t.Fatalf("remote targets wrong or missing: %+v", out.Manifest.ExpandedMembers)
	}
	refs := map[string]int{}
	for _, revision := range out.Manifest.RegistryRevisionVector {
		refs[revision.NodeID+"/"+revision.DirectoryRef]++
	}
	local := f.server.nodeIdentity.NodeID()
	for _, want := range []string{local + "/members", local + "/collections", pNode + "/members", pNode + "/collections", bNode + "/members", bNode + "/collections"} {
		if refs[want] == 0 {
			t.Fatalf("missing revision vector entry %s: %+v", want, out.Manifest.RegistryRevisionVector)
		}
	}
	children := map[string]string{}
	for _, child := range out.Manifest.ChildManifests {
		children[child.NodeID] = child.EnumerationState
	}
	if children[pNode] != "sealed" || children[bNode] != "sealed" || len(children) != 2 {
		t.Fatalf("child manifests wrong: %+v", out.Manifest.ChildManifests)
	}
	if len(out.Manifest.UnexpandedSubtrees) != 0 {
		t.Fatalf("sealed scope kept unknown subtrees: %+v", out.Manifest.UnexpandedSubtrees)
	}

	// The frozen manifest round-trips through the persisted scope read.
	read := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+out.Manifest.ScopeID, f.aliceToken, nil), 200)
	if len(read.Manifest.ChildManifests) != 2 || read.Manifest.ManifestDigest != out.Manifest.ManifestDigest {
		t.Fatalf("child manifests did not round-trip: %+v", read.Manifest)
	}
	var persisted []byte
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT child_manifests FROM control.scope_manifests WHERE id=$1`, out.Manifest.ScopeID).Scan(&persisted); err != nil {
		t.Fatal(err)
	}
	var persistedChildren []discovery.ChildManifest
	if json.Unmarshal(persisted, &persistedChildren) != nil || len(persistedChildren) != 2 {
		t.Fatalf("child_manifests column not persisted: %s", persisted)
	}

	// Both mesh origins are approved locally, so their frozen targets remain
	// available without relying on a transitive trust registration.
	page := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+out.Manifest.ScopeID+"/targets", f.aliceToken, nil), 200)
	for _, target := range page.Targets {
		if target.State != "not_attempted" {
			t.Fatalf("frozen remote target falsely revoked: %+v", target)
		}
	}

	// Revoking P cannot revoke a separately approved direct member B.
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+pNode+"/revoke", f.adminToken, nil), 200)
	revoked := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+out.Manifest.ScopeID+"/targets", f.aliceToken, nil), 200)
	for _, target := range revoked.Targets {
		wantState := "not_attempted"
		if target.TargetKey.OriginNodeID == pNode {
			wantState = "revoked"
		}
		if target.State != wantState {
			t.Fatalf("live mesh authorization overlay wrong: %+v", target)
		}
	}
	if revoked.TotalTargets != 2 || revoked.ManifestDigest != out.Manifest.ManifestDigest {
		t.Fatalf("revocation rewrote the frozen denominator: %+v", revoked)
	}
}

func TestScopeCreateToleratesMutualMemberDirectoriesAndSeals(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	pNode := registration.Descriptor.NodeID
	childRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, childRegistration)
	bNode := childRegistration.Descriptor.NodeID
	// P lists B and B lists P (and the local node); a directory cycle must not
	// recurse, duplicate targets or manufacture unknown subtrees.
	p := newFakePeerServer(pNode,
		[]discovery.PeerMember{enumerablePeerMember(bNode)},
		[]discovery.CollectionRef{{CollectionID: "p-col", OriginNodeID: pNode}})
	b := newFakePeerServer(bNode,
		[]discovery.PeerMember{enumerablePeerMember(pNode), enumerablePeerMember(f.server.nodeIdentity.NodeID())},
		[]discovery.CollectionRef{{CollectionID: "b-col", OriginNodeID: bNode}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{pNode: p.serve(t), bNode: b.serve(t)})
	out := createScope(t, f, map[string]any{"operation": "search"})
	if out.Manifest.EnumerationState != "sealed" || out.TotalTargets != 2 || len(out.Manifest.UnexpandedSubtrees) != 0 {
		t.Fatalf("mutual directories did not seal cleanly: %+v", out.Manifest)
	}
	if len(out.Manifest.ChildManifests) != 2 {
		t.Fatalf("cycle duplicated child manifests: %+v", out.Manifest.ChildManifests)
	}
	if p.countPath("/api/v1/federation/members") != 2 || b.countPath("/api/v1/federation/members") != 2 {
		t.Fatalf("cycle re-queried a directory: p=%d b=%d", p.countPath("/api/v1/federation/members"), b.countPath("/api/v1/federation/members"))
	}
}

func TestScopeCreateLocallyRevokedChildIsDeniedWithoutSigning(t *testing.T) {
	for _, configured := range []bool{true, false} {
		t.Run(map[bool]string{true: "configured", false: "unconfigured"}[configured], func(t *testing.T) {
			f := discoveryPGFixture(t)
			verifiedEmptyCatalog(t, f)
			parentRegistration := remoteRegistration(t, true)
			approveEnumerableNode(t, f, parentRegistration)
			parentNode := parentRegistration.Descriptor.NodeID
			childRegistration := remoteRegistration(t, true)
			approveEnumerableNode(t, f, childRegistration)
			childNode := childRegistration.Descriptor.NodeID
			decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+childNode+"/revoke", f.adminToken, nil), 200)

			parent := newFakePeerServer(parentNode,
				[]discovery.PeerMember{enumerablePeerMember(childNode)},
				[]discovery.CollectionRef{{CollectionID: "parent-col", OriginNodeID: parentNode}})
			child := newFakePeerServer(childNode, nil,
				[]discovery.CollectionRef{{CollectionID: "revoked-col", OriginNodeID: childNode}})
			childServer := child.serve(t)
			configs := map[string]discovery.PeerConfig{
				parentNode: {NodeID: parentNode, Endpoint: parent.serve(t).URL},
			}
			if configured {
				configs[childNode] = discovery.PeerConfig{NodeID: childNode, Endpoint: childServer.URL}
			}
			sign := peerCollectorSigner(t)
			childSignAttempts := 0
			f.server.peers = discovery.NewPeerDirectory(configs, nil, 2*time.Second,
				func(ctx context.Context, cfg discovery.PeerConfig, r *http.Request) error {
					if cfg.NodeID == childNode {
						childSignAttempts++
					}
					trust, err := f.server.store.PeerTrust(ctx, f.org, cfg.NodeID)
					if err != nil {
						return err
					}
					if trust.State != discovery.MemberApproved {
						return errors.New("peer audience is not approved")
					}
					return sign(ctx, cfg, r)
				})

			out := createScope(t, f, map[string]any{"operation": "search"})
			want := discovery.UnknownSubtree{NodeID: childNode, Reason: "denied"}
			if len(out.Manifest.UnexpandedSubtrees) != 1 || out.Manifest.UnexpandedSubtrees[0] != want {
				t.Errorf("locally revoked child must have reason denied: got %+v", out.Manifest.UnexpandedSubtrees)
			}
			if childSignAttempts != 0 {
				t.Errorf("locally revoked child must not reach signer: got %d attempts", childSignAttempts)
			}
			if child.count() != 0 {
				t.Errorf("locally revoked child must receive zero requests: got %d", child.count())
			}
			if out.Manifest.EnumerationState != "partial" || out.TotalTargets != 1 ||
				len(out.Manifest.ExpandedMembers) != 1 || out.Manifest.ExpandedMembers[0].OriginNodeID != parentNode {
				t.Fatalf("revocation must preserve the gap and observed parent target: %+v", out.Manifest)
			}
			read := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", "/api/v1/federation/scopes/"+out.Manifest.ScopeID, f.aliceToken, nil), 200)
			if len(read.Manifest.UnexpandedSubtrees) != 1 || read.Manifest.UnexpandedSubtrees[0] != want ||
				read.Manifest.ManifestDigest != out.Manifest.ManifestDigest {
				t.Errorf("persisted scope must retain the denied subtree: %+v", read.Manifest)
			}
		})
	}
}

func TestScopeCreateNeverContactsRevokedMember(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	nodeID := registration.Descriptor.NodeID
	fake := newFakePeerServer(nodeID, nil, []discovery.CollectionRef{{CollectionID: "revoked-col", OriginNodeID: nodeID}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{nodeID: fake.serve(t)})
	decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/nodes/"+nodeID+"/revoke", f.adminToken, nil), 200)
	out := createScope(t, f, map[string]any{"operation": "search"})
	if fake.count() != 0 {
		t.Fatal("a locally revoked member was contacted")
	}
	if out.TotalTargets != 0 || out.Manifest.EnumerationState != "sealed" {
		t.Fatalf("revoked member leaked into the scope: %+v", out.Manifest)
	}
}

func TestScopeCreateRemoteDeniedKeepsObservedTargets(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	nodeID := registration.Descriptor.NodeID
	denied := newFakePeerServer(nodeID,
		[]discovery.PeerMember{enumerablePeerMember(freshNodeID(t))},
		[]discovery.CollectionRef{{CollectionID: "denied-member-col", OriginNodeID: nodeID}})
	denied.membersState = http.StatusUnauthorized
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{nodeID: denied.serve(t)})
	out := createScope(t, f, map[string]any{"operation": "search"})
	if out.Manifest.EnumerationState != "partial" {
		t.Fatalf("denied scope claimed sealed: %+v", out.Manifest)
	}
	reasons := map[string]string{}
	for _, unknown := range out.Manifest.UnexpandedSubtrees {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons[nodeID] != "denied" {
		t.Fatalf("unexpanded reasons not honest: %+v", out.Manifest.UnexpandedSubtrees)
	}
	if len(out.Manifest.ExpandedMembers) != 1 || out.Manifest.ExpandedMembers[0].CollectionID != "denied-member-col" {
		t.Fatalf("observed target of a denied member was dropped: %+v", out.Manifest.ExpandedMembers)
	}
}

func TestScopeCreateApprovedBoundaryExcludesDirectAndTransitivePeers(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	pRegistration := remoteRegistration(t, true)
	excludedRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, pRegistration)
	approveEnumerableNode(t, f, excludedRegistration)
	pNode := pRegistration.Descriptor.NodeID
	excludedNode := excludedRegistration.Descriptor.NodeID
	childRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, childRegistration)
	bNode, childNode := childRegistration.Descriptor.NodeID, freshNodeID(t)
	p := newFakePeerServer(pNode,
		[]discovery.PeerMember{enumerablePeerMember(bNode), enumerablePeerMember(childNode)},
		[]discovery.CollectionRef{{CollectionID: "p-col", OriginNodeID: pNode}})
	b := newFakePeerServer(bNode, nil, []discovery.CollectionRef{{CollectionID: "b-col", OriginNodeID: bNode}})
	excluded := newFakePeerServer(excludedNode, nil, nil)
	child := newFakePeerServer(childNode, nil, nil)
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{
		pNode: p.serve(t), bNode: b.serve(t), excludedNode: excluded.serve(t), childNode: child.serve(t),
	})
	invalid := requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken,
		map[string]any{"operation": "search", "allowed_node_ids": []string{pNode, bNode}})
	if invalid.Code != http.StatusBadRequest || p.count() != 0 || b.count() != 0 {
		t.Fatal("a boundary omitting the coordinator must fail before contacting peers")
	}
	out := createScope(t, f, map[string]any{
		"operation": "search", "max_members": 2,
		"allowed_node_ids": []string{f.server.nodeIdentity.NodeID(), pNode, bNode},
	})
	if excluded.count() != 0 || child.count() != 0 {
		t.Fatal("directory traversal crossed the approved direct/transitive recipient boundary")
	}
	if out.Manifest.EnumerationState != "partial" || out.TotalTargets != 2 {
		t.Fatalf("boundary exclusions must surface as partial with honest unknowns: %+v", out.Manifest)
	}
	reasons := map[string]string{}
	for _, unknown := range out.Manifest.UnexpandedSubtrees {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons[excludedNode] != "denied" || reasons[childNode] != "denied" {
		t.Fatalf("boundary-excluded members must be denied unknowns: %+v", out.Manifest.UnexpandedSubtrees)
	}
	targets := map[string]string{}
	for _, target := range out.Manifest.ExpandedMembers {
		targets[target.CollectionID] = target.OriginNodeID
	}
	if targets["p-col"] != pNode || targets["b-col"] != bNode {
		t.Fatalf("the approved scope lost its actual collections: %+v", out.Manifest.ExpandedMembers)
	}
	stored := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET",
		"/api/v1/federation/scopes/"+out.Manifest.ScopeID, f.aliceToken, nil), http.StatusOK)
	if stored.Manifest.ManifestDigest != out.Manifest.ManifestDigest || stored.TotalTargets != 2 {
		t.Fatal("the persisted scope differs from the approved frozen denominator")
	}
}

func TestScopeCreateRemoteBudgetStopsRecursionHonestly(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	pNode := registration.Descriptor.NodeID
	childRegistration := remoteRegistration(t, false)
	approveEnumerableNode(t, f, childRegistration)
	bNode := childRegistration.Descriptor.NodeID
	p := newFakePeerServer(pNode,
		[]discovery.PeerMember{enumerablePeerMember(bNode)},
		[]discovery.CollectionRef{{CollectionID: "p-col", OriginNodeID: pNode}})
	b := newFakePeerServer(bNode, nil,
		[]discovery.CollectionRef{{CollectionID: "b-col", OriginNodeID: bNode}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{pNode: p.serve(t), bNode: b.serve(t)})
	out := createScope(t, f, map[string]any{"operation": "search", "max_remote_members": 1})
	if out.Manifest.EnumerationState != "partial" {
		t.Fatalf("budgeted scope claimed sealed: %+v", out.Manifest)
	}
	reasons := map[string]string{}
	for _, unknown := range out.Manifest.UnexpandedSubtrees {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons[bNode] != "budget_exhausted" {
		t.Fatalf("child of a budget-stopped node must be budget_exhausted: %+v", out.Manifest.UnexpandedSubtrees)
	}
	if b.count() != 0 {
		t.Fatal("budget-exhausted node was contacted")
	}
	for _, target := range out.Manifest.ExpandedMembers {
		if target.CollectionID == "b-col" {
			t.Fatal("budget-exhausted node's collections were fabricated")
		}
	}
}

func TestScopeCreateRemoteTimeoutIsHonestAndPartial(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	registration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, registration)
	nodeID := registration.Descriptor.NodeID
	slow := newFakePeerServer(nodeID,
		[]discovery.PeerMember{enumerablePeerMember(freshNodeID(t))},
		[]discovery.CollectionRef{{CollectionID: "slow-col", OriginNodeID: nodeID}})
	slow.delay = 150 * time.Millisecond
	f.server.peers = func() *discovery.PeerDirectory {
		server := slow.serve(t)
		return discovery.NewPeerDirectory(map[string]discovery.PeerConfig{
			nodeID: {NodeID: nodeID, Endpoint: server.URL},
		}, nil, 40*time.Millisecond, peerCollectorSigner(t))
	}()
	out := createScope(t, f, map[string]any{"operation": "search"})
	found := false
	for _, unknown := range out.Manifest.UnexpandedSubtrees {
		if unknown.NodeID == nodeID && unknown.Reason == "timeout" {
			found = true
		}
	}
	if !found || out.Manifest.EnumerationState != "partial" {
		t.Fatalf("timeout not reported honestly: %+v", out.Manifest)
	}
}

func TestScopeCreateFreezesApprovalSetAtMemberSnapshot(t *testing.T) {
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	pRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, pRegistration)
	pNode := pRegistration.Descriptor.NodeID
	// Freeze the member snapshot BEFORE the late node exists: S1 contains only P.
	snap := decodeDiscovery[discovery.Snapshot](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/member-snapshots", f.aliceToken, map[string]any{"page_size": 50}), 201)
	xRegistration := remoteRegistration(t, true)
	approveEnumerableNode(t, f, xRegistration)
	xNode := xRegistration.Descriptor.NodeID
	// P's live directory already lists the post-snapshot approval X.
	p := newFakePeerServer(pNode,
		[]discovery.PeerMember{enumerablePeerMember(xNode)},
		[]discovery.CollectionRef{{CollectionID: "p-col", OriginNodeID: pNode}})
	x := newFakePeerServer(xNode, nil,
		[]discovery.CollectionRef{{CollectionID: "x-col", OriginNodeID: xNode}})
	f.server.peers = peerDirectoryFor(t, map[string]*httptest.Server{pNode: p.serve(t), xNode: x.serve(t)})
	out := createScope(t, f, map[string]any{"operation": "search", "member_snapshot_id": snap.ID})
	if x.count() != 0 {
		t.Fatal("post-snapshot approval contacted directly; the scope must freeze approvals at the snapshot")
	}
	for _, target := range out.Manifest.ExpandedMembers {
		if target.OriginNodeID == xNode {
			t.Fatalf("post-snapshot approval produced a frozen target: %+v", out.Manifest.ExpandedMembers)
		}
	}
}
