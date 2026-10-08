package api

import (
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestClientIPViaIgnoresSpoofedXFF：不可信对端发什么 XFF 都不认，
// 限速看到的永远是 RemoteAddr，轮换 XFF 换不来新桶。
func TestClientIPViaIgnoresSpoofedXFF(t *testing.T) {
	trusted := mustParseProxies(t, "10.0.0.0/8")
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.RemoteAddr = "203.0.113.7:1234"
	r.Header.Set("X-Forwarded-For", "10.9.9.9")
	if got := clientIPVia(r, trusted); got != "203.0.113.7" {
		t.Fatalf("untrusted peer XFF honored: %q", got)
	}
	// 空信任集 = 不信任任何代理，XFF 一律忽略。
	if got := clientIPVia(r, testProxySet{}); got != "203.0.113.7" {
		t.Fatalf("empty trust set honored XFF: %q", got)
	}
}

// TestClientIPViaHonorsTrustedProxy：受信网关后面的 XFF 最左一跳可用。
func TestClientIPViaHonorsTrustedProxy(t *testing.T) {
	trusted := mustParseProxies(t, "10.0.0.0/8")
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.RemoteAddr = "10.1.2.3:4321"
	r.Header.Set("X-Forwarded-For", "198.51.100.9, 10.1.2.3")
	if got := clientIPVia(r, trusted); got != "198.51.100.9" {
		t.Fatalf("trusted proxy XFF ignored: %q", got)
	}
}

type testProxySet struct{ nets []*net.IPNet }

func (s testProxySet) Contains(ip net.IP) bool {
	for _, n := range s.nets {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

func mustParseProxies(t *testing.T, raw string) testProxySet {
	t.Helper()
	var out testProxySet
	for _, part := range strings.Split(raw, ",") {
		p := strings.TrimSpace(part)
		if p == "" {
			continue
		}
		_, n, err := net.ParseCIDR(p)
		if err != nil {
			t.Fatal(err)
		}
		out.nets = append(out.nets, n)
	}
	return out
}

// TestClientIPViaTrustedProxies：limitByIP 经 clientIPVia
// 走 cfg.TrustedProxies：网段内对端认 XFF，网段外不认。
func TestClientIPViaTrustedProxies(t *testing.T) {
	trusted := mustParseProxies(t, "127.0.0.0/8,10.0.0.0/8")
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.RemoteAddr = "10.9.9.9:1234"
	r.Header.Set("X-Forwarded-For", "198.51.100.9")
	if got := clientIPVia(r, trusted); got != "198.51.100.9" {
		t.Fatalf("trusted peer XFF ignored: %q", got)
	}
	r2 := httptest.NewRequest(http.MethodGet, "/", nil)
	r2.RemoteAddr = "203.0.113.7:1234"
	r2.Header.Set("X-Forwarded-For", "198.51.100.9")
	if got := clientIPVia(r2, trusted); got != "203.0.113.7" {
		t.Fatalf("untrusted peer XFF honored: %q", got)
	}
}
