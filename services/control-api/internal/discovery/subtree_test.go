package discovery

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"
)

// The peer seam asserts public wire routes rather than expansion internals.
func TestExpandScopeDelegatesUnregisteredDescendants(t *testing.T) {
	for _, deep := range []bool{false, true} {
		t.Run(map[bool]string{false: "one relay", true: "two relays"}[deep], func(t *testing.T) {
			parent := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-r", true)}}
			parent.nodeID = "node-p"
			parent.created = time.Now().UTC().Truncate(time.Second)
			reads := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/v1/federation/members":
					parent.writeMembers(w, r)
				case "/api/v1/federation/collections":
					parent.writeCollections(w, r)
				case "/api/v1/federation/subtree":
					reads++
					if r.URL.Query().Get("path") != "node-a" {
						t.Errorf("wrong caller path: %s", r.URL.RawQuery)
					}
					origin := "node-r"
					via := []string{}
					if deep {
						origin = "node-s"
						via = []string{"node-r"}
					}
					targets := []map[string]any{}
					next := any(nil)
					complete := r.URL.Query().Get("cursor") == "end"
					if !complete {
						targets = append(targets, map[string]any{"target_key": TargetKey{OriginNodeID: origin, CollectionID: "leaf", Operation: "search"}, "via_node_ids": via})
						next = "end"
					}
					json.NewEncoder(w).Encode(map[string]any{"authority_node_id": "node-p", "operation": "search", "snapshot_id": "subtree", "created_at": parent.created, "valid_until": parent.created.Add(time.Minute), "first_cursor": "first", "terminal_cursor": "end", "targets": targets, "next_cursor": next, "complete": complete, "registry_revision_vector": []DirectoryRevision{{NodeID: origin, RegistryRevision: 7, FetchedAt: parent.created, DirectoryRef: "collections", SnapshotRef: "leaf-snapshot"}}, "unexpanded_subtrees": []UnknownSubtree{}, "enumeration_state": "sealed", "consumption": map[string]int{"requests": 3, "nodes": 1}})
				default:
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			configs := map[string]PeerConfig{"node-p": {NodeID: "node-p", Endpoint: server.URL}, "node-r": {NodeID: "node-r", Endpoint: server.URL}}
			sign, _ := peerTestSigner(t)
			dir := NewPeerDirectory(configs, nil, 2*time.Second, sign)
			approved := map[string]bool{"node-p": true}
			out := ExpandScope(context.Background(), dir, ExpansionInput{Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a", Operation: "search", ApprovedNodeIDs: approved, MaxTargets: 100, MaxRequests: 20, MaxNodes: 10})
			origin := "node-r"
			expected := []string{"node-p"}
			if deep {
				origin = "node-s"
				expected = append(expected, "node-r")
			}
			if len(out.Targets) != 1 || out.Targets[0].OriginNodeID != origin {
				t.Fatalf("delegated leaf absent: %+v", out)
			}
			raw, _ := json.Marshal(out)
			var wire map[string]any
			json.Unmarshal(raw, &wire)
			var routes []struct {
				NodeID string   `json:"node_id"`
				Via    []string `json:"via_node_ids"`
			}
			routeBody, _ := json.Marshal(wire["NodeRoutes"])
			json.Unmarshal(routeBody, &routes)
			if len(routes) != 1 || routes[0].NodeID != origin || !reflect.DeepEqual(routes[0].Via, expected) {
				t.Fatalf("wrong intermediate route: %s", routeBody)
			}
			if reads != 2 {
				t.Fatalf("subtree snapshot pages = %d, want 2", reads)
			}
			if out.Sources[origin] != "node-p" {
				t.Fatalf("authorization root lost: %+v", out.Sources)
			}
			found := false
			for _, rev := range out.Revisions {
				if rev.NodeID == origin && rev.DirectoryRef == "subtree:node-p:collections" {
					found = true
				}
			}
			if !found {
				t.Fatal("unverifiable peer report not identified")
			}
		})
	}
}

func TestExpandScopeRejectsMixedOperationSubtreePages(t *testing.T) {
	parent := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-r", true)}}
	parent.nodeID = "node-p"
	parent.created = time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/federation/members":
			parent.writeMembers(w, r)
		case "/api/v1/federation/collections":
			parent.writeCollections(w, r)
		case "/api/v1/federation/subtree":
			targets := []map[string]any{}
			next := any(nil)
			complete := r.URL.Query().Get("cursor") == "end"
			operation := "search"
			// The terminal page binds a different operation than the first; the
			// puller must reject the mixed snapshot rather than blend it.
			if !complete {
				operation = "exec"
				targets = append(targets, map[string]any{"target_key": TargetKey{OriginNodeID: "node-r", CollectionID: "leaf", Operation: operation}, "via_node_ids": []string{}})
				next = "end"
			}
			json.NewEncoder(w).Encode(map[string]any{"authority_node_id": "node-p", "operation": operation, "snapshot_id": "subtree", "created_at": parent.created, "valid_until": parent.created.Add(time.Minute), "first_cursor": "first", "terminal_cursor": "end", "targets": targets, "next_cursor": next, "complete": complete, "registry_revision_vector": []DirectoryRevision{}, "unexpanded_subtrees": []UnknownSubtree{}, "enumeration_state": "sealed", "consumption": map[string]int{"requests": 1, "nodes": 1}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	configs := map[string]PeerConfig{"node-p": {NodeID: "node-p", Endpoint: server.URL}, "node-r": {NodeID: "node-r", Endpoint: server.URL}}
	sign, _ := peerTestSigner(t)
	dir := NewPeerDirectory(configs, nil, 2*time.Second, sign)
	out := ExpandScope(context.Background(), dir, ExpansionInput{Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a", Operation: "search", ApprovedNodeIDs: map[string]bool{"node-p": true}, MaxTargets: 100, MaxRequests: 20, MaxNodes: 10})
	for _, target := range out.Targets {
		if target.OriginNodeID == "node-r" {
			t.Fatalf("mixed-operation subtree page blended: %+v", out.Targets)
		}
	}
	found := false
	for _, unknown := range out.Unknowns {
		if unknown.NodeID == "node-r" && unknown.Reason == "unknown" {
			found = true
		}
	}
	if !found {
		t.Fatalf("mixed-operation snapshot not reported as unknown: %+v", out.Unknowns)
	}
}

func TestExpandScopeNormalizesForeignSubtreeOperation(t *testing.T) {
	parent := &fakeDirectory{members: []PeerMember{federatedPeerMember("node-r", true)}}
	parent.nodeID = "node-p"
	parent.created = time.Now().UTC().Truncate(time.Second)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/federation/members":
			parent.writeMembers(w, r)
		case "/api/v1/federation/collections":
			parent.writeCollections(w, r)
		case "/api/v1/federation/subtree":
			targets := []map[string]any{}
			next := any(nil)
			complete := r.URL.Query().Get("cursor") == "end"
			if !complete {
				targets = append(targets, map[string]any{"target_key": TargetKey{OriginNodeID: "node-r", CollectionID: "leaf", Operation: "exec"}, "via_node_ids": []string{}})
				next = "end"
			}
			json.NewEncoder(w).Encode(map[string]any{"authority_node_id": "node-p", "operation": "search", "snapshot_id": "subtree", "created_at": parent.created, "valid_until": parent.created.Add(time.Minute), "first_cursor": "first", "terminal_cursor": "end", "targets": targets, "next_cursor": next, "complete": complete, "registry_revision_vector": []DirectoryRevision{}, "unexpanded_subtrees": []UnknownSubtree{}, "enumeration_state": "sealed", "consumption": map[string]int{"requests": 1, "nodes": 1}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	configs := map[string]PeerConfig{"node-p": {NodeID: "node-p", Endpoint: server.URL}, "node-r": {NodeID: "node-r", Endpoint: server.URL}}
	sign, _ := peerTestSigner(t)
	dir := NewPeerDirectory(configs, nil, 2*time.Second, sign)
	out := ExpandScope(context.Background(), dir, ExpansionInput{Members: []Member{federatedMemberDescriptor("node-p", true)}, LocalNodeID: "node-a", Operation: "search", ApprovedNodeIDs: map[string]bool{"node-p": true}, MaxTargets: 100, MaxRequests: 20, MaxNodes: 10})
	if len(out.Targets) != 1 || out.Targets[0].OriginNodeID != "node-r" || out.Targets[0].Operation != "search" {
		t.Fatalf("foreign subtree operation not normalized to caller: %+v", out.Targets)
	}
}
