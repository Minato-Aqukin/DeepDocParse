package discovery

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"
)

// fakeDirectory serves the peer wire contract from memory. It records every
// request, including headers, so credential and redirect rules can be checked.
type fakeDirectory struct {
	members         []PeerMember
	collections     []CollectionRef
	membersRevision int64
	catalogRevision int64
	pageSize        int
	membersStatus   int
	catalogStatus   int
	delay           time.Duration
	redirectTo      string
	nodeID          string
	created         time.Time
	mu              sync.Mutex
	requests        int
	paths           []string
	headers         []http.Header
}

func (f *fakeDirectory) snapshotHeaders(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
}

func (f *fakeDirectory) cursors(count, pageSize int, prefix string) []string {
	pages := (count + pageSize - 1) / pageSize
	out := make([]string, pages+1)
	for i := range out {
		out[i] = fmt.Sprintf("%s%d", prefix, i)
	}
	return out
}

func (f *fakeDirectory) writeMembers(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	status, redirect := f.membersStatus, f.redirectTo
	f.mu.Unlock()
	if redirect != "" {
		http.Redirect(w, r, redirect, http.StatusFound)
		return
	}
	if status != 0 && status != http.StatusOK {
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "peer_unauthenticated"}})
		return
	}
	f.mu.Lock()
	size := f.pageSize
	f.mu.Unlock()
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			size = n
		}
	}
	if size == 0 {
		size = 50
	}
	// The real handler (handlePeerMembers) only accepts a cursor together with
	// the snapshot it belongs to; a cursor-only follow-up is a 400.
	if !f.snapshotMatches(w, r, "members-snapshot", "invalid_peer_page") {
		return
	}
	f.mu.Lock()
	if r.URL.Query().Get("snapshot_id") == "" {
		f.pageSize = size
	}
	f.mu.Unlock()
	cursors := f.cursors(len(f.members), size, "m")
	cursor := r.URL.Query().Get("cursor")
	if cursor == "" {
		cursor = cursors[0]
	}
	terminal := cursors[len(cursors)-1]
	page := []PeerMember{}
	var next *string
	complete := false
	if cursor == terminal {
		complete = true
	} else {
		index, err := strconv.Atoi(cursor[1:])
		if err != nil || index < 0 || index >= len(cursors)-1 {
			http.Error(w, "bad cursor", http.StatusInternalServerError)
			return
		}
		end := min((index+1)*size, len(f.members))
		page = f.members[index*size : end]
		if index+1 < len(cursors)-1 {
			value := cursors[index+1]
			next = &value
		} else {
			value := terminal
			next = &value
		}
	}
	created := f.created
	if created.IsZero() {
		created = time.Now().UTC()
	}
	f.snapshotHeaders(w)
	json.NewEncoder(w).Encode(PeerMemberPage{
		AuthorityNodeID: f.authority(), SnapshotID: "members-snapshot",
		RegistryRevision: max(f.membersRevision, 1), CreatedAt: created, ExpiresAt: created.Add(5 * time.Minute),
		FirstCursor: cursors[0], TerminalCursor: terminal,
		Members: page, NextCursor: next, Complete: complete,
	})
}

func (f *fakeDirectory) writeCollections(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	status, redirect := f.catalogStatus, f.redirectTo
	f.mu.Unlock()
	if redirect != "" {
		http.Redirect(w, r, redirect, http.StatusFound)
		return
	}
	if status != 0 && status != http.StatusOK {
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "catalog_snapshot_invalid"}})
		return
	}
	f.mu.Lock()
	size := f.pageSize
	f.mu.Unlock()
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			size = n
		}
	}
	if size == 0 {
		size = 50
	}
	// corpus snapshot_page rejects a cursor without its snapshot_id the same way.
	if !f.snapshotMatches(w, r, "catalog-snapshot", "invalid_catalog_request") {
		return
	}
	f.mu.Lock()
	if r.URL.Query().Get("snapshot_id") == "" {
		f.pageSize = size
	}
	f.mu.Unlock()
	cursors := f.cursors(len(f.collections), size, "c")
	cursor := r.URL.Query().Get("cursor")
	if cursor == "" {
		cursor = cursors[0]
	}
	terminal := cursors[len(cursors)-1]
	page := []CollectionRef{}
	var next *string
	complete := false
	if cursor == terminal {
		complete = true
	} else {
		index, err := strconv.Atoi(cursor[1:])
		if err != nil || index < 0 || index >= len(cursors)-1 {
			http.Error(w, "bad cursor", http.StatusInternalServerError)
			return
		}
		end := min((index+1)*size, len(f.collections))
		page = f.collections[index*size : end]
		if index+1 < len(cursors)-1 {
			value := cursors[index+1]
			next = &value
		} else {
			value := terminal
			next = &value
		}
	}
	total := len(f.collections)
	created := f.created
	if created.IsZero() {
		created = time.Now().UTC()
	}
	f.snapshotHeaders(w)
	json.NewEncoder(w).Encode(PeerCatalogPage{
		AuthorityNodeID: f.authority(), SnapshotID: "catalog-snapshot",
		RegistryRevision: max(f.catalogRevision, 1), CreatedAt: created, ValidUntil: created.Add(5 * time.Minute),
		FirstCursor: cursors[0], TerminalCursor: terminal, Total: &total,
		Collections: page, NextCursor: next, Complete: complete,
	})
}

