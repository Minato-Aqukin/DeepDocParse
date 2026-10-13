package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// 这个文件钉的是游客模式的入口门（guest.go + 路由装配）。
//
// 门的行为只有四句：有凭据走会话语义；无凭据 + 开关开 + GET/HEAD +
// 路径在白名单里才放游客；其余无凭据一律 401；游客按 IP 限速。
// 下面每一条测的都是其中一句 —— 漏掉任何一句的表现都是
// "本不该进来的调用进来了"，不会在日志里自己报出来。

// guestTestServer 装一条带 corpus 替身的路由表：门 + 转发的装配
// 一起测 —— 只测 sessionOrGuest 的话，"路由上挂错了门"测不出来。
func guestTestServer(t *testing.T, cfg *config.Config, corpus http.Handler) *Server {
	t.Helper()
	target := httptest.NewServer(corpus)
	t.Cleanup(target.Close)
	upstream, err := proxy.New("corpus-api", target.URL, "internal-secret-never-forward")
	if err != nil {
		t.Fatal(err)
	}
	cfg.CorpusURL = target.URL
	cfg.ServiceToken = "internal-secret-never-forward"
	if cfg.GuestRatePerMin == 0 {
		cfg.GuestRatePerMin = 300
	}
	if cfg.GuestSearchRatePerMin == 0 {
		cfg.GuestSearchRatePerMin = 30
	}
	return &Server{cfg: cfg, corpus: upstream, defaultOrg: "org-default",
		limiter: ratelimit.NewMemory()}
}

func guestCfg() *config.Config {
	// 与 Load() 的缺省一致：限速 0 在 limiter 语义里是"坏限额，默认拒绝"，
	// 不是"不限速" —— 缺省必须显式写出来。
	return &config.Config{GuestMode: "read", GuestRatePerMin: 300, GuestSearchRatePerMin: 30}
}

// TestGuestPathOKSegmentBoundaries：白名单匹配认路径段边界 ——
// `/api/wikis` 放行它自己与 `/api/wikis/…`，但 `/api/wikis-evil` 这种
// "长得像"的必须拦住。strings.HasPrefix 不认段边界，所以这里单测钉住。
func TestGuestPathOKSegmentBoundaries(t *testing.T) {
	allowed := []string{
		"/api/resources", "/api/resources/r1",
		"/api/v1/resources", "/api/v1/resources/r1",
		"/api/v1/collections/c1",
		"/api/documents", "/api/documents/d1",
		"/api/documents/d1/download-url",
		"/api/search",
		"/api/evidence/e1", "/api/evidence/e1/backlinks",
		"/api/wikis", "/api/wikis/w1",
		"/api/wikis/w1/revisions/rev1",
	}
	for _, path := range allowed {
		if !guestPathOK(path) {
			t.Errorf("guestPathOK(%q) = false，应为 true", path)
		}
	}
	denied := []string{
		"/api/documentsX", "/api/wikis-evil", "/api/searchX",
		"/api/resources2", "/api/evidence2/e1",
		"/api/conversations", "/api/wiki", "/api/keys",
		"/api/auth/me", "/api/v1/tasks", "/api/",
	}
	for _, path := range denied {
		if guestPathOK(path) {
			t.Errorf("guestPathOK(%q) = true，应为 false", path)
		}
	}
}

// TestGuestOffKeepsAnonymousOut：开关没开时，无凭据 GET 白名单路径
// 仍然是 401 —— 开关的缺省必须是"关"，不能靠"没配就放行"。
func TestGuestOffKeepsAnonymousOut(t *testing.T) {
	var calls atomic.Int32
	s := guestTestServer(t, &config.Config{GuestMode: "off"},
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			w.WriteHeader(http.StatusOK)
		}))
	h := s.Routes()
	for _, path := range []string{
		"/api/documents", "/api/documents/d1", "/api/search",
		"/api/resources", "/api/v1/resources", "/api/v1/collections/c1",
		"/api/evidence/e1", "/api/wikis", "/api/wikis/w1",
		"/api/documents/d1/download-url",
	} {
		r := httptest.NewRequest(http.MethodGet, path, nil)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != http.StatusUnauthorized {
			t.Errorf("GET %s（guest off）= %d，应为 401", path, w.Code)
		}
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("guest off 时 corpus 被调了 %d 次 —— 门没关住", got)
	}
}

