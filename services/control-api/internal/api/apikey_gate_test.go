package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// 这个文件补的是 `apiKeyGate` —— 全站唯一同时做 key 校验、撤销/过期、
// 作用域、限速、配额的一段。旧系统（web 的 test_proxy.py / test_ops.py）
// 对这些行为有十来条用例，合仓搬到 Go 时一条都没跟过来。
//
// **这里的每一条失败都不是 500，而是"本不该进来的调用进来了"**：
// 越权、超额、免费用 —— 没有一个会在日志里自己报出来。

type fakeKeyStore struct {
	key     *store.APIKey
	role    rbac.Role
	authErr error

	quotaErr error

	mu        sync.Mutex
	touched   []string
	auditLog  []string
	quotaCall int
}

func (f *fakeKeyStore) AuthenticateAPIKey(context.Context, string) (*store.APIKey, rbac.Role, error) {
	if f.authErr != nil {
		return nil, "", f.authErr
	}
	return f.key, f.role, nil
}

func (f *fakeKeyStore) ReserveQuota(context.Context, string, int) error {
	f.mu.Lock()
	f.quotaCall++
	f.mu.Unlock()
	return f.quotaErr
}

func (f *fakeKeyStore) TouchAPIKey(_ context.Context, id string) {
	f.mu.Lock()
	f.touched = append(f.touched, id)
	f.mu.Unlock()
}

func (f *fakeKeyStore) Audit(_ context.Context, _, _, _, action, target, _ string, _ map[string]any) {
	f.mu.Lock()
	f.auditLog = append(f.auditLog, action+":"+target)
	f.mu.Unlock()
}

func liveKey() *store.APIKey {
	return &store.APIKey{
		ID: "key-1", OrganizationID: "org-1", UserID: "user-1",
		Scopes:          []rbac.Scope{rbac.ScopeRead, rbac.ScopeParse},
		RateLimitPerMin: 60,
	}
}

// run 跑一次网关，返回响应与"下游有没有被调到"。
func run(t *testing.T, keys apiKeyStore, limiter ratelimit.Limiter, scope rbac.Scope,
	authorization string) (*httptest.ResponseRecorder, bool, *identity.Actor) {

	t.Helper()
	if limiter == nil {
		limiter = ratelimit.NewMemory()
	}
	reached := false
	var seen *identity.Actor
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached = true
		seen = identity.From(r.Context())
		w.WriteHeader(http.StatusNoContent)
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/parse", nil)
	if authorization != "" {
		req.Header.Set("Authorization", authorization)
	}
	rec := httptest.NewRecorder()
	apiKeyGate(keys, limiter, scope, next).ServeHTTP(rec, req)
	return rec, reached, seen
}

func errCode(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应体不是 OpenAI 风格的错误：%s", rec.Body.String())
	}
	return body.Error.Code
}

func TestGateRejectsMissingAndMalformedCredentials(t *testing.T) {
	for _, tc := range []struct {
		name, authorization, wantCode string
	}{
		{"没有 Authorization", "", "no_api_key"},
		{"不是 Bearer", "Basic c2s6eA==", "no_api_key"},
		// 拿浏览器 JWT 调 /v1/* 是最常见的误用，必须给一条能自救的错误码，
		// 而不是笼统的 invalid_api_key
		{"拿 JWT 当 key", "Bearer eyJhbGciOiJIUzI1NiJ9.e30.x", "not_an_api_key"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec, reached, _ := run(t, &fakeKeyStore{}, nil, rbac.ScopeRead, tc.authorization)
			if rec.Code != http.StatusUnauthorized {
				t.Errorf("状态码 = %d，应为 401", rec.Code)
			}
			if got := errCode(t, rec); got != tc.wantCode {
				t.Errorf("错误码 = %q，应为 %q", got, tc.wantCode)
			}
			if reached {
				t.Error("下游被调到了 —— 凭证没拦住")
			}
		})
	}
}

func TestGateRejectsUnknownKey(t *testing.T) {
	keys := &fakeKeyStore{authErr: store.ErrNotFound}
	rec, reached, _ := run(t, keys, nil, rbac.ScopeRead, "Bearer sk-nope")
	if rec.Code != http.StatusUnauthorized || errCode(t, rec) != "invalid_api_key" {
		t.Errorf("未知 key 应当 401 invalid_api_key，得到 %d %s", rec.Code, rec.Body.String())
	}
	if reached {
		t.Error("下游被调到了")
	}
}

