package api

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

func peerIngressServer(t *testing.T, corpus http.Handler) *Server {
	t.Helper()
	target := httptest.NewServer(corpus)
	t.Cleanup(target.Close)
	upstream, err := proxy.New("corpus-api", target.URL, "internal-secret-never-forward")
	if err != nil {
		t.Fatal(err)
	}
	return &Server{cfg: &config.Config{CorpusURL: target.URL, ServiceToken: "internal-secret-never-forward"}, corpus: upstream}
}

func TestFederationCorpusIngressWithoutSession(t *testing.T) {
	type received struct {
		method, uri, body string
		headers           http.Header
	}
	calls := make(chan received, 1)
	s := peerIngressServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Like corpus, this peer endpoint accepts only node credentials, never a
		// session or the control service's token in place of peer authentication.
		if r.Header.Get("X-DDP-Node-Credential") != "signed-peer-credential" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		calls <- received{r.Method, r.URL.RequestURI(), string(body), r.Header.Clone()}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("ETag", "peer-result-v1")
		w.WriteHeader(http.StatusAccepted)
		_, _ = io.WriteString(w, `{"status":"queued"}`)
	}))
	h := s.Routes()
	paths := []struct{ method, path string }{
		{"POST", "/api/v1/federation/probes"},
		{"GET", "/api/v1/federation/probes/probe-1"},
		{"POST", "/api/v1/federation/admissions"},
		{"POST", "/api/v1/federation/admissions/lookup"},
		{"GET", "/api/v1/federation/tasks/execution-1"},
		{"POST", "/api/v1/federation/tasks/execution-1/cancel"},
		{"POST", "/api/v1/federation/resources/locate"},
		{"POST", "/api/v1/federation/results/resolve"},
		{"GET", "/api/v1/federation/evidence-sets/set-1"},
		{"GET", "/api/v1/federation/published-collections"},
	}
	for _, route := range paths {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			uri := route.path + "?cursor=a%2Fb&limit=3&tag=one&tag=two"
			r := httptest.NewRequest(route.method, uri, strings.NewReader(`{"scope_ref":"scope-1"}`))
			peerHeaders := map[string]string{
				"X-DDP-Node-Credential": "signed-peer-credential",
				"X-DDP-Target-Node":     "node-receiver",
				"Idempotency-Key":       "peer-idempotency-1",
				"Content-Type":          "application/json",
				"Accept":                "application/json",
				"X-Request-Id":          "peer-request-1",
			}
			for name, value := range peerHeaders {
				r.Header.Set(name, value)
			}
			for _, name := range identity.Inbound {
				r.Header.Set(name, "forged-internal-identity")
			}
			r.Header.Set("Authorization", "Bearer attacker-service-token")
			r.Header.Set("Cookie", "ddp_session=attacker-session")
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != http.StatusAccepted {
				t.Fatalf("peer route did not reach corpus without a session: got %d, want 202", w.Code)
			}
			call := <-calls
			if call.method != route.method || call.uri != uri || call.body != `{"scope_ref":"scope-1"}` {
				t.Fatalf("peer request changed: %+v", call)
			}
			for name, value := range peerHeaders {
				if got := call.headers.Get(name); got != value {
					t.Errorf("peer header %s = %q, want %q", name, got, value)
				}
			}
			for _, name := range append(append([]string{}, identity.Inbound...), "Authorization", "Cookie") {
				if call.headers.Get(name) != "" {
					t.Errorf("internal/session header %s escaped to corpus", name)
				}
			}
			if w.Body.String() != `{"status":"queued"}` || w.Header().Get("ETag") != "peer-result-v1" || w.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("peer response changed: %d %v %s", w.Code, w.Header(), w.Body.String())
			}
		})
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("POST", "/api/v1/federation/probes", nil))
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("corpus credential refusal changed: got %d, want 401", w.Code)
	}
}

func TestFederationCorpusIngressDoesNotBroadenPublicSurface(t *testing.T) {
	var calls atomic.Int32
	s := peerIngressServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusAccepted)
	}))
	h := s.Routes()
	for _, route := range []struct {
		method, path string
		status       int
	}{
		{"POST", "/api/v1/tasks", 401},
		{"GET", "/api/documents", 401},
		{"GET", "/internal/capabilities", 404},
		{"GET", "/internal/actors", 404}, // 公开监听从不服务 /internal/*；内网路由在 InternalRoutes()
		{"GET", "/api/v1/federation/tasksX/execution-1", 404},
		{"POST", "/api/v1/federation/probesX", 404},
		{"POST", "/api/v1/federation/admissionsX/lookup", 404},
		{"POST", "/api/v1/federation/resources/locateX", 404},
		{"POST", "/api/v1/federation/results/resolveX", 404},
		{"GET", "/api/v1/federation/evidence-setsX/set-1", 404},
		{"GET", "/api/v1/federation/published-collectionsX", 404},
		{"POST", "/api/v1/federation/tasks/execution-1/not-a-route", 404},
		{"GET", "/api/v1/federation/nodes", 401},
		{"POST", "/api/v1/federation/member-snapshots", 401},
		{"GET", "/api/v1/federation/members", 503},
		{"GET", "/api/v1/federation/collections", 503},
		{"GET", "/api/v1/federation/generation-descriptor", 503},
		{"GET", "/api/v1/federation/node", 503},
	} {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			r := httptest.NewRequest(route.method, route.path, nil)
			r.Header.Set("X-DDP-Node-Credential", "signed-peer-credential")
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != route.status {
				t.Fatalf("non-peer-corpus route status = %d, want %d", w.Code, route.status)
			}
		})
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("corpus received %d requests outside its peer route allowlist", got)
	}
}

