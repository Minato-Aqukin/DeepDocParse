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

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

// 追加版本上传：准入在分配存储之前，目标在会话上冻结，登记结果如实回读。
// 需要真 PG + 真 S3（与 upload_pg_s3_test.go 同一套环境变量）；corpus 用假端点，
// 因为这里验的是 control 这一侧的判定与持久化，不是 corpus 的谓词本身
// （后者在 corpus-api 的 test_upload_versions.py 里对真实实现验）。

type targetCorpus struct {
	admission atomic.Int32
	mode      atomic.Value // "ok" | "missing" | "exists" | "foreign"
	events    atomic.Value // func() (int, string)
}

func uploadTargetFixture(t *testing.T) (*discoveryFixture, *targetCorpus) {
	t.Helper()
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	c := &targetCorpus{}
	c.mode.Store("ok")
	c.events.Store(func() (int, string) { return 200, `{"ok":true}` })
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Header.Get("Authorization") != "Bearer "+f.server.cfg.ServiceToken {
			w.WriteHeader(401)
			return
		}
		if r.URL.Path == "/internal/events" {
			status, body := c.events.Load().(func() (int, string))()
			w.WriteHeader(status)
			_, _ = w.Write([]byte(body))
			return
		}
		if !strings.HasPrefix(r.URL.Path, "/internal/upload-target/") {
			w.WriteHeader(404)
			return
		}
		c.admission.Add(1)
		// 准入按上传者本人判：身份头必须是 alice，而不是服务身份
		if r.Header.Get(identity.HeaderActor) != f.alice.ID || r.Header.Get(identity.HeaderActorKind) != "user" {
			w.WriteHeader(403)
			_, _ = w.Write([]byte(`{"error":{"code":"permission_denied"}}`))
			return
		}
		id := strings.TrimPrefix(r.URL.Path, "/internal/upload-target/")
		switch c.mode.Load().(string) {
		case "missing":
			w.WriteHeader(404)
			_, _ = w.Write([]byte(`{"error":{"message":"x","type":"invalid_request_error","code":"resource_not_found"}}`))
		case "exists":
			w.WriteHeader(409)
			_, _ = w.Write([]byte(`{"error":{"message":"x","type":"invalid_request_error","code":"resource_version_exists"}}`))
		case "foreign":
			_, _ = w.Write([]byte(`{"resource_id":"someone-else"}`))
		default:
			if r.URL.Query().Get("sha256") == "" {
				w.WriteHeader(400)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]string{"resource_id": id})
		}
	}))
	t.Cleanup(server.Close)
	f.server.cfg.CorpusURL = server.URL
	f.server.corpus, _ = proxy.New("corpus", server.URL, f.server.cfg.ServiceToken)
	return f, c
}