// snapshotMatches mirrors the peer wire rule both real endpoints enforce: a
// follow-up page must name the snapshot its cursor belongs to.
func (f *fakeDirectory) snapshotMatches(w http.ResponseWriter, r *http.Request, snapshotID, code string) bool {
	q := r.URL.Query()
	got := q.Get("snapshot_id")
	if (got == "" && q.Get("cursor") != "") || (got != "" && got != snapshotID) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code}})
		return false
	}
	return true
}

func (f *fakeDirectory) authority() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.nodeID
}

// nodeID is assigned when the server starts so pages claim the contacted node.
func (f *fakeDirectory) serve(t *testing.T, nodeID string) *httptest.Server {
	t.Helper()
	f.mu.Lock()
	f.nodeID = nodeID
	f.created = time.Now().UTC().Truncate(time.Second)
	f.mu.Unlock()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.requests++
		f.paths = append(f.paths, r.URL.Path)
		f.headers = append(f.headers, r.Header.Clone())
		delay := f.delay
		f.mu.Unlock()
		if delay > 0 {
			time.Sleep(delay)
		}
		switch r.URL.Path {
		case "/api/v1/federation/members":
			f.writeMembers(w, r)
		case "/api/v1/federation/collections":
			f.writeCollections(w, r)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func (f *fakeDirectory) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.requests
}

func (f *fakeDirectory) countPath(path string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	total := 0
	for _, p := range f.paths {
		if p == path {
			total++
		}
	}
	return total
}

func federatedPeerMember(nodeID string, enumerable bool) PeerMember {
	state := ExpansionUnexpanded
	if enumerable {
		state = ExpansionNotRequested
	}
	return PeerMember{
		NodeID: nodeID, DescriptorRevision: 1, ValidUntil: time.Now().UTC().Add(time.Hour),
		Endpoints: []Endpoint{{Purpose: "federation", URL: "https://" + nodeID + ".example/api/v1/federation"}},
		State:     MemberApproved, Health: HealthUnknown, AcceptingAdmissions: false,
		ExpansionState: state,
	}
}

func federatedMemberDescriptor(nodeID string, enumerable bool) Member {
	descriptor := NodeDescriptor{
		Schema: "ddp-discovery/1#NodeDescriptor", NodeID: nodeID,
		ProtocolVersions:      []string{"ddp-discovery/1"},
		ControlledEndpoints:   []Endpoint{{Purpose: "federation", URL: "https://" + nodeID + ".example/api/v1/federation"}},
		AuthMethods:           []string{"future_node_credentials"},
		DiscoveryCapabilities: DiscoveryCapabilities{EnumerateMembers: enumerable},
		Revision:              1, ValidUntil: time.Now().UTC().Add(time.Hour),
	}
	return Member{NodeID: nodeID, State: MemberApproved, Revision: 1, Descriptor: &descriptor, Configured: true, Health: HealthUnknown, ExpansionState: ExpansionNotRequested}
}

func directoryFor(t *testing.T, peers map[string]string) *PeerDirectory {
	t.Helper()
	configs := map[string]PeerConfig{}
	for nodeID, endpoint := range peers {
		configs[nodeID] = PeerConfig{NodeID: nodeID, Endpoint: endpoint}
	}
	sign, _ := peerTestSigner(t)
	return NewPeerDirectory(configs, nil, 2*time.Second, sign)
}

func TestExpandScopeRecursesThroughDirectoryAndCatalogs(t *testing.T) {
	p := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-b", true)}, collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}}, membersRevision: 4, catalogRevision: 7}
	pServer := p.serve(t, "node-p")
	b := &fakeDirectory{collections: []CollectionRef{{CollectionID: "b-col", OriginNodeID: "node-b"}}, membersRevision: 2, catalogRevision: 3}
	bServer := b.serve(t, "node-b")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-b": bServer.URL})

	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	targets := map[string]string{}
	for _, target := range out.Targets {
		targets[target.CollectionID] = target.OriginNodeID
	}
	if targets["p-col"] != "node-p" || targets["b-col"] != "node-b" {
		t.Fatalf("missing or wrong-origin targets: %+v", out.Targets)
	}
	if out.Sources["node-b"] != "node-p" || out.Sources["node-p"] != "node-p" {
		t.Fatalf("authorization roots not recorded: %+v", out.Sources)
	}
	if len(out.Unknowns) != 0 || !out.Handled["node-p"] {
		t.Fatalf("expansion reported false gaps: %+v", out)
	}
	refs := map[string]int{}
	for _, revision := range out.Revisions {
		refs[revision.NodeID+"/"+revision.DirectoryRef]++
	}
	for _, want := range []string{"node-p/members", "node-p/collections", "node-b/members", "node-b/collections"} {
		if refs[want] != 1 {
			t.Fatalf("missing revision vector entry %s: %+v", want, out.Revisions)
		}
	}
	if len(out.Children) != 2 || out.Children[0].NodeID != "node-p" || out.Children[1].NodeID != "node-b" || out.Children[0].EnumerationState != "sealed" {
		t.Fatalf("child manifests wrong: %+v", out.Children)
	}
	if p.count() != 4 || b.count() != 3 {
		t.Fatalf("expected both directories fully consumed once: p=%d b=%d", p.count(), b.count())
	}
}

func TestExpandScopeStopsCyclesAndDuplicatePaths(t *testing.T) {
	p := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-b", true)}, collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}}}
	pServer := p.serve(t, "node-p")
	b := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-p", true), federatedPeerMember("node-a", true), federatedPeerMember("node-r", true)}, collections: []CollectionRef{{CollectionID: "b-col", OriginNodeID: "node-b"}}}
	bServer := b.serve(t, "node-b")
	r := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-b", true)}, collections: []CollectionRef{{CollectionID: "r-col", OriginNodeID: "node-r"}}}
	rServer := r.serve(t, "node-r")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-b": bServer.URL, "node-r": rServer.URL})

	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true), federatedMemberDescriptor("node-r", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if len(out.Targets) != 3 || len(out.Unknowns) != 0 {
		t.Fatalf("cycle produced false unknown subtrees or wrong targets: %+v", out)
	}
	if b.countPath("/api/v1/federation/members") != 2 || b.countPath("/api/v1/federation/collections") != 2 {
		t.Fatalf("duplicate path re-queried node-b: %d", b.count())
	}
	if refs := len(out.Children); refs != 3 {
		t.Fatalf("child manifests should cover p, r, b exactly once: %+v", out.Children)
	}
	for _, child := range out.Children {
		if child.NodeID == "node-a" {
			t.Fatal("local node was recursively re-entered")
		}
	}
	if out.Sources["node-b"] != "node-p" {
		t.Fatalf("first discovery path must own the root: %+v", out.Sources)
	}
}

func TestExpandScopeBudgetKeepsObservedTargetsAndMarksPartial(t *testing.T) {
	p := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-b", true)}, collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}}}
	pServer := p.serve(t, "node-p")
	r := &fakeDirectory{collections: []CollectionRef{{CollectionID: "r-col", OriginNodeID: "node-r"}}}
	rServer := r.serve(t, "node-r")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-r": rServer.URL})

	// Two requests exactly consume the members pages of node-p; the catalog page
	// is observed but its terminal page is not. The observed target stays.
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true), federatedMemberDescriptor("node-r", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 3, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if p.count() != 3 || r.count() != 0 {
		t.Fatalf("budget not enforced at request level: p=%d r=%d", p.count(), r.count())
	}
	if len(out.Targets) != 1 || out.Targets[0].CollectionID != "p-col" {
		t.Fatalf("observed target was dropped: %+v", out.Targets)
	}
	found := false
	for _, unknown := range out.Unknowns {
		if unknown.NodeID == "node-p" && unknown.Reason == "budget_exhausted" {
			found = true
		}
		if unknown.NodeID == "node-r" && unknown.Reason != "budget_exhausted" {
			t.Fatalf("uncontacted node must be budget_exhausted: %+v", unknown)
		}
	}
	if !found {
		t.Fatalf("missing budget_exhausted reason: %+v", out.Unknowns)
	}
}

func TestExpandScopeMemberPageBudgetKeepsObservedMembers(t *testing.T) {
	members := make([]PeerMember, 150)
	for i := range members {
		members[i] = federatedPeerMember(fmt.Sprintf("node-%03d", i), false)
	}
	p := &fakeDirectory{members: members, membersRevision: 1, catalogRevision: 1}
	pServer := p.serve(t, "node-p")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL})
	// Page size is 100: the first two member pages are observed, the terminal page
	// is not, and no collection request may be issued after the budget is hit.
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 2, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if p.countPath("/api/v1/federation/members") != 2 || p.countPath("/api/v1/federation/collections") != 0 {
		t.Fatalf("member page budget not enforced: members=%d catalogs=%d", p.countPath("/api/v1/federation/members"), p.countPath("/api/v1/federation/collections"))
	}
	if len(out.Unknowns) != 1 || out.Unknowns[0].Reason != "budget_exhausted" {
		t.Fatalf("member budget exhaustion not honest: %+v", out.Unknowns)
	}
}

