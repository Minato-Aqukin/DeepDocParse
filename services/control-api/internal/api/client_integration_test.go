package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/discovery"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

func TestCenterClientRealCorpusAuthAndScope(t *testing.T) {
	if os.Getenv("CENTER_CLIENT_TEST_DATABASE_URL") == "" {
		t.Skip("CENTER_CLIENT_TEST_DATABASE_URL required for real PostgreSQL corpus HTTP integration")
	}
	f := discoveryPGFixture(t)
	python, _ := filepath.Abs("../../../../.venv/bin/python")
	script, _ := filepath.Abs("../../../../services/corpus-api/tests/center_client_server.py")
	ready := filepath.Join(t.TempDir(), "ready")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd := exec.CommandContext(ctx, python, script, ready)
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); _ = cmd.Wait() }()
	var endpoint []byte
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		value, err := os.ReadFile(ready)
		if err == nil {
			endpoint = value
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if len(endpoint) == 0 {
		t.Fatal("corpus HTTP server did not start")
	}
	// Record the actual producer response while forwarding unchanged to real ASGI.
	// An internal 403 silently projected as unknown must fail even with no live models.
	type observation struct {
		status int
		body   []byte
	}
	observations := make(chan observation, 1)
	actualURL, _ := url.Parse(string(endpoint))
	relay := httputil.NewSingleHostReverseProxy(actualURL)
	relay.ModifyResponse = func(resp *http.Response) error {
		if resp.Request.URL.Path == "/internal/capabilities" {
			body, err := io.ReadAll(resp.Body)
			if err != nil {
				return err
			}
			resp.Body.Close()
			resp.Body = io.NopCloser(bytes.NewReader(body))
			observations <- observation{resp.StatusCode, body}
		}
		return nil
	}
	forward := httptest.NewServer(relay)
	defer forward.Close()
	f.server.cfg.CorpusURL = forward.URL
	f.server.corpus, _ = proxy.New("corpus", forward.URL, f.server.cfg.ServiceToken)
	// Actual Python require_service_actor rejects the ordinary user; a canned 200
	// response could not establish this cross-service authentication boundary.
	req, _ := http.NewRequest("GET", string(endpoint)+"/internal/client/protocol", nil)
	req.Header.Set("Authorization", "Bearer internal-test-service")
	req.Header.Set(identity.HeaderActor, f.alice.ID)
	req.Header.Set(identity.HeaderUser, f.alice.ID)
	req.Header.Set(identity.HeaderActorKind, "user")
	req.Header.Set(identity.HeaderOrganization, f.org)
	req.Header.Set(identity.HeaderRole, "contributor")
	denied, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	denied.Body.Close()
	if denied.StatusCode != 403 {
		t.Fatalf("corpus service-only endpoint accepts user: %d", denied.StatusCode)
	}
	handshake := decodeDiscovery[struct {
		Capabilities     []string `json:"capabilities"`
		CapabilityStatus string   `json:"capability_status"`
	}](t,
		requestDiscovery(t, f.handler, "GET", "/api/v1/client/handshake", f.aliceToken, nil), 200)
	select {
	case observed := <-observations:
		if observed.status != 200 {
			t.Fatalf("real capability producer status %d", observed.status)
		}
		profiles, status := discovery.ProjectProfiles(observed.body, f.server.nodeIdentity.NodeID(), time.Now())
		if status != handshake.CapabilityStatus || status != "unknown" || len(profiles) != 0 {
			t.Fatalf("actual producer/Go projection/handshake differ: %s %+v", status, profiles)
		}
	default:
		t.Fatal("handshake never read real capability producer")
	}
	for _, capability := range []string{"client.snapshot", "client.events", "client.receipt", "client.query", "client.windows"} {
		if !slices.Contains(handshake.Capabilities, capability) {
			t.Fatalf("real corpus protocol unavailable: %+v", handshake)
		}
	}
	first := decodeDiscovery[map[string]any](t, requestDiscovery(t, f.handler, "GET", "/api/v1/client/snapshot", f.aliceToken, nil), 200)
	cursor := first["cursor"].(string)
	unchanged := decodeDiscovery[struct {
		Events []map[string]any `json:"events"`
	}](t,
		requestDiscovery(t, f.handler, "GET", "/api/v1/client/events?after="+cursor, f.aliceToken, nil), 200)
	got := unchanged.Events[0]
	if got["cursor"] != cursor || got["sequence"] != first["sequence"] || got["previous_sequence"] != first["sequence"] {
		t.Fatal("unchanged ACK advanced")
	}
	beforeState, _ := json.Marshal(first["state"])
	afterState, _ := json.Marshal(got["state"])
	if string(beforeState) != string(afterState) {
		t.Fatal("unchanged ACK changed state")
	}
	if w := requestDiscovery(t, f.handler, "GET", "/api/v1/client/events?after="+cursor, f.bobToken, nil); w.Code != 410 {
		t.Fatalf("cross-user cursor: %d %s", w.Code, w.Body)
	}
	if w := requestDiscovery(t, f.handler, "POST", "/api/v1/client/commands", f.aliceToken, map[string]any{}); w.Code != 409 {
		t.Fatalf("unapproved remote command: %d", w.Code)
	}
}