// TestGuestAllowlistForwardsWithGuestActor：开关开时，无凭据 GET 白名单
// 路径转发给 corpus，actor 是 kind=guest / 组织=默认组织 / 角色=viewer，
// 且不带 X-DDP-User。
func TestGuestAllowlistForwardsWithGuestActor(t *testing.T) {
	type received struct {
		kind, actor, user, org, role string
		hasUser                      bool
	}
	calls := make(chan received, 64)
	s := guestTestServer(t, guestCfg(),
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, hasUser := r.Header[http.CanonicalHeaderKey(identity.HeaderUser)]
			user := r.Header.Get(identity.HeaderUser)
			calls <- received{
				kind:    r.Header.Get(identity.HeaderActorKind),
				actor:   r.Header.Get(identity.HeaderActor),
				user:    user,
				hasUser: hasUser && user != "",
				org:     r.Header.Get(identity.HeaderOrganization),
				role:    r.Header.Get(identity.HeaderRole),
			}
			w.WriteHeader(http.StatusOK)
		}))
	h := s.Routes()
	allowed := []string{
		"/api/documents", "/api/documents/d1",
		"/api/documents/d1/result", "/api/documents/d1/pages",
		"/api/search",
		"/api/resources", "/api/resources/r1",
		"/api/v1/resources", "/api/v1/resources/r1",
		"/api/v1/collections/c1",
		"/api/evidence/e1", "/api/evidence/e1/backlinks",
		"/api/wikis", "/api/wikis/w1",
		"/api/wikis/w1/revisions/rev1",
	}
	for _, path := range allowed {
		r := httptest.NewRequest(http.MethodGet, path, nil)
		// download-url 由 control-api 自己实现，不进 corpus 替身：
		// 它的游客分支由 TestGuestDownloadURLPresignsFromFileAccess 覆盖。
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != http.StatusOK {
			t.Errorf("GET %s（guest on）= %d，应为 200（转发成功）", path, w.Code)
		}
	}
	// 用 len 判而不是阻塞读：某条白名单路径不再转发时，这里要当场红，而不是卡到 go test 超时
	if got := len(calls); got != len(allowed) {
		t.Fatalf("corpus 收到 %d 次转发，应为 %d 次（每条白名单路径一次）", got, len(allowed))
	}
	for range allowed {
		got := <-calls
		if got.kind != string(identity.KindGuest) {
			t.Errorf("X-DDP-Actor-Kind = %q，应为 guest", got.kind)
		}
		if got.actor != "guest" {
			t.Errorf("X-DDP-Actor = %q，应为 guest", got.actor)
		}
		if got.hasUser {
			t.Errorf("游客带了 X-DDP-User: %q —— 游客没有 user id", got.user)
		}
		if got.org != "org-default" {
			t.Errorf("X-DDP-Organization = %q，应为默认组织", got.org)
		}
		if got.role != string(rbac.Viewer) {
			t.Errorf("X-DDP-Role = %q，应为 viewer", got.role)
		}
	}
}

// TestGuestAllowlistRejectsEverythingElse：白名单之外的路径、
// 非 GET/HEAD 方法，无凭据一律 401 —— 游客只读，且只读那几条。
//
// **变异确认**：删掉 guestPrefixes 里的一行（或把 guestPathOK 改成恒真），
// 这条必须红 —— 它是"门真正在拦"的证据。
func TestGuestAllowlistRejectsEverythingElse(t *testing.T) {
	var calls atomic.Int32
	s := guestTestServer(t, guestCfg(),
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls.Add(1)
			w.WriteHeader(http.StatusOK)
		}))
	h := s.Routes()
	denied := []struct{ method, path string }{
		// 写操作：一个都不许
		{http.MethodPost, "/api/conversations"},
		{http.MethodPost, "/api/documents/d1/conversations"},
		{http.MethodPost, "/api/extractions/runs"},
		{http.MethodPost, "/api/knowledge/build"},
		{http.MethodPost, "/api/wikis"},
		{http.MethodDelete, "/api/documents/d1"},
		{http.MethodPatch, "/api/wikis/w1/pages/p1"},
		// 读操作但不在白名单里
		{http.MethodGet, "/api/keys"},
		{http.MethodGet, "/api/conversations"},
		{http.MethodGet, "/api/conversations/c1/messages"},
		{http.MethodGet, "/api/wiki"},
		{http.MethodGet, "/api/wiki/something"},
		{http.MethodGet, "/api/extractions/runs/r1"},
		{http.MethodGet, "/api/knowledge/builds/b1"},
		{http.MethodGet, "/api/reviews"},
		{http.MethodGet, "/api/bundles"},
		{http.MethodGet, "/api/uploads/u1"},
		{http.MethodGet, "/api/usage"},
		{http.MethodGet, "/api/audit"},
		{http.MethodGet, "/api/auth/me"},
		{http.MethodGet, "/api/org"},
		{http.MethodGet, "/api/org/members"},
		{http.MethodGet, "/api/v1/tasks"},
		{http.MethodGet, "/api/v1/task-plans/p1"},
		{http.MethodGet, "/api/v1/deliveries"},
		{http.MethodGet, "/api/v1/remote-compute"},
		// 注意："长得像白名单"的路径（/api/documentsX 之类）在 mux 层就 404，
		// 根本到不了门 —— 段边界由 TestGuestPathOKSegmentBoundaries 单测钉住。
	}
	for _, route := range denied {
		t.Run(route.method+" "+route.path, func(t *testing.T) {
			r := httptest.NewRequest(route.method, route.path, nil)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != http.StatusUnauthorized {
				t.Fatalf("%s %s（guest on）= %d，应为 401", route.method, route.path, w.Code)
			}
		})
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("白名单外的请求进了 corpus %d 次 —— 门没关住", got)
	}
}