func TestExpandScopeNodeBudgetBoundsContactedNodes(t *testing.T) {
	p := &fakeDirectory{collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}}}
	pServer := p.serve(t, "node-p")
	r := &fakeDirectory{collections: []CollectionRef{{CollectionID: "r-col", OriginNodeID: "node-r"}}}
	rServer := r.serve(t, "node-r")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-r": rServer.URL})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true), federatedMemberDescriptor("node-r", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 1, Now: time.Now().UTC(),
	})
	if p.countPath("/api/v1/federation/collections") == 0 || r.count() != 0 {
		t.Fatalf("node budget not enforced: p=%d r=%d", p.count(), r.count())
	}
	if len(out.Targets) != 1 || out.Targets[0].OriginNodeID != "node-p" {
		t.Fatalf("first node targets lost: %+v", out.Targets)
	}
	for _, unknown := range out.Unknowns {
		if unknown.NodeID == "node-r" && unknown.Reason != "budget_exhausted" {
			t.Fatalf("second node must be budget_exhausted: %+v", unknown)
		}
	}
}

func TestExpandScopeDeniedAndUnregisteredStayUncontacted(t *testing.T) {
	p := &fakeDirectory{membersStatus: http.StatusUnauthorized}
	pServer := p.serve(t, "node-p")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true), federatedMemberDescriptor("node-unregistered", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	denied := false
	unconfigured := false
	for _, unknown := range out.Unknowns {
		if unknown.NodeID == "node-p" && unknown.Reason == "denied" {
			denied = true
		}
		if unknown.NodeID == "node-unregistered" && unknown.Reason == "unknown" {
			unconfigured = true
		}
	}
	if !denied || !unconfigured {
		t.Fatalf("honest reasons wrong: %+v", out.Unknowns)
	}
	if p.countPath("/api/v1/federation/collections") == 0 {
		t.Fatal("denied peer directory should still be tried for its own catalog")
	}
}