func orgUploadCount(t *testing.T, f *discoveryFixture) int {
	t.Helper()
	var n int
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT count(*) FROM control.upload_sessions WHERE organization_id=$1`, f.org).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

type targetWire struct {
	uploadWire
	TargetResourceID *string `json:"target_resource_id"`
	IngestStatus     *string `json:"ingest_status"`
	IngestError      *string `json:"ingest_error"`
}

func TestUploadTargetAdmissionPrecedesStorageAndKeyReplaySkipsIt(t *testing.T) {
	f, corpus := uploadTargetFixture(t)
	body := uploadBody([]byte("controller manual v2"))
	body["target_resource_id"] = "res-controller"

	for mode, want := range map[string]struct {
		status int
		code   string
	}{"missing": {404, "resource_not_found"}, "exists": {409, "resource_version_exists"}, "foreign": {502, "upload_target_unavailable"}} {
		corpus.mode.Store(mode)
		w := uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "denied-"+mode, body)
		var out struct {
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
		}
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		if w.Code != want.status || out.Error.Code != want.code {
			t.Fatalf("%s: %d %s", mode, w.Code, w.Body.String())
		}
	}
	if n := orgUploadCount(t, f); n != 0 {
		t.Fatalf("denied targets still allocated %d sessions", n)
	}

	corpus.mode.Store("ok")
	created := decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "append-v2", body), 201)
	if created.TargetResourceID == nil || *created.TargetResourceID != "res-controller" || created.IngestStatus != nil {
		t.Fatalf("target not frozen / ingest fabricated: %+v", created)
	}
	calls := corpus.admission.Load()

	// 原上传登记成功后目标里已经有这份内容；同键重试必须取回原会话，而不是被准入判成冲突。
	corpus.mode.Store("exists")
	again := decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "append-v2", body), 200)
	if again.ID != created.ID || corpus.admission.Load() != calls {
		t.Fatalf("replay re-admitted or diverged: %+v calls=%d->%d", again, calls, corpus.admission.Load())
	}
	moved := uploadBody([]byte("controller manual v2"))
	moved["target_resource_id"] = "res-other"
	if w := uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "append-v2", moved); w.Code != 409 || !strings.Contains(w.Body.String(), "idempotency_conflict") {
		t.Fatalf("same key moved target: %d %s", w.Code, w.Body.String())
	}

	tempNoID := uploadBody([]byte("scratch"))
	tempNoID["target_resource_id"] = "res-controller"
	tempNoID["purpose"] = "temporary_compute"
	tempWithTarget := uploadBody([]byte("scratch"))
	tempWithTarget["target_resource_id"] = "res-controller"
	tempWithTarget["purpose"] = "temporary_compute"
	tempWithTarget["remote_compute_id"] = "compute-valid-1"
	escaped := uploadBody([]byte("bad id"))
	escaped["target_resource_id"] = "../escape"
	for _, bad := range []struct {
		body map[string]any
		code string
	}{
		{tempNoID, "bad_remote_compute_id"},
		{tempWithTarget, "invalid_upload_target"},
		{escaped, "invalid_upload_target"},
	} {
		if w := uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "", bad.body); w.Code != 400 || !strings.Contains(w.Body.String(), bad.code) {
			t.Fatalf("bad target accepted: %d %s want %s", w.Code, w.Body.String(), bad.code)
		}
	}
	if n := orgUploadCount(t, f); n != 1 {
		t.Fatalf("expected exactly the one admitted session, got %d", n)
	}
}

func TestUploadTargetFrozenIntoEventAndIngestStatesReadBack(t *testing.T) {
	f, corpus := uploadTargetFixture(t)
	data := []byte("%PDF-1.4 controller 23 ms")
	body := uploadBody(data)
	body["target_resource_id"] = "res-controller"
	u := decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "frozen", body), 201)
	if status := putUploadPart(t, u.Parts[0], data); status != 200 {
		t.Fatalf("part %d", status)
	}
	// finalize 不接受目标：想在这里换目标只能得到 400，而不是被静默忽略
	if w := uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "fin", map[string]any{"engine": "borndigital", "target_resource_id": "res-other"}); w.Code != 400 {
		t.Fatalf("finalize accepted a target: %d %s", w.Code, w.Body.String())
	}
	decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "fin", map[string]any{"engine": "borndigital"}), 202)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.server.verifyUploads(ctx)
	read := func() targetWire {
		return decodeDiscovery[targetWire](t, uploadRequest(t, f, "GET", "/api/uploads/"+u.ID, f.aliceToken, "", nil), 200)
	}
	deadline := time.Now().Add(8 * time.Second)
	for read().Status != "ready" && time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
	}
	ready := read()
	if ready.Status != "ready" || ready.IngestStatus == nil || *ready.IngestStatus != "pending" || ready.IngestError != nil {
		t.Fatalf("byte-ready upload must read ingest pending: %+v", ready)
	}

	event := func() store.OutboxEvent {
		var e store.OutboxEvent
		if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT id, organization_id, type, payload, attempts, created_at FROM control.control_outbox WHERE organization_id=$1 AND type='DocumentSubmitted' AND payload->>'upload_id'=$2`, f.org, u.ID).Scan(&e.ID, &e.OrganizationID, &e.Type, &e.Payload, &e.Attempts, &e.CreatedAt); err != nil {
			t.Fatal(err)
		}
		return e
	}
	var payload map[string]any
	if err := json.Unmarshal(event().Payload, &payload); err != nil || payload["target_resource_id"] != "res-controller" {
		t.Fatalf("event target not bound from the stored session: %v %v", payload, err)
	}

	client := &http.Client{Timeout: 5 * time.Second}
	deliver := func(status int, body string) targetWire {
		corpus.events.Store(func() (int, string) { return status, body })
		e := event()
		// 与 ClaimOutbox 同样记一次尝试，但只动这一行：全局领取会抢走并行包的事件
		if err := f.server.store.Pool().QueryRow(context.Background(), `UPDATE control.control_outbox SET attempts=attempts+1 WHERE id=$1 RETURNING attempts`, e.ID).Scan(&e.Attempts); err != nil {
			t.Fatal(err)
		}
		f.server.deliverEvent(context.Background(), client, e)
		return read()
	}
	// 可恢复的 409（corpus 明说 retry）不是 ACK，也不是终态
	if got := deliver(409, `{"error":{"code":"document_state_changed"}}`); got.IngestStatus == nil || *got.IngestStatus != "retrying" || got.IngestError != nil {
		t.Fatalf("transient conflict: %+v", got)
	}
	// 目标里已经有这份内容：确定性拒绝，透出契约码，事件离开投递队列
	if got := deliver(409, `{"error":{"code":"resource_version_exists"}}`); got.IngestStatus == nil || *got.IngestStatus != "rejected" || got.IngestError == nil || *got.IngestError != "resource_version_exists" {
		t.Fatalf("deterministic conflict: %+v", got)
	}
	var rejected bool
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT rejected_at IS NOT NULL FROM control.control_outbox WHERE id=$1`, event().ID).Scan(&rejected); err != nil || !rejected {
		t.Fatalf("rejection not terminal on the event row: %v %v", rejected, err)
	}
}

func TestUploadDuplicateEventIsTheOnlyConflictThatAcknowledges(t *testing.T) {
	f, corpus := uploadTargetFixture(t)
	data := []byte("%PDF-1.4 independent")
	u := decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "plain", uploadBody(data)), 201)
	if u.TargetResourceID != nil {
		t.Fatalf("untargeted upload grew a target: %+v", u)
	}
	if status := putUploadPart(t, u.Parts[0], data); status != 200 {
		t.Fatalf("part %d", status)
	}
	decodeDiscovery[targetWire](t, uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "", nil), 202)
	// verify 只认领取租约：先 PendingVerification 领一行（单测里就是这一行），
	// 再 MarkUploadVerified —— 与 verifyUploads 生产路径同顺序。
	claimed, err := f.server.store.PendingVerification(context.Background(), 16)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, c := range claimed {
		if c.ID == u.ID {
			found = true
		}
	}
	if !found {
		t.Fatalf("upload not claimed by PendingVerification: %s", u.ID)
	}
	if err := f.server.store.MarkUploadVerified(context.Background(), f.org, u.ID, uploadBody(data)["sha256"].(string)); err != nil {
		t.Fatal(err)
	}
	var e store.OutboxEvent
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT id, organization_id, type, payload, attempts, created_at FROM control.control_outbox WHERE organization_id=$1 AND payload->>'upload_id'=$2`, f.org, u.ID).Scan(&e.ID, &e.OrganizationID, &e.Type, &e.Payload, &e.Attempts, &e.CreatedAt); err != nil {
		t.Fatal(err)
	}
	corpus.events.Store(func() (int, string) { return 409, `{"error":{"code":"duplicate_event"}}` })
	f.server.deliverEvent(context.Background(), &http.Client{Timeout: 5 * time.Second}, e)
	got := decodeDiscovery[targetWire](t, uploadRequest(t, f, "GET", "/api/uploads/"+u.ID, f.aliceToken, "", nil), 200)
	if got.IngestStatus == nil || *got.IngestStatus != "ready" || got.IngestError != nil {
		t.Fatalf("duplicate_event must acknowledge: %+v", got)
	}
}
