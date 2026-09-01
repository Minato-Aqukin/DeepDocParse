package httpx_test

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

// TestClientCannotForgeIdentity 是这个服务最重要的一条守卫。
//
// 少了 StripInboundIdentity，任何人都能发一个 `X-DDP-Role: admin`
// 或 `X-DDP-Organization: <别人的组织>`，而上游会完全正常地接受它 ——
// 没有报错、没有异常日志，就是越权了。
func TestClientCannotForgeIdentity(t *testing.T) {
	var seen http.Header
	h := httpx.StripInboundIdentity(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
	}))

	req := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
	for _, name := range identity.Inbound {
		req.Header.Set(name, "forged")
	}
	h.ServeHTTP(httptest.NewRecorder(), req)

	for _, name := range identity.Inbound {
		if v := seen.Get(name); v != "" {
			t.Fatalf("%s 没被剥掉，客户端可以伪造身份（收到 %q）", name, v)
		}
	}
}

// TestInboundListCoversEveryIdentityHeader 是上一条的反哨兵。
//
// 上面那条只检查 identity.Inbound 里列出的头。**漏列一个等于开了后门**，
// 而漏列不会让任何测试变红 —— 所以这里反过来钉：所有以 X-DDP- 开头的
// 内部头常量都必须出现在 Inbound 里。
func TestInboundListCoversEveryIdentityHeader(t *testing.T) {
	internal := []string{
		identity.HeaderService, identity.HeaderOrganization, identity.HeaderActor,
		identity.HeaderActorKind, identity.HeaderRole, identity.HeaderAPIKeyID,
	}
	in := map[string]bool{}
	for _, h := range identity.Inbound {
		in[h] = true
	}
	for _, h := range internal {
		if !strings.HasPrefix(h, "X-DDP-") {
			continue
		}
		if !in[h] {
			t.Fatalf("内部头 %s 不在 identity.Inbound 里 —— 客户端可以伪造它", h)
		}
	}
	if len(identity.Inbound) < len(internal) {
		t.Fatalf("Inbound 只有 %d 项，少于已知的 %d 个内部头", len(identity.Inbound), len(internal))
	}
}

// TestStatusRecorderForwardsFlush 守 SSE。
//
// StatusRecorder 包住 ResponseWriter 之后如果不透传 Flush，
// **SSE 会整段缓冲住**：客户端要等到响应结束才一次性收到全部事件，
// 表现为"问答一直没反应"，而不是任何形式的报错。
func TestStatusRecorderForwardsFlush(t *testing.T) {
	inner := &flushSpy{ResponseWriter: httptest.NewRecorder()}
	rec := &httpx.StatusRecorder{ResponseWriter: inner}

	var flusher http.Flusher = rec // 编译期就钉住：不实现 Flusher 这里就编不过
	flusher.Flush()
	if !inner.flushed {
		t.Fatal("Flush 没有透传到底层 ResponseWriter")
	}
}

type flushSpy struct {
	http.ResponseWriter
	flushed bool
}

func (f *flushSpy) Flush() { f.flushed = true }

// TestDecodeJSONRejectsUnknownFields：多写一个字段却被静默忽略，
// 是配置类 bug 最常见的来源（"我明明设了 quota 啊"）。
func TestDecodeJSONRejectsUnknownFields(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"name":"a","quotaPages":5}`))
	var dst struct {
		Name string `json:"name"`
	}
	if err := httpx.DecodeJSON(req, &dst); err == nil {
		t.Fatal("未知字段被静默忽略了")
	}
}

// TestDecodeJSONLimitsBodySize：不限长的话一个超大 body 就能把进程打死。
func TestDecodeJSONLimitsBodySize(t *testing.T) {
	huge := `{"name":"` + strings.Repeat("x", 2<<20) + `"}`
	req := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(huge))
	var dst struct {
		Name string `json:"name"`
	}
	if err := httpx.DecodeJSON(req, &dst); err == nil {
		t.Fatal("超长请求体没有被拒绝")
	}
}

func TestCORSDoesNotEchoUnknownOrigin(t *testing.T) {
	h := httpx.CORS([]string{"http://localhost:5173"})(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	req := httptest.NewRequest(http.MethodGet, "/api/org", nil)
	req.Header.Set("Origin", "https://evil.example")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("白名单外的 Origin 被回显了：%q —— 那等于放弃同源保护", got)
	}
}
