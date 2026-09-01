package proxy_test

import (
	"bufio"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func withActor(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a := &identity.Actor{
			Kind: identity.KindAPIKey, ID: "key1", UserID: "u1",
			OrganizationID: "org1", Role: rbac.Contributor, APIKeyID: "key1",
			RequestID: "req1",
		}
		h.ServeHTTP(w, r.WithContext(identity.With(r.Context(), a)))
	})
}

// TestProxyStreamsWithoutBuffering 走**完整的中间件链**验流式。
//
// ⚠️ **它守的不是 `FlushInterval: -1`。** Go 的 ReverseProxy 对
// `Content-Type: text/event-stream` 与 `ContentLength == -1` 的响应
// 一律立即 flush，与 FlushInterval 无关 —— 把那一行改成 0 这条用例照样绿
// （合仓时做过变异确认）。那一行是给"有 Content-Length 但很慢"的响应兜底的。
//
// 它真正守的是**中间件链里有没有人把响应缓冲住**：`httpx.StatusRecorder`
// 包住 ResponseWriter，少了 Flush 透传就会让 SSE 整段卡住，
// 表现是"问答一直没反应"而不是任何报错。所以这条用例必须经过它。
func TestProxyStreamsWithoutBuffering(t *testing.T) {
	const gap = 700 * time.Millisecond

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "data: first\n\n")
		w.(http.Flusher).Flush()
		// 慢一拍再写第二块。**不阻塞、不等信号** —— 上游卡在 channel 上的话，
		// 断言失败时 httptest.Server.Close() 会一直等在途请求，
		// 整轮测试挂死而不是给出干净的 FAIL
		time.Sleep(gap)
		fmt.Fprint(w, "data: second\n\n")
	}))
	defer upstream.Close()

	up, err := proxy.New("test", upstream.URL, "svc-token")
	if err != nil {
		t.Fatal(err)
	}
	// **经过生产同款的包装层**：这才是这条用例的价值所在
	front := httptest.NewServer(withActor(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := &httpx.StatusRecorder{ResponseWriter: w}
		up.ServeHTTP(rec, r, "")
	})))
	defer front.Close()

	start := time.Now()
	resp, err := http.Get(front.URL + "/v1/chat/completions")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	line, err := bufio.NewReader(resp.Body).ReadString('\n')
	if err != nil {
		t.Fatalf("读第一块失败：%v", err)
	}
	elapsed := time.Since(start)

	if !strings.Contains(line, "first") {
		t.Fatalf("第一块内容不对：%q", line)
	}
	// 第一块必须**远早于**上游写完第二块。缓冲住的话它会等到整个响应结束
	if elapsed >= gap {
		t.Fatalf("第一块等了 %v（上游在 %v 后才写第二块）—— 响应被缓冲了，"+
			"SSE 在这条路径上是坏的", elapsed, gap)
	}
}

// TestProxyReplacesClientAuthorization：客户端的 Authorization 到此为止。
//
// 透传的话上游会把一个用户的 sk- 当成 service token 去比对 —— 结果是
// 上游拒绝所有请求，而错误信息指向"service token 不对"，与真实原因无关。
func TestProxyReplacesClientAuthorization(t *testing.T) {
	var seen http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
	}))
	defer upstream.Close()

	up, _ := proxy.New("test", upstream.URL, "svc-token")
	front := httptest.NewServer(withActor(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up.ServeHTTP(w, r, "")
	})))
	defer front.Close()

	req, _ := http.NewRequest(http.MethodGet, front.URL+"/v1/models", nil)
	req.Header.Set("Authorization", "Bearer sk-user-secret")
	req.Header.Set("Connection", "keep-alive, X-Custom")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if got := seen.Get("Authorization"); got != "Bearer svc-token" {
		t.Fatalf("上游收到的 Authorization = %q，应当是服务凭据", got)
	}
	if seen.Get("Connection") != "" {
		t.Fatal("逐跳头 Connection 被转发给上游了（RFC 7230 §6.1）")
	}
}

// TestProxyForwardsActorContext：上游靠这组头认人，漏一个就等于丢身份。
func TestProxyForwardsActorContext(t *testing.T) {
	var seen http.Header
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
	}))
	defer upstream.Close()

	up, _ := proxy.New("test", upstream.URL, "svc-token")
	front := httptest.NewServer(withActor(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up.ServeHTTP(w, r, "")
	})))
	defer front.Close()

	resp, err := http.Get(front.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	want := map[string]string{
		identity.HeaderService:      "control-api",
		identity.HeaderOrganization: "org1",
		identity.HeaderActor:        "key1",
		identity.HeaderActorKind:    "api_key",
		identity.HeaderRole:         "contributor",
		identity.HeaderAPIKeyID:     "key1",
		identity.HeaderRequestID:    "req1",
	}
	for k, v := range want {
		if got := seen.Get(k); got != v {
			t.Fatalf("上游收到的 %s = %q，应当是 %q", k, got, v)
		}
	}
}

// TestProxyWithoutActorFailsLoudly：鉴权中间件漏挂时必须 500，
// 而不是"以匿名身份正常工作"。
func TestProxyWithoutActorFailsLoudly(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatal("没有 actor 的请求不该到达上游")
	}))
	defer upstream.Close()

	up, _ := proxy.New("test", upstream.URL, "svc-token")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	up.ServeHTTP(rec, req, "")

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("状态码 %d，应当是 500", rec.Code)
	}
}

// TestProxyTrimsPrefix：/mcp 前缀在转发时要去掉。
func TestProxyTrimsPrefix(t *testing.T) {
	var path string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path = r.URL.Path
	}))
	defer upstream.Close()

	up, _ := proxy.New("mcp", upstream.URL, "svc-token")
	front := httptest.NewServer(withActor(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up.ServeHTTP(w, r, "/mcp")
	})))
	defer front.Close()

	resp, _ := http.Get(front.URL + "/mcp/tools")
	if resp != nil {
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
	}
	if path != "/tools" {
		t.Fatalf("上游收到的路径 = %q，应当去掉 /mcp 前缀", path)
	}
}

// TestProxyIgnoresProxyEnv：带 HTTP_PROXY 的机器上，内网调用被塞进代理
// 的表现是**卡住而不是报错**。三个 Python 服务的 httpx 都写着
// trust_env=False，Go 这边靠 Transport.Proxy = nil。
func TestProxyIgnoresProxyEnv(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer upstream.Close()

	up, err := proxy.New("test", upstream.URL, "svc-token")
	if err != nil {
		t.Fatal(err)
	}
	if up.Transport() == nil {
		t.Fatal("Transport 是 nil —— 这条守卫无从检查")
	}
	if up.Transport().Proxy != nil {
		t.Fatal("Transport.Proxy 不是 nil：带代理变量的机器上，" +
			"内网调用会被塞进代理并卡住而不是报错")
	}
}
