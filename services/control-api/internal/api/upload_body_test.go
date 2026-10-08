package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestDecodeFinalizeBodyChunked：chunked（ContentLength -1）带 body
// 必须解出来；空 body（EOF）合法；半截 JSON 照样 400。
func TestDecodeFinalizeBodyChunked(t *testing.T) {
	payload := `{"engine":"mineru","options":{"a":1}}`
	r := httptest.NewRequest(http.MethodPost, "/api/uploads/u1/finalize", strings.NewReader(payload))
	r.ContentLength = -1 // chunked：没有 Content-Length
	var body finalizeBody
	if err := decodeFinalizeBody(r, &body); err != nil {
		t.Fatalf("chunked body 解失败：%v", err)
	}
	if body.Engine != "mineru" {
		t.Fatalf("engine 丢了：%+v", body)
	}

	// 空 body：NoBody 与空 reader 都是合法空。
	for _, req := range []*http.Request{
		httptest.NewRequest(http.MethodPost, "/x", nil),
		httptest.NewRequest(http.MethodPost, "/x", strings.NewReader("")),
	} {
		var b finalizeBody
		if err := decodeFinalizeBody(req, &b); err != nil {
			t.Fatalf("空 body 应当合法：%v", err)
		}
		if b.Engine != "" {
			t.Fatalf("空 body 解出东西：%+v", b)
		}
	}

	// 半截 JSON 不能吞成空。
	bad := httptest.NewRequest(http.MethodPost, "/x", strings.NewReader(`{"engine":`))
	var b finalizeBody
	if err := decodeFinalizeBody(bad, &b); err == nil {
		t.Fatal("半截 JSON 应当 400")
	} else if !strings.Contains(err.Error(), "invalid_json") && !strings.Contains(err.Error(), "请求体") {
		t.Fatalf("错误码不对：%v", err)
	}

	// io.EOF 直传的判定：DecodeJSON 把 EOF 包成 invalid_json，
	// decodeFinalizeBody 只放行这种，不放行别的 invalid_json。
	_ = io.EOF
}

// TestHandleFinalizeChunkedBodyReachesHandler：helper 测的是解码，
// 这里走真正的 handleFinalizeUpload —— chunked（ContentLength -1）的
// engine/options 必须落到会话行上，而不是被当成空 body 吞掉。
// finalize 之后换 engine 重放必须 409：摘要绑定证明 handler 真读到了 body。
func TestHandleFinalizeChunkedBodyReachesHandler(t *testing.T) {
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	data := []byte("chunked finalize body")
	u := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "chunked-fin", uploadBody(data)), 201)
	if got := putUploadPart(t, u.Parts[0], data); got != 200 {
		t.Fatalf("part status %d", got)
	}
	payload := `{"engine":"mineru","options":{"chunked":true}}`
	r := httptest.NewRequest(http.MethodPost, "/api/uploads/"+u.ID+"/finalize", strings.NewReader(payload))
	r.ContentLength = -1 // chunked：没有 Content-Length
	r.Header.Set("Authorization", "Bearer "+f.aliceToken)
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("Idempotency-Key", "chunked-fin-key")
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	if w.Code != 202 {
		t.Fatalf("chunked finalize: %d %s", w.Code, w.Body.String())
	}
	row, err := f.server.store.UploadSession(t.Context(), f.org, u.ID)
	if err != nil {
		t.Fatal(err)
	}
	if row.Engine == nil || *row.Engine != "mineru" || !strings.Contains(string(row.Options), "chunked") {
		t.Fatalf("chunked body lost in handler: engine=%v options=%s", row.Engine, string(row.Options))
	}
	if w := uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "chunked-fin-key", map[string]any{"engine": "different"}); w.Code != 409 {
		t.Fatalf("finalize digest not bound: %d %s", w.Code, w.Body.String())
	}
}

// TestTmpObjectKeyShapes：temporary_compute 的 key 带 session 段，
// 前缀保持 tmp-remote-compute/<org>/<id>/（corpus 前缀检查兼容）；
// permanent 保持 uploads/ 形状。
func TestTmpObjectKeyShapes(t *testing.T) {
	id := "compute_abc-123"
	random := "sess-random-id"
	got := tmpObjectKey("org1", "temporary_compute", &id, random)
	want := "tmp-remote-compute/org1/compute_abc-123/sess-random-id/source.bin"
	if got != want {
		t.Fatalf("tmp key 形状不对：%q want %q", got, want)
	}
	if !strings.HasPrefix(got, "tmp-remote-compute/org1/compute_abc-123/") {
		t.Fatalf("前缀丢了，corpus 前缀检查会拒：%q", got)
	}
	// 同 compute 不同会话 key 不同：覆盖攻击的前提没了。
	other := tmpObjectKey("org1", "temporary_compute", &id, "other-session")
	if other == got {
		t.Fatal("不同会话 key 相同，覆盖攻击仍可行")
	}
	perm := tmpObjectKey("org1", "permanent", &id, random)
	if perm != "uploads/org1/"+random {
		t.Fatalf("permanent 形状变了：%q", perm)
	}
	// remote_compute_id 为空/目的不对时回落到 permanent 形状（防御性）。
	empty := ""
	if got := tmpObjectKey("org1", "temporary_compute", &empty, random); got != "uploads/org1/"+random {
		t.Fatalf("空 id 应回落：%q", got)
	}
}

// TestValidRemoteComputeID：路径穿越字符一律拒。
func TestValidRemoteComputeID(t *testing.T) {
	for _, bad := range []string{"", "../escape", "a/b", "a\\b", "id with space", strings.Repeat("x", 129), "has.dot", "semi;colon"} {
		if validRemoteComputeID(bad) {
			t.Fatalf("validRemoteComputeID(%q) = true，应当拒绝", bad)
		}
	}
	for _, ok := range []string{"abc", "compute_abc-123", "A0_-x", strings.Repeat("y", 128)} {
		if !validRemoteComputeID(ok) {
			t.Fatalf("validRemoteComputeID(%q) = false，应当放行", ok)
		}
	}
}
