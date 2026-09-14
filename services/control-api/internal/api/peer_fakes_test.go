package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// fakePeerServer serves the control peer wire contract from memory so scope
// expansion can be driven end to end without a second real node. Every request
// is counted; header/count assertions keep credential rules honest.
type fakePeerServer struct {
	nodeID       string
	members      []discovery.PeerMember
	collections  []discovery.CollectionRef
	membersState int
	catalogState int
	delay        time.Duration
	created      time.Time
	mu           sync.Mutex
	requests     int
	paths        []string
	headers      []http.Header
}

func newFakePeerServer(nodeID string, members []discovery.PeerMember, collections []discovery.CollectionRef) *fakePeerServer {
	return &fakePeerServer{nodeID: nodeID, members: members, collections: collections, created: time.Now().UTC().Truncate(time.Second)}
}

func (f *fakePeerServer) serve(t *testing.T) *httptest.Server {
	t.Helper()
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

func (f *fakePeerServer) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.requests
}

func (f *fakePeerServer) countPath(path string) int {
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

// fakeSnapshotMatches applies the rule both real peer endpoints enforce
// (handlePeerMembers, corpus snapshot_page): a follow-up page names the
// snapshot its cursor belongs to, otherwise 400. A fake that accepted bare
// cursors hid a collector that never sent snapshot_id.
func fakeSnapshotMatches(w http.ResponseWriter, r *http.Request, snapshotID, code string) bool {
	q := r.URL.Query()
	got := q.Get("snapshot_id")
	if (got == "" && q.Get("cursor") != "") || (got != "" && got != snapshotID) {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": code}})
		return false
	}
	return true
}

func fakeCursors(count, size int, prefix string) []string {
	pages := (count + size - 1) / size
	out := make([]string, pages+1)
	for i := range out {
		out[i] = fmt.Sprintf("%s%d", prefix, i)
	}
	return out
}

func (f *fakePeerServer) writeMembers(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	status := f.membersState
	f.mu.Unlock()
	if status != 0 && status != http.StatusOK {
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "peer_unauthenticated"}})
		return
	}
	if !fakeSnapshotMatches(w, r, "peer-members-snapshot", "invalid_peer_page") {
		return
	}
	size := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			size = n
		}
	}
	cursors := fakeCursors(len(f.members), size, "m")
	cursor := r.URL.Query().Get("cursor")
	if cursor == "" {
		cursor = cursors[0]
	}
	terminal := cursors[len(cursors)-1]
	page := []discovery.PeerMember{}
	var next *string
	complete := cursor == terminal
	if !complete {
		index, _ := strconv.Atoi(cursor[1:])
		page = f.members[index*size : min((index+1)*size, len(f.members))]
		value := terminal
		if index+1 < len(cursors)-1 {
			value = cursors[index+1]
		}
		next = &value
	}
	json.NewEncoder(w).Encode(discovery.PeerMemberPage{
		AuthorityNodeID: f.nodeID, SnapshotID: "peer-members-snapshot",
		RegistryRevision: 3, CreatedAt: f.created, ExpiresAt: f.created.Add(5 * time.Minute),
		FirstCursor: cursors[0], TerminalCursor: terminal, Members: page, NextCursor: next, Complete: complete,
	})
}

func (f *fakePeerServer) writeCollections(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	status := f.catalogState
	f.mu.Unlock()
	if status != 0 && status != http.StatusOK {
		w.WriteHeader(status)
		json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "catalog_snapshot_invalid"}})
		return
	}
	if !fakeSnapshotMatches(w, r, "peer-catalog-snapshot", "invalid_catalog_request") {
		return
	}
	size := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			size = n
		}
	}
	cursors := fakeCursors(len(f.collections), size, "c")
	cursor := r.URL.Query().Get("cursor")
	if cursor == "" {
		cursor = cursors[0]
	}
	terminal := cursors[len(cursors)-1]
	page := []discovery.CollectionRef{}
	var next *string
	complete := cursor == terminal
	if !complete {
		index, _ := strconv.Atoi(cursor[1:])
		page = f.collections[index*size : min((index+1)*size, len(f.collections))]
		value := terminal
		if index+1 < len(cursors)-1 {
			value = cursors[index+1]
		}
		next = &value
	}
	total := len(f.collections)
	json.NewEncoder(w).Encode(discovery.PeerCatalogPage{
		AuthorityNodeID: f.nodeID, SnapshotID: "peer-catalog-snapshot",
		RegistryRevision: 5, CreatedAt: f.created, ValidUntil: f.created.Add(5 * time.Minute),
		FirstCursor: cursors[0], TerminalCursor: terminal, Total: &total,
		Collections: page, NextCursor: next, Complete: complete,
	})
}

func enumerablePeerMember(nodeID string) discovery.PeerMember {
	return discovery.PeerMember{
		NodeID: nodeID, DescriptorRevision: 1, ValidUntil: time.Now().UTC().Add(time.Hour),
		Endpoints: []discovery.Endpoint{{Purpose: "federation", URL: "https://" + nodeID + ".example/api/v1/federation"}},
		State:     discovery.MemberApproved, Health: discovery.HealthUnknown, AcceptingAdmissions: false,
		ExpansionState: discovery.ExpansionNotRequested,
	}
}

// peerDirectoryFor wires a PeerDirectory directly (no FEDERATION_PEERS JSON) to
// the given fake servers.
func peerDirectoryFor(servers map[string]*httptest.Server) *discovery.PeerDirectory {
	configs := map[string]discovery.PeerConfig{}
	for nodeID, server := range servers {
		configs[nodeID] = discovery.PeerConfig{NodeID: nodeID, Endpoint: server.URL, ServiceToken: "service-" + nodeID, PeerToken: "peer-" + nodeID}
	}
	return discovery.NewPeerDirectory(configs, nil, 2*time.Second)
}