func TestExpandScopeTimeoutIsReportedHonestly(t *testing.T) {
	p := &fakeDirectory{delay: 150 * time.Millisecond}
	pServer := p.serve(t, "node-p")
	configs := map[string]PeerConfig{"node-p": {NodeID: "node-p", Endpoint: pServer.URL}}
	sign, _ := peerTestSigner(t)
	dir := NewPeerDirectory(configs, nil, 40*time.Millisecond, sign)
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if len(out.Targets) != 0 || len(out.Unknowns) != 1 || out.Unknowns[0].Reason != "timeout" {
		t.Fatalf("timeout must produce one honest unknown: %+v", out)
	}
}

func TestExpandScopeNonEnumerableDirectMemberStillPublishesCollections(t *testing.T) {
	leaf := &fakeDirectory{collections: []CollectionRef{{CollectionID: "leaf-col", OriginNodeID: "node-leaf"}}}
	leafServer := leaf.serve(t, "node-leaf")
	dir := directoryFor(t, map[string]string{"node-leaf": leafServer.URL})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-leaf", false)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if len(out.Targets) != 1 || out.Targets[0].CollectionID != "leaf-col" {
		t.Fatalf("leaf collections not observed: %+v", out.Targets)
	}
	if leaf.countPath("/api/v1/federation/members") != 0 {
		t.Fatal("member-enumeration was requested from a directory that declared it unsupported")
	}
	if len(out.Unknowns) != 1 || out.Unknowns[0].Reason != "enumeration_unsupported" {
		t.Fatalf("subtree gap not declared: %+v", out.Unknowns)
	}
}