// 撤销与过期**必须报得出是哪一种**。"API key 无效"这句话对排查毫无帮助，
// 而这两种的处置完全不同（重新签发 vs 续期）。
func TestGateDistinguishesRevokedExpiredAndScopeless(t *testing.T) {
	past := time.Now().Add(-time.Hour)
	for _, tc := range []struct {
		name     string
		mutate   func(*store.APIKey)
		wantCode string
	}{
		{"已撤销", func(k *store.APIKey) { k.RevokedAt = &past }, "api_key_revoked"},
		{"已过期", func(k *store.APIKey) { k.ExpiresAt = &past }, "api_key_expired"},
		{"空作用域", func(k *store.APIKey) { k.Scopes = nil }, "api_key_no_scopes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			key := liveKey()
			tc.mutate(key)
			rec, reached, _ := run(t, &fakeKeyStore{key: key, role: rbac.Admin},
				nil, rbac.ScopeRead, "Bearer sk-live")
			if rec.Code != http.StatusUnauthorized {
				t.Errorf("状态码 = %d，应为 401", rec.Code)
			}
			if got := errCode(t, rec); got != tc.wantCode {
				t.Errorf("错误码 = %q，应为 %q", got, tc.wantCode)
			}
			if reached {
				t.Error("下游被调到了")
			}
		})
	}
}

// 作用域不足是 403 不是 401，而且**必须留审计**：越权尝试是安全事件。
func TestGateDeniesMissingScopeAndAudits(t *testing.T) {
	keys := &fakeKeyStore{key: liveKey(), role: rbac.Admin}
	rec, reached, _ := run(t, keys, nil, rbac.ScopeExtract, "Bearer sk-live")

	if rec.Code != http.StatusForbidden {
		t.Errorf("状态码 = %d，应为 403", rec.Code)
	}
	if got := errCode(t, rec); got != "scope_denied" {
		t.Errorf("错误码 = %q，应为 scope_denied", got)
	}
	if reached {
		t.Error("下游被调到了 —— 越权没拦住")
	}
	if len(keys.auditLog) != 1 || !strings.HasPrefix(keys.auditLog[0], "apikey.scope_denied:") {
		t.Errorf("越权尝试没留审计：%v", keys.auditLog)
	}
}

func TestGatePassesWithScopeAndBuildsActor(t *testing.T) {
	keys := &fakeKeyStore{key: liveKey(), role: rbac.Contributor}
	rec, reached, actor := run(t, keys, nil, rbac.ScopeParse, "Bearer sk-live")

	if !reached || rec.Code != http.StatusNoContent {
		t.Fatalf("有作用域却没放行：%d %s", rec.Code, rec.Body.String())
	}
	if actor == nil {
		t.Fatal("下游拿不到 actor —— 后面每一条查询都会缺组织边界")
	}
	if actor.Kind != identity.KindAPIKey || actor.OrganizationID != "org-1" ||
		actor.APIKeyID != "key-1" || actor.Role != rbac.Contributor {
		t.Errorf("actor 组装错了：%+v", actor)
	}
	if len(keys.touched) != 1 {
		t.Errorf("放行后没更新 last_used_at：%v", keys.touched)
	}
}

func TestGateRateLimitsAndReportsHeaders(t *testing.T) {
	key := liveKey()
	key.RateLimitPerMin = 2
	keys := &fakeKeyStore{key: key, role: rbac.Admin}
	limiter := ratelimit.NewMemory()

	for i := 1; i <= 2; i++ {
		rec, reached, _ := run(t, keys, limiter, rbac.ScopeRead, "Bearer sk-live")
		if !reached {
			t.Fatalf("第 %d 次就被限了，限额是 2", i)
		}
		if got := rec.Header().Get("X-RateLimit-Limit"); got != "2" {
			t.Errorf("X-RateLimit-Limit = %q，应为 \"2\"", got)
		}
		if want := itoa(2 - i); rec.Header().Get("X-RateLimit-Remaining") != want {
			t.Errorf("第 %d 次 Remaining = %q，应为 %q", i,
				rec.Header().Get("X-RateLimit-Remaining"), want)
		}
	}

	rec, reached, _ := run(t, keys, limiter, rbac.ScopeRead, "Bearer sk-live")
	if rec.Code != http.StatusTooManyRequests || errCode(t, rec) != "rate_limited" {
		t.Errorf("超额应当 429 rate_limited，得到 %d %s", rec.Code, rec.Body.String())
	}
	if reached {
		t.Error("超额后下游还是被调到了")
	}
}

