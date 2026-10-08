package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
)

// routeTestServer 只装路由表：不连 DB、不起后台循环。
// store/corpus/gateway/mcp 全是 nil —— 测的是"哪条路由被哪道门守着、
// 404 还是 401"，不是下游行为。需要下游行为的用 peerIngressServer 那类夹具。
func routeTestServer() *Server {
	return &Server{cfg: &config.Config{ServiceToken: "route-test-service-token"}}
}

func serveRoute(t *testing.T, h http.Handler, method, path string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest(method, path, nil)
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func svcHeaders() map[string]string {
	return map[string]string{"Authorization": "Bearer route-test-service-token"}
}

// TestPublicListenerNeverServesInternal：公开监听上每条 /internal/* 一律
// 404 —— 带着正确的服务凭据也一样；这些模式只注册在 InternalRoutes() 上。
func TestPublicListenerNeverServesInternal(t *testing.T) {
	h := routeTestServer().Routes()
	for _, route := range []struct{ method, path string }{
		{"POST", "/internal/file-grants"},
		{"GET", "/internal/actors"},
		{"POST", "/internal/usage"},
		{"GET", "/internal/federation/identity"},
		{"POST", "/internal/federation/node-credentials"},
		{"GET", "/internal/federation/peer-keys/some-node"},
	} {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			if got := serveRoute(t, h, route.method, route.path, svcHeaders()).Code; got != http.StatusNotFound {
				t.Fatalf("公开监听 %s %s = %d，应为 404", route.method, route.path, got)
			}
			// 不带凭据也是 404 而不是 401 —— 公开监听上这条路径根本不存在
			if got := serveRoute(t, h, route.method, route.path, nil).Code; got != http.StatusNotFound {
				t.Fatalf("公开监听 %s %s（无凭据）= %d，应为 404", route.method, route.path, got)
			}
		})
	}
}

// TestInternalListenerServesInternalWithSameGate：内网监听上
// 同样的路径走同一条 svc 门 —— 无凭据 401，有凭据则进到 handler（handler
// 自己可能再报 400/404，但不再是鉴权层的 401）。
func TestInternalListenerServesInternalWithSameGate(t *testing.T) {
	h := routeTestServer().InternalRoutes()
	for _, route := range []struct{ method, path string }{
		{"POST", "/internal/file-grants"},
		{"GET", "/internal/actors"},
		{"POST", "/internal/usage"},
		{"GET", "/internal/federation/identity"},
		{"POST", "/internal/federation/node-credentials"},
		{"GET", "/internal/federation/peer-keys/some-node"},
	} {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			if got := serveRoute(t, h, route.method, route.path, nil).Code; got != http.StatusUnauthorized {
				t.Fatalf("内网监听 %s %s（无凭据）= %d，应为 401", route.method, route.path, got)
			}
			if got := serveRoute(t, h, route.method, route.path, svcHeaders()).Code; got == http.StatusUnauthorized {
				t.Fatalf("内网监听 %s %s（有凭据）仍然 401（%d）—— svc 门没认服务凭据", route.method, route.path, got)
			}
		})
	}
}

// TestPublicListenerUnknownV1PlaneIs404：兜底 /v1/ 不再
// 代给网关。未知平面直接 404（无鉴权门 —— 连路由都不存在，就不该再问 key；
// 已知平面无 key 时 401 由 apiKeyGate 的单测覆盖）。
func TestPublicListenerUnknownV1PlaneIs404(t *testing.T) {
	h := routeTestServer().Routes()
	for _, path := range []string{"/v1/nope", "/v1/chat", "/v1/models/extra"} {
		if got := serveRoute(t, h, "POST", path, nil).Code; got != http.StatusNotFound {
			t.Fatalf("POST %s（未知平面）= %d，应为 404", path, got)
		}
	}
}