// TestGuestNeverDowngradesBadCredentials：带了凭据但凭据无效时是 401，
// 绝不静默降级成游客 —— 否则过期会话的用户会在毫无提示的情况下
// 看到公开视图，还以为自己的东西丢了。
//
// 验签失败发生在 membership 回查之前，所以 store 保持 nil 也能测：
// 能进到这里说明门把无效凭据拦在了会话语义那条路上。
func TestGuestNeverDowngradesBadCredentials(t *testing.T) {
	var calls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(target.Close)
	upstream, err := proxy.New("corpus-api", target.URL, "internal-secret-never-forward")
	if err != nil {
		t.Fatal(err)
	}
	cfg := guestCfg()
	cfg.CorpusURL = target.URL
	cfg.ServiceToken = "internal-secret-never-forward"
	s := &Server{cfg: cfg, corpus: upstream, defaultOrg: "org-default",
		sessions: auth.NewSessions(strings.Repeat("s", 32), time.Hour),
		limiter:  ratelimit.NewMemory()}
	h := s.Routes()
	for _, tc := range []struct {
		name  string
		setup func(r *http.Request)
	}{
		{"坏 Bearer", func(r *http.Request) {
			r.Header.Set("Authorization", "Bearer this.is.not.a.valid.session")
		}},
		{"坏 cookie", func(r *http.Request) {
			r.AddCookie(&http.Cookie{Name: "ddp_session", Value: "garbage"})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
			tc.setup(r)
			w := httptest.NewRecorder()
			h.ServeHTTP(w, r)
			if w.Code != http.StatusUnauthorized {
				t.Fatalf("无效凭据 = %d，应为 401（绝不降级成游客）", w.Code)
			}
			if !strings.Contains(w.Body.String(), "invalid_session") {
				t.Fatalf("错误码丢了 invalid_session：%s", w.Body.String())
			}
		})
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("无效凭据进了 corpus %d 次 —— 降级成游客了", got)
	}
}