// 限速器坏掉时**放行**是刻意取舍（写在 middleware.go 的注释里）：
// 做成硬失败等于让 Redis 抖动变成全站不可用。
// 钉住它是为了让以后改这个决定的人知道自己在改一个决定，而不是修一个 bug。
func TestGateFailsOpenWhenLimiterIsBroken(t *testing.T) {
	keys := &fakeKeyStore{key: liveKey(), role: rbac.Admin}
	rec, reached, _ := run(t, keys, brokenLimiter{}, rbac.ScopeRead, "Bearer sk-live")

	if !reached || rec.Code != http.StatusNoContent {
		t.Fatalf("限速器坏了不该拦请求：%d %s", rec.Code, rec.Body.String())
	}
	// 放行了就不该再报剩余额度 —— 那个数字是假的
	if rec.Header().Get("X-RateLimit-Remaining") != "" {
		t.Error("限速器坏了却还发了 X-RateLimit-Remaining")
	}
}

type brokenLimiter struct{}

func (brokenLimiter) Kind() string { return "broken" }
func (brokenLimiter) Allow(context.Context, string, int, time.Duration) (bool, int, error) {
	return false, 0, errors.New("redis down")
}

// 配额只在**会消耗页数**的平面上查。查询类平面多查一次是纯开销，
// 而漏查计费平面是直接的收入损失。
func TestGateChecksQuotaOnlyOnBillablePlanes(t *testing.T) {
	for _, tc := range []struct {
		scope     rbac.Scope
		wantCalls int
	}{
		{rbac.ScopeParse, 1},
		{rbac.ScopeExtract, 1},
		{rbac.ScopeRead, 0},
	} {
		t.Run(string(tc.scope), func(t *testing.T) {
			key := liveKey()
			key.Scopes = []rbac.Scope{tc.scope}
			keys := &fakeKeyStore{key: key, role: rbac.Admin}
			if _, reached, _ := run(t, keys, nil, tc.scope, "Bearer sk-live"); !reached {
				t.Fatal("应当放行")
			}
			if keys.quotaCall != tc.wantCalls {
				t.Errorf("%s 平面查了 %d 次配额，应为 %d 次",
					tc.scope, keys.quotaCall, tc.wantCalls)
			}
		})
	}
}

// 配额用尽是 402 —— 它与 401/403 是三件不同的事，客户端的处置也不同。
func TestGateReturns402WhenQuotaExhausted(t *testing.T) {
	key := liveKey()
	key.Scopes = []rbac.Scope{rbac.ScopeParse}
	keys := &fakeKeyStore{key: key, role: rbac.Admin, quotaErr: store.ErrQuotaExceeded}

	rec, reached, _ := run(t, keys, nil, rbac.ScopeParse, "Bearer sk-live")
	if rec.Code != http.StatusPaymentRequired {
		t.Errorf("状态码 = %d，应为 402", rec.Code)
	}
	if got := errCode(t, rec); got != "quota_exceeded" {
		t.Errorf("错误码 = %q，应为 quota_exceeded", got)
	}
	if reached {
		t.Error("配额用尽后下游还是被调到了 —— 这是直接的免费用")
	}
}

// 配额查询本身出错（数据库抖动）不能被当成"配额用尽"。
// 402 会让客户端去充值，而真正的问题在我们这边。
func TestGateSurfacesQuotaLookupFailureAsServerError(t *testing.T) {
	key := liveKey()
	key.Scopes = []rbac.Scope{rbac.ScopeParse}
	keys := &fakeKeyStore{key: key, role: rbac.Admin, quotaErr: errors.New("connection refused")}

	rec, reached, _ := run(t, keys, nil, rbac.ScopeParse, "Bearer sk-live")
	if rec.Code == http.StatusPaymentRequired {
		t.Error("数据库出错被报成了配额用尽")
	}
	if rec.Code < 500 {
		t.Errorf("状态码 = %d，应当是 5xx", rec.Code)
	}
	if reached {
		t.Error("下游被调到了")
	}
}
