package api

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// 以下两条需要真 PG（discoveryPGFixture）：handler 级，
// 验 org 强制 + 负数 400 + 未知组织 404。
// 无 CONTROL_TEST_DATABASE_URL 时跳过（与其它 pg 测试同规则）。

func usageRequest(t *testing.T, f *discoveryFixture, body map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest("POST", "/internal/usage", strings.NewReader(mustJSON(t, body)))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Authorization", "Bearer "+f.server.cfg.ServiceToken)
	w := httptest.NewRecorder()
	f.server.InternalRoutes().ServeHTTP(w, r)
	return w
}

func usageBody(org, event string, pages, requests int) map[string]any {
	return map[string]any{
		"event_id": event, "type": "UsageRecorded", "organization_id": org,
		"payload": map[string]any{
			"actor_id": "u1", "api_key_id": "", "parse_job_id": "j1",
			"kind": "parse", "pages": pages, "requests": requests,
		},
	}
}

func TestInternalUsageRejectsNegativePages(t *testing.T) {
	f := discoveryPGFixture(t)
	w := usageRequest(t, f, usageBody(f.org, "evt-neg-1", -3, 1))
	if w.Code != 400 || !strings.Contains(w.Body.String(), "negative_usage") {
		t.Fatalf("负 pages 应 400 negative_usage：%d %s", w.Code, w.Body.String())
	}
	w = usageRequest(t, f, usageBody(f.org, "evt-neg-2", 1, -2))
	if w.Code != 400 || !strings.Contains(w.Body.String(), "negative_usage") {
		t.Fatalf("负 requests 应 400 negative_usage：%d %s", w.Code, w.Body.String())
	}
}

func TestInternalUsageUnknownOrg404(t *testing.T) {
	f := discoveryPGFixture(t)
	w := usageRequest(t, f, usageBody("org-does-not-exist", "evt-404-1", 1, 1))
	if w.Code != 404 || !strings.Contains(w.Body.String(), "unknown_org") {
		t.Fatalf("未知组织应 404 unknown_org：%d %s", w.Code, w.Body.String())
	}
}

func TestInternalUsageKnownOrgAccepted(t *testing.T) {
	f := discoveryPGFixture(t)
	w := usageRequest(t, f, usageBody(f.org, "evt-ok-1", 2, 1))
	if w.Code != 200 {
		t.Fatalf("已知组织应 200：%d %s", w.Code, w.Body.String())
	}
	// 同 event_id 重投幂等 200（RecordUsage ON CONFLICT DO NOTHING）。
	w = usageRequest(t, f, usageBody(f.org, "evt-ok-1", 2, 1))
	if w.Code != 200 {
		t.Fatalf("重投应 200：%d %s", w.Code, w.Body.String())
	}
}

func TestInternalActorsOrgParam(t *testing.T) {
	f := discoveryPGFixture(t)
	// ?org= 缺省回落 defaultOrg：不带参也 200。
	r := httptest.NewRequest("GET", "/internal/actors?ids="+f.alice.ID, nil)
	r.Header.Set("Authorization", "Bearer "+f.server.cfg.ServiceToken)
	w := httptest.NewRecorder()
	f.server.InternalRoutes().ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatalf("actors 应 200：%d %s", w.Code, w.Body.String())
	}
	// 显式 ?org= 同组织能解出名字。
	r = httptest.NewRequest("GET", "/internal/actors?org="+f.org+"&ids="+f.alice.ID, nil)
	r.Header.Set("Authorization", "Bearer "+f.server.cfg.ServiceToken)
	w = httptest.NewRecorder()
	f.server.InternalRoutes().ServeHTTP(w, r)
	if w.Code != 200 || !strings.Contains(w.Body.String(), f.alice.Username) {
		t.Fatalf("同组织应解出名字：%d %s", w.Code, w.Body.String())
	}
	// 跨组织 ?org= 解不出（组织隔离，handler 只透传 org）。
	r = httptest.NewRequest("GET", "/internal/actors?org=other-org&ids="+f.alice.ID, nil)
	r.Header.Set("Authorization", "Bearer "+f.server.cfg.ServiceToken)
	w = httptest.NewRecorder()
	f.server.InternalRoutes().ServeHTTP(w, r)
	if w.Code != 200 || strings.Contains(w.Body.String(), f.alice.Username) {
		t.Fatalf("跨组织不应泄露名字：%d %s", w.Code, w.Body.String())
	}
}