func TestExpandScopeUnknownMemberReasonMatchesLocalFallback(t *testing.T) {
	dir := directoryFor(t, map[string]string{})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-missing", true), federatedMemberDescriptor("node-leaf", false)}, LocalNodeID: "node-a",
		Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	reasons := map[string]string{}
	for _, unknown := range out.Unknowns {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons["node-missing"] != "unknown" || reasons["node-leaf"] != "enumeration_unsupported" {
		t.Fatalf("fallback reasons drifted: %+v", reasons)
	}
}

func TestExpandScopeLocalRevocationPrecedesChildMetadata(t *testing.T) {
	for _, shape := range []string{"approved", "expired", "missing_endpoint", "non_enumerable"} {
		t.Run(shape, func(t *testing.T) {
			child := federatedPeerMember("node-revoked", true)
			switch shape {
			case "expired":
				child.ValidUntil = time.Now().UTC().Add(-time.Hour)
			case "missing_endpoint":
				child.Endpoints = nil
			case "non_enumerable":
				child.ExpansionState = ExpansionUnexpanded
			}
			parent := &fakeDirectory{members: []PeerMember{child}}
			parentServer := parent.serve(t, "node-parent")
			revoked := &fakeDirectory{}
			revokedServer := revoked.serve(t, "node-revoked")
			dir := directoryFor(t, map[string]string{"node-parent": parentServer.URL, "node-revoked": revokedServer.URL})

			out := ExpandScope(context.Background(), dir, ExpansionInput{
				Members:        []Member{federatedMemberDescriptor("node-parent", true)},
				RevokedNodeIDs: map[string]bool{"node-revoked": true}, LocalNodeID: "node-local",
				Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32,
			})
			want := UnknownSubtree{NodeID: "node-revoked", Reason: "denied"}
			if len(out.Unknowns) != 1 || out.Unknowns[0] != want {
				t.Fatalf("local revocation must override child metadata: %+v", out.Unknowns)
			}
			if revoked.count() != 0 {
				t.Fatalf("locally revoked child received %d requests", revoked.count())
			}
		})
	}
}

func TestExpandScopeUnconfiguredChildrenWithoutLocalRevocationStayUnknown(t *testing.T) {
	parent := &fakeDirectory{members: []PeerMember{
		federatedPeerMember("node-unregistered", true),
		federatedPeerMember("node-approved", true),
	}}
	parentServer := parent.serve(t, "node-parent")
	dir := directoryFor(t, map[string]string{"node-parent": parentServer.URL})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members:     []Member{federatedMemberDescriptor("node-parent", true), federatedMemberDescriptor("node-approved", true)},
		LocalNodeID: "node-local", Operation: "search", MaxTargets: 100, MaxRequests: 64, MaxNodes: 32,
	})
	reasons := map[string]string{}
	for _, subtree := range out.Unknowns {
		reasons[subtree.NodeID] = subtree.Reason
	}
	if len(reasons) != 2 || reasons["node-unregistered"] != "unknown" || reasons["node-approved"] != "unknown" {
		t.Fatalf("unconfigured children without local revocation changed reason: %+v", out.Unknowns)
	}
}

