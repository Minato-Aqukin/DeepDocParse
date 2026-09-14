package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

// This test starts the actual corpus collection router over HTTP with migrated PG
// storage. Neither the catalog producer nor its resource authorization is mocked.
func TestScopeRealCorpusPublishedCatalogAndWithdrawal(t *testing.T) {
	dsn := os.Getenv("SCOPE_CORPUS_DATABASE_URL")
	if dsn == "" {
		t.Skip("SCOPE_CORPUS_DATABASE_URL required for actual corpus catalog integration")
	}
	f := discoveryPGFixture(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	listener.Close()
	python, _ := filepath.Abs("../../../../.venv/bin/python")
	script, _ := filepath.Abs("../../../../services/corpus-api/tests/scope_catalog_server.py")
	fixturePath := filepath.Join(t.TempDir(), "catalog.json")
	ctx, cancel := context.WithCancel(context.Background())
	cmd := exec.CommandContext(ctx, python, script, "--port", fmt.Sprint(port), "--organization", f.org, "--owner", f.alice.ID, "--fixture-file", fixturePath)
	cmd.Env = append(os.Environ(), "SERVICE_TOKEN="+f.server.cfg.ServiceToken, "SCOPE_CORPUS_DATABASE_URL="+dsn)
	cmd.Stderr = os.Stderr
	if err = cmd.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); _ = cmd.Wait() })
	endpoint := fmt.Sprintf("http://127.0.0.1:%d", port)
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: time.Second}
	ready := false
	for deadline := time.Now().Add(15 * time.Second); time.Now().Before(deadline); {
		resp, e := client.Get(endpoint + "/internal/federation/collections")
		if e == nil {
			resp.Body.Close()
			ready = true
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if !ready {
		t.Fatal("actual corpus catalog HTTP process did not start")
	}
	body, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		PublicCollectionIDs []string `json:"public_collection_ids"`
		PrivateCollectionID string   `json:"private_collection_id"`
	}
	if err = json.Unmarshal(body, &fixture); err != nil {
		t.Fatal(err)
	}
	f.server.cfg.CorpusURL = endpoint
	f.server.corpus, _ = proxy.New("corpus", endpoint, f.server.cfg.ServiceToken)
	create := func() discovery.ScopeEnvelope {
		return decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "POST", "/api/v1/federation/scopes", f.aliceToken, map[string]any{"operation": "search", "page_size": 1}), 201)
	}
	scope := create()
	if scope.Manifest.EnumerationState != "sealed" || scope.TotalTargets != 2 || len(scope.Manifest.RegistryRevisionVector) != 2 {
		t.Fatalf("actual producer did not yield sealed scope %+v", scope)
	}
	for _, target := range scope.Manifest.ExpandedMembers {
		if !slices.Contains(fixture.PublicCollectionIDs, target.CollectionID) || target.CollectionID == fixture.PrivateCollectionID || target.OriginNodeID != f.server.nodeIdentity.NodeID() {
			t.Fatalf("unauthorized/invented catalog target %+v", target)
		}
	}
	path := "/api/v1/federation/scopes/" + scope.Manifest.ScopeID + "/targets"
	page := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path, f.aliceToken, nil), 200)
	if len(page.Targets) != 1 || page.Targets[0].State != "not_attempted" || page.Complete {
		t.Fatalf("actual catalog revalidation failed %+v", page)
	}
	withdrawn := page.Targets[0].TargetKey.CollectionID
	// Use the real user-facing control proxy to withdraw one explicit collection.
	req := httptest.NewRequest("POST", "/api/v1/collections/"+withdrawn+"/withdraw", bytes.NewBufferString(`{"expected_revision":2}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+f.aliceToken)
	req.Header.Set("Idempotency-Key", "scope-integration-withdraw")
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("actual collection withdrawal failed %d %s", w.Code, w.Body.String())
	}
	revoked := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path, f.aliceToken, nil), 200)
	if revoked.TotalTargets != 2 || revoked.ManifestDigest != scope.Manifest.ManifestDigest || revoked.Targets[0].State != "revoked" {
		t.Fatalf("actual revocation lost frozen denominator %+v", revoked)
	}
	if revoked.NextCursor == nil {
		t.Fatal("old catalog lost second target")
	}
	other := decodeDiscovery[discovery.ScopeTargetPage](t, requestDiscovery(t, f.handler, "GET", path+"?cursor="+*revoked.NextCursor, f.aliceToken, nil), 200)
	if len(other.Targets) != 1 || other.Targets[0].State != "unreachable" {
		t.Fatalf("other collection falsely reported revoked %+v", other)
	}
	newScope := create()
	if newScope.Manifest.ScopeID == scope.Manifest.ScopeID || newScope.TotalTargets != 1 || newScope.Manifest.EnumerationState != "sealed" || newScope.Manifest.ExpandedMembers[0].CollectionID == withdrawn {
		t.Fatalf("new scope did not reflect actual withdrawal %+v", newScope)
	}
	old := decodeDiscovery[discovery.ScopeEnvelope](t, requestDiscovery(t, f.handler, "GET", strings.TrimSuffix(path, "/targets"), f.aliceToken, nil), 200)
	if old.TotalTargets != 2 || old.Manifest.ManifestDigest != scope.Manifest.ManifestDigest {
		t.Fatal("new scope rewrote original denominator")
	}
}
