package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"strconv"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
)

// Opt-in T64 measurement, driven by scripts/scale_storage_experiment.py.
// Uses production registration, approval, snapshot and scope HTTP handlers.
func TestScaleStorageExperiment(t *testing.T) {
	output := os.Getenv("DDP_SCALE_OUTPUT")
	if output == "" {
		t.Skip("run scripts/scale_storage_experiment.py for isolated T64 measurements")
	}
	n, err := strconv.Atoi(os.Getenv("DDP_SCALE_N"))
	if err != nil || n < 1 || n > 200 {
		t.Fatal("DDP_SCALE_N must be in 1..200")
	}
	f := discoveryPGFixture(t)
	verifiedEmptyCatalog(t, f)
	measure := func() map[string]map[string]int64 {
		result := map[string]map[string]int64{}
		rows, err := f.server.store.Pool().Query(context.Background(), `SELECT tablename FROM pg_tables WHERE schemaname='control' ORDER BY tablename`)
		if err != nil {
			t.Fatal(err)
		}
		var tables []string
		for rows.Next() {
			var name string
			if err := rows.Scan(&name); err != nil {
				t.Fatal(err)
			}
			tables = append(tables, name)
		}
		rows.Close()
		if err := rows.Err(); err != nil {
			t.Fatal(err)
		}
		for _, name := range tables {
			var count, size, payload int64
			query := fmt.Sprintf(`SELECT count(*),coalesce(sum(pg_column_size(t)),0),pg_total_relation_size('control.%s') FROM control.%s t`, name, name)
			if err := f.server.store.Pool().QueryRow(context.Background(), query).Scan(&count, &payload, &size); err != nil {
				t.Fatal(err)
			}
			result[name] = map[string]int64{"rows": count, "relation_bytes": size, "row_payload_bytes": payload}
		}
		return result
	}
	before := measure()
	servers := map[string]*httptest.Server{}
	peers := []*fakePeerServer{}
	for i := range n {
		registration := remoteRegistration(t, true)
		approveEnumerableNode(t, f, registration)
		node := registration.Descriptor.NodeID
		peer := newFakePeerServer(node, nil, []discovery.CollectionRef{{CollectionID: fmt.Sprintf("scale-col-%03d", i), OriginNodeID: node}})
		servers[node] = peer.serve(t)
		peers = append(peers, peer)
	}
	f.server.peers = peerDirectoryFor(t, servers)
	registered := measure()
	options := map[string]any{"operation": "corpus.retrieve", "max_members": 256, "max_remote_members": 256, "max_discovery_requests": 1024, "page_size": 50, "ttl_seconds": 900}
	first := createScope(t, f, options)
	if first.TotalTargets != n || first.Manifest.EnumerationState != "sealed" {
		t.Fatalf("expected sealed %d-target scope, got %+v", n, first)
	}
	after := measure()
	// Expiry is deliberately observed rather than assumed to mean deletion.
	if _, err := f.server.store.Pool().Exec(context.Background(), `UPDATE control.member_snapshots SET expires_at=now()-interval '1 second'; UPDATE control.scope_manifests SET valid_until=now()-interval '1 second'`); err != nil {
		t.Fatal(err)
	}
	second := createScope(t, f, options)
	if second.TotalTargets != n {
		t.Fatalf("second scope targets: %d", second.TotalTargets)
	}
	repeated := measure()
	// T64 bound: the renewal housekeeping seam purges directory projections
	// only past the retention window (0 = immediately). A purged scope reads
	// back 404, never a fabricated empty denominator.
	if _, err := f.server.store.SweepDiscoveryMetadata(context.Background(), 0, 5000); err != nil {
		t.Fatal(err)
	}
	swept := measure()
	requests := 0
	for _, peer := range peers {
		requests += peer.count()
	}
	body, err := json.MarshalIndent(map[string]any{"n": n, "local_node_id": f.server.nodeIdentity.NodeID(), "manifest": second.Manifest, "options": options, "before": before, "after_registration": registered, "after_discovery": after, "after_expiry_and_second_scope": repeated, "after_retention_sweep": swept, "peer_http_requests_two_scopes": requests}, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(output, body, 0600); err != nil {
		t.Fatal(err)
	}
}