func TestFederationCorpusIngressStreamsBothDirections(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	requestStarted := make(chan struct{})
	responseFinish := make(chan struct{})
	s := peerIngressServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		first := make([]byte, 5)
		if _, err := io.ReadFull(r.Body, first); err != nil || string(first) != "first" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		close(requestStarted)
		rest, err := io.ReadAll(r.Body)
		if err != nil || string(rest) != "-last" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		select {
		case <-responseFinish:
			_, _ = io.WriteString(w, "data: last\n\n")
		case <-r.Context().Done():
		}
	}))
	entry := httptest.NewServer(s.Routes())
	defer entry.Close()
	reader, writer := io.Pipe()
	defer writer.CloseWithError(context.Canceled)
	defer reader.Close()
	req, err := http.NewRequestWithContext(ctx, "POST", entry.URL+"/api/v1/federation/probes", reader)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("X-DDP-Node-Credential", "signed-peer-credential")
	type result struct {
		response *http.Response
		err      error
	}
	done := make(chan result, 1)
	go func() {
		resp, err := entry.Client().Do(req)
		done <- result{resp, err}
	}()
	written := make(chan error, 1)
	go func() {
		_, err := io.WriteString(writer, "first")
		written <- err
	}()
	select {
	case <-requestStarted:
	case <-ctx.Done():
		t.Fatal("corpus did not receive the first chunk before the request body finished")
	}
	if err := <-written; err != nil {
		t.Fatal(err)
	}
	if _, err := io.WriteString(writer, "-last"); err != nil {
		t.Fatal(err)
	}
	_ = writer.Close()
	select {
	case got := <-done:
		if got.err != nil {
			t.Fatal(got.err)
		}
		defer got.response.Body.Close()
		first := make([]byte, len("data: first\n\n"))
		if _, err := io.ReadFull(got.response.Body, first); err != nil || string(first) != "data: first\n\n" {
			t.Fatalf("first response chunk was buffered or changed: %q, %v", first, err)
		}
		close(responseFinish)
		rest, err := io.ReadAll(got.response.Body)
		if err != nil || string(rest) != "data: last\n\n" {
			t.Fatalf("response tail changed: %q, %v", rest, err)
		}
	case <-ctx.Done():
		t.Fatal("entry did not return response headers before corpus finished its response")
	}
}

// The peer routes are public and corpus reads the whole body before checking the
// credential, so the entry must stop oversized bodies — chunked ones too — and
// never pass more than the cap to corpus.
func TestFederationCorpusIngressBoundsPeerRequestBodies(t *testing.T) {
	var reached atomic.Int64
	// Each upstream request reports how many body bytes it received when it finishes, so a
	// subtest never reads (or resets) a counter a previous request's handler still writes.
	finished := make(chan int64, 4)
	s := peerIngressServer(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached.Add(1)
		n, err := io.Copy(io.Discard, r.Body)
		defer func() { finished <- n }()
		if err != nil {
			return
		}
		w.WriteHeader(http.StatusCreated)
	}))
	upstreamReceived := func(t *testing.T) (int64, bool) {
		t.Helper()
		select {
		case n := <-finished:
			return n, true
		case <-time.After(5 * time.Second):
			return 0, false
		}
	}
	entry := httptest.NewServer(s.Routes())
	defer entry.Close()
	send := func(t *testing.T, body io.Reader, length int64) *http.Response {
		t.Helper()
		req, err := http.NewRequest("POST", entry.URL+"/api/v1/federation/admissions", body)
		if err != nil {
			t.Fatal(err)
		}
		req.ContentLength = length // -1 forces chunked transfer encoding
		req.Header.Set("X-DDP-Node-Credential", "bogus-but-present")
		resp, err := entry.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = resp.Body.Close() })
		return resp
	}
	expectTooLarge := func(t *testing.T, resp *http.Response) {
		t.Helper()
		raw, _ := io.ReadAll(resp.Body)
		if resp.StatusCode != http.StatusRequestEntityTooLarge || !strings.Contains(string(raw), `"code":"too_large"`) ||
			!strings.Contains(string(raw), `"type":"invalid_request_error"`) {
			t.Fatalf("oversized peer body: got %d %s, want 413 invalid_request_error/too_large", resp.StatusCode, raw)
		}
	}

	t.Run("declared length", func(t *testing.T) {
		reached.Store(0)
		resp := send(t, strings.NewReader(strings.Repeat("x", int(peerRequestBodyMaxBytes)+1)), peerRequestBodyMaxBytes+1)
		expectTooLarge(t, resp)
		if reached.Load() != 0 {
			t.Fatal("a body declared over the cap must not reach corpus at all")
		}
	})
	t.Run("chunked", func(t *testing.T) {
		reached.Store(0)
		body := io.MultiReader(strings.NewReader(strings.Repeat("y", int(peerRequestBodyMaxBytes))),
			strings.NewReader(strings.Repeat("z", 64<<10)))
		resp := send(t, body, -1)
		expectTooLarge(t, resp)
		n, done := upstreamReceived(t)
		if !done && reached.Load() != 0 {
			t.Fatal("the aborted upstream request never finished")
		}
		if n > peerRequestBodyMaxBytes {
			t.Fatalf("corpus received %d bytes, more than the %d-byte cap", n, peerRequestBodyMaxBytes)
		}
	})
	t.Run("at the cap", func(t *testing.T) {
		resp := send(t, strings.NewReader(strings.Repeat("a", int(peerRequestBodyMaxBytes))), -1)
		n, done := upstreamReceived(t)
		if resp.StatusCode != http.StatusCreated || !done || n != peerRequestBodyMaxBytes {
			t.Fatalf("a body exactly at the cap must pass intact: got %d after %d bytes", resp.StatusCode, n)
		}
	})
}