// TestGuestStatusReflectsConfig：/api/auth/guest 无需鉴权，
// enabled 当且仅当 GUEST_MODE=read。
func TestGuestStatusReflectsConfig(t *testing.T) {
	for _, tc := range []struct {
		mode string
		want bool
	}{
		{"off", false},
		{"read", true},
	} {
		s := &Server{cfg: &config.Config{GuestMode: tc.mode}}
		r := httptest.NewRequest(http.MethodGet, "/api/auth/guest", nil)
		w := httptest.NewRecorder()
		s.Routes().ServeHTTP(w, r)
		if w.Code != http.StatusOK {
			t.Fatalf("mode=%s：状态码 = %d，应为 200", tc.mode, w.Code)
		}
		var body struct {
			Enabled bool `json:"enabled"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
			t.Fatalf("mode=%s：响应体不是 JSON：%s", tc.mode, w.Body.String())
		}
		if body.Enabled != tc.want {
			t.Fatalf("mode=%s：enabled = %v，应为 %v", tc.mode, body.Enabled, tc.want)
		}
	}
}

// TestGuestRateLimitIsPerIP：游客限速按客户端 IP 分桶 ——
// 一个 IP 刷满只限它自己，换个 IP 照样能进；超限是 429 rate_limited。
func TestGuestRateLimitIsPerIP(t *testing.T) {
	cfg := guestCfg()
	cfg.GuestRatePerMin = 2
	s := guestTestServer(t, cfg,
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))
	h := s.Routes()
	get := func(ip string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
		r.RemoteAddr = ip + ":1234"
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if w := get("198.51.100.1"); w.Code != http.StatusOK {
		t.Fatalf("第 1 次 = %d，应为 200", w.Code)
	}
	if w := get("198.51.100.1"); w.Code != http.StatusOK {
		t.Fatalf("第 2 次 = %d，应为 200", w.Code)
	}
	limited := get("198.51.100.1")
	if limited.Code != http.StatusTooManyRequests {
		t.Fatalf("第 3 次 = %d，应为 429", limited.Code)
	}
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(limited.Body.Bytes(), &body); err != nil {
		t.Fatalf("429 响应体不是 OpenAI 风格：%s", limited.Body.String())
	}
	if body.Error.Code != "rate_limited" {
		t.Fatalf("错误码 = %q，应为 rate_limited", body.Error.Code)
	}
	// 另一个 IP 不受影响 —— 桶是按 IP 分的
	if w := get("198.51.100.2"); w.Code != http.StatusOK {
		t.Fatalf("另一个 IP = %d，应为 200（桶是按 IP 分的）", w.Code)
	}
}

// TestGuestSearchRateLimitIsSeparate：检索有自己更紧的桶 ——
// 通用配额还没用完时，检索先被限住；且它同样按 IP 分。
func TestGuestSearchRateLimitIsSeparate(t *testing.T) {
	cfg := guestCfg()
	cfg.GuestRatePerMin = 100
	cfg.GuestSearchRatePerMin = 1
	s := guestTestServer(t, cfg,
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))
	h := s.Routes()
	search := func(ip string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodGet, "/api/search?q=x", nil)
		r.RemoteAddr = ip + ":1234"
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w
	}
	if w := search("198.51.100.7"); w.Code != http.StatusOK {
		t.Fatalf("第 1 次检索 = %d，应为 200", w.Code)
	}
	if w := search("198.51.100.7"); w.Code != http.StatusTooManyRequests {
		t.Fatalf("第 2 次检索 = %d，应为 429（检索桶更紧）", w.Code)
	}
	// 非检索路径不受检索桶影响
	r := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
	r.RemoteAddr = "198.51.100.7:1234"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("同 IP 读文档 = %d，应为 200（检索桶只管检索）", w.Code)
	}
}

// TestGuestCannotForgeIdentity：游客请求里夹带的 X-DDP-* 头必须被剥掉 ——
// StripInboundIdentity 排在一切鉴权之前，游客路径也不例外。
func TestGuestCannotForgeIdentity(t *testing.T) {
	seen := make(chan http.Header, 1)
	s := guestTestServer(t, guestCfg(),
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			seen <- r.Header.Clone()
			w.WriteHeader(http.StatusOK)
		}))
	r := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
	r.Header.Set(identity.HeaderRole, "admin")
	r.Header.Set(identity.HeaderUser, "someone-else")
	r.Header.Set(identity.HeaderActorKind, "user")
	w := httptest.NewRecorder()
	s.Routes().ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("GET /api/documents（guest on）= %d，应为 200", w.Code)
	}
	got := <-seen
	if role := got.Get(identity.HeaderRole); role != string(rbac.Viewer) {
		t.Fatalf("X-DDP-Role = %q，伪造的 admin 没被盖掉", role)
	}
	if user := got.Get(identity.HeaderUser); user != "" {
		t.Fatalf("X-DDP-User = %q，伪造的头没被剥掉", user)
	}
	if kind := got.Get(identity.HeaderActorKind); kind != string(identity.KindGuest) {
		t.Fatalf("X-DDP-Actor-Kind = %q，伪造的 user 没被盖掉", kind)
	}
}

// TestSessionOrGuestPreservesCredentialedSemantics：带凭据的请求走的
// 还是 requireSession 那条路 —— API key 走错门的那句错必须原样保留，
// 不能变成游客放行或笼统的 401。
func TestSessionOrGuestPreservesCredentialedSemantics(t *testing.T) {
	s := guestTestServer(t, guestCfg(),
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
		}))
	r := httptest.NewRequest(http.MethodGet, "/api/documents", nil)
	r.Header.Set("Authorization", "Bearer sk-testkey1234567890")
	w := httptest.NewRecorder()
	s.Routes().ServeHTTP(w, r)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("API key 调 /api/* = %d，应为 401", w.Code)
	}
	if !strings.Contains(w.Body.String(), "api_key_on_session_route") {
		t.Fatalf("错误码丢了 api_key_on_session_route：%s", w.Body.String())
	}
}

// TestGuestDownloadURLPresignsFromFileAccess：游客调 download-url 时，
// 授权走同一条 corpus /internal/file-access（带着 guest actor），
// 签名直接用已授权的 file-access —— 不经过 StableGrantFor 落库。
//
// store 故意保持 nil：游客分支碰一下数据库这里就 panic。
// file-access 替身同时断言它收到的是 guest 头，不是伪造/缺失的 actor。
func TestGuestDownloadURLPresignsFromFileAccess(t *testing.T) {
	var accessCalls atomic.Int32
	seenKind := make(chan string, 1)
	seenUser := make(chan string, 1)
	fileAccess := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		accessCalls.Add(1)
		seenKind <- r.Header.Get(identity.HeaderActorKind)
		seenUser <- r.Header.Get(identity.HeaderUser)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"document_id":"d1","resource_id":"r1",` +
			`"object_key":"org/d1.pdf","filename":"d1.pdf","mime":"application/pdf"}`))
	}))
	t.Cleanup(fileAccess.Close)

	// 签名器：Open() 会探一次桶，拿一个永远 200 的假 endpoint 顶住
	//（与 objectstore 包内单测同一个做法）；签名本身是纯本地计算。
	probe := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(probe.Close)
	objects, err := objectstore.Open(context.Background(), objectstore.Config{
		Endpoint:  strings.TrimPrefix(probe.URL, "http://"),
		AccessKey: "ak", SecretKey: "sk",
		Bucket: "deepdocparse", Region: "us-east-1", PresignTTL: 15 * time.Minute,
	})
	if err != nil {
		t.Fatal(err)
	}
	upstream, err := proxy.New("corpus-api", fileAccess.URL, "internal-secret")
	if err != nil {
		t.Fatal(err)
	}
	cfg := guestCfg()
	cfg.CorpusURL = fileAccess.URL
	cfg.ServiceToken = "internal-secret"
	cfg.AllowedMIME = []string{"application/pdf"}
	s := &Server{cfg: cfg, corpus: upstream, objects: objects,
		defaultOrg: "org-default", limiter: ratelimit.NewMemory()}

	r := httptest.NewRequest(http.MethodGet, "/api/documents/d1/download-url", nil)
	w := httptest.NewRecorder()
	s.Routes().ServeHTTP(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("游客 download-url = %d，应为 200：%s", w.Code, w.Body.String())
	}
	var body struct {
		URL           string `json:"url"`
		SupportsRange bool   `json:"supports_range"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应体不是 JSON：%s", w.Body.String())
	}
	if body.URL == "" || !body.SupportsRange {
		t.Fatalf("签名响应缺字段：%s", w.Body.String())
	}
	if got := accessCalls.Load(); got != 1 {
		t.Fatalf("file-access 被调了 %d 次，应为 1（授权必须实时查）", got)
	}
	if kind := <-seenKind; kind != string(identity.KindGuest) {
		t.Fatalf("file-access 收到的 kind = %q，应为 guest", kind)
	}
	if user := <-seenUser; user != "" {
		t.Fatalf("file-access 收到的 X-DDP-User = %q，应为空", user)
	}

	// 开关没开时同一条路径回到 401 —— download-url 也挂在同一道门上
	cfgOff := guestCfg()
	cfgOff.GuestMode = "off"
	cfgOff.CorpusURL = fileAccess.URL
	cfgOff.ServiceToken = "internal-secret"
	cfgOff.AllowedMIME = []string{"application/pdf"}
	sOff := &Server{cfg: cfgOff, corpus: upstream, objects: objects,
		defaultOrg: "org-default", limiter: ratelimit.NewMemory()}
	wOff := httptest.NewRecorder()
	sOff.Routes().ServeHTTP(wOff, httptest.NewRequest(http.MethodGet, "/api/documents/d1/download-url", nil))
	if wOff.Code != http.StatusUnauthorized {
		t.Fatalf("guest off 时 download-url = %d，应为 401", wOff.Code)
	}
}