func TestExpandScopeFrozenApprovalSetExcludesPostSnapshotApprovals(t *testing.T) {
	p := &fakeDirectory{
		members:     []PeerMember{federatedPeerMember("node-x", true)},
		collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}},
	}
	pServer := p.serve(t, "node-p")
	x := &fakeDirectory{collections: []CollectionRef{{CollectionID: "x-col", OriginNodeID: "node-x"}}}
	xServer := x.serve(t, "node-x")
	dir := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-x": xServer.URL})

	// node-x is approved after the snapshot froze: it is missing from the frozen
	// ApprovedNodeIDs even though P's live directory already lists it. P 404s the
	// subtree read, so node-x must surface as unknown via the subtree path and
	// never receive a direct directory or catalog pull.
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a",
		Operation: "search", ApprovedNodeIDs: map[string]bool{"node-p": true},
		MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if x.count() != 0 {
		t.Fatalf("post-snapshot approval contacted directly: %d requests %v", x.count(), x.paths)
	}
	reasons := map[string]string{}
	for _, unknown := range out.Unknowns {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons["node-x"] != "unknown" {
		t.Fatalf("post-snapshot approval must arrive as unknown via the subtree path: %+v", out.Unknowns)
	}
	for _, target := range out.Targets {
		if target.OriginNodeID == "node-x" {
			t.Fatalf("post-snapshot approval produced a frozen target: %+v", out.Targets)
		}
	}

	// Control case: when the frozen set already contains node-x, the same live
	// directory is contacted directly for both its directory and its catalog.
	x2 := &fakeDirectory{collections: []CollectionRef{{CollectionID: "x-col", OriginNodeID: "node-x"}}}
	x2Server := x2.serve(t, "node-x")
	dir2 := directoryFor(t, map[string]string{"node-p": pServer.URL, "node-x": x2Server.URL})
	control := ExpandScope(context.Background(), dir2, ExpansionInput{
		Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a",
		Operation: "search", ApprovedNodeIDs: map[string]bool{"node-p": true, "node-x": true},
		MaxTargets: 100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	if x2.countPath("/api/v1/federation/members") == 0 || x2.countPath("/api/v1/federation/collections") == 0 {
		t.Fatalf("frozen approval not contacted directly: members=%d catalogs=%d", x2.countPath("/api/v1/federation/members"), x2.countPath("/api/v1/federation/collections"))
	}
	found := false
	for _, target := range control.Targets {
		if target.OriginNodeID == "node-x" && target.CollectionID == "x-col" {
			found = true
		}
	}
	if !found {
		t.Fatalf("frozen approval produced no direct target: %+v", control.Targets)
	}
}

func TestExpandScopeAllowedBoundaryRecordsDeniedWithoutBudget(t *testing.T) {
	p := &fakeDirectory{
		members:     []PeerMember{federatedPeerMember("node-direct-excluded", true), federatedPeerMember("node-child", true)},
		collections: []CollectionRef{{CollectionID: "p-col", OriginNodeID: "node-p"}},
	}
	pServer := p.serve(t, "node-p")
	child := &fakeDirectory{
		members:     []PeerMember{federatedPeerMember("node-transitive-excluded", true)},
		collections: []CollectionRef{{CollectionID: "child-col", OriginNodeID: "node-child"}},
	}
	childServer := child.serve(t, "node-child")
	excluded := &fakeDirectory{}
	excludedServer := excluded.serve(t, "node-direct-excluded")
	transitive := &fakeDirectory{}
	transitiveServer := transitive.serve(t, "node-transitive-excluded")
	dir := directoryFor(t, map[string]string{
		"node-p": pServer.URL, "node-child": childServer.URL,
		"node-direct-excluded": excludedServer.URL, "node-transitive-excluded": transitiveServer.URL,
	})
	out := ExpandScope(context.Background(), dir, ExpansionInput{
		Members: []Member{
			federatedMemberDescriptor("node-p", true),
			federatedMemberDescriptor("node-direct-excluded", true),
		},
		LocalNodeID: "node-a", Operation: "search",
		AllowedNodeIDs: []string{"node-a", "node-p", "node-child"},
		MaxTargets:     100, MaxRequests: 64, MaxNodes: 32, Now: time.Now().UTC(),
	})
	reasons := map[string]string{}
	for _, unknown := range out.Unknowns {
		reasons[unknown.NodeID] = unknown.Reason
	}
	if reasons["node-direct-excluded"] != "denied" || reasons["node-transitive-excluded"] != "denied" {
		t.Fatalf("boundary exclusions must be denied unknowns: %+v", out.Unknowns)
	}
	if excluded.count() != 0 || transitive.count() != 0 {
		t.Fatalf("boundary-excluded nodes contacted: direct=%d transitive=%d", excluded.count(), transitive.count())
	}
	if out.Consumption.Requests != p.count()+child.count() {
		t.Fatalf("denied unknowns consumed budget: %+v", out.Consumption)
	}
	found := false
	for _, target := range out.Targets {
		if target.OriginNodeID == "node-transitive-excluded" {
			found = true
		}
	}
	if found {
		t.Fatalf("boundary-excluded origin produced a target: %+v", out.Targets)
	}
}
