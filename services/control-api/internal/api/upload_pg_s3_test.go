package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/store"
)

type uploadWire struct {
	ID              string                      `json:"id"`
	Status          string                      `json:"status"`
	AllocationState string                      `json:"allocation_state"`
	InputState      string                      `json:"input_state"`
	ObjectKey       string                      `json:"object_key"`
	RequestDigest   string                      `json:"request_digest"`
	VerifiedSHA256  string                      `json:"verified_sha256"`
	Parts           []objectstore.Part          `json:"parts"`
	Completed       []objectstore.CompletedPart `json:"completed_parts"`
}

func uploadRealFixture(t *testing.T, ttl time.Duration) (*discoveryFixture, *atomic.Int32, *atomic.Int32) {
	t.Helper()
	endpoint := os.Getenv("UPLOAD_TEST_S3_ENDPOINT")
	if endpoint == "" {
		t.Skip("UPLOAD_TEST_S3_ENDPOINT and CONTROL_TEST_DATABASE_URL required for real multipart tests")
	}
	f := discoveryPGFixture(t)
	var dropCreate, dropComplete atomic.Int32
	target, err := url.Parse("http://" + endpoint)
	if err != nil {
		t.Fatal(err)
	}
	p := httputil.NewSingleHostReverseProxy(target)
	p.ErrorLog = log.New(io.Discard, "", 0)
	p.ModifyResponse = func(r *http.Response) error {
		if r.Request.Method == "POST" && r.StatusCode < 300 {
			if r.Request.URL.Query().Has("uploads") && dropCreate.CompareAndSwap(1, 0) {
				r.Body.Close()
				return errors.New("lost create receipt after S3 committed")
			}
			if r.Request.URL.Query().Has("uploadId") && dropComplete.CompareAndSwap(1, 0) {
				r.Body.Close()
				return errors.New("lost completion receipt after S3 committed")
			}
		}
		return nil
	}
	proxy := httptest.NewServer(p)
	t.Cleanup(proxy.Close)
	objects, err := objectstore.Open(context.Background(), objectstore.Config{Endpoint: strings.TrimPrefix(proxy.URL, "http://"), PublicEndpoint: endpoint, AccessKey: os.Getenv("UPLOAD_TEST_S3_ACCESS_KEY"), SecretKey: os.Getenv("UPLOAD_TEST_S3_SECRET_KEY"), Bucket: "upload-test-" + strings.ToLower(auth.NewID()), Region: "us-east-1", PresignTTL: ttl})
	if err != nil {
		t.Fatal(err)
	}
	f.server.objects = objects
	f.server.cfg.MaxUploadBytes = 30 << 20
	f.server.cfg.UploadPartSize = 5 << 20
	f.server.cfg.UploadTTL = time.Hour
	f.server.cfg.AllowedMIME = []string{"application/pdf"}
	// Every test writes isolated synthetic objects, then cleans only that org.
	t.Cleanup(func() {
		ctx := context.Background()
		rows, err := f.server.store.Pool().Query(ctx, `SELECT object_key,coalesce(upload_id,'') FROM control.upload_sessions WHERE organization_id=$1`, f.org)
		if err != nil {
			t.Error(err)
			return
		}
		type item struct{ key, id string }
		var all []item
		for rows.Next() {
			var x item
			if err := rows.Scan(&x.key, &x.id); err != nil {
				t.Error(err)
			}
			all = append(all, x)
		}
		rows.Close()
		for _, x := range all {
			ids, _ := objects.FindMultipart(ctx, x.key)
			for _, id := range ids {
				_ = objects.AbortMultipart(ctx, x.key, id)
			}
			_ = objects.Remove(ctx, x.key)
		}
		_, _ = f.server.store.Pool().Exec(ctx, `DELETE FROM control.control_outbox WHERE organization_id=$1`, f.org)
	})
	return f, &dropCreate, &dropComplete
}
func uploadRequest(t *testing.T, f *discoveryFixture, method, path, token, key string, body any) *httptest.ResponseRecorder {
	t.Helper()
	raw, _ := json.Marshal(body)
	r := httptest.NewRequest(method, path, bytes.NewReader(raw))
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	if key != "" {
		r.Header.Set("Idempotency-Key", key)
	}
	w := httptest.NewRecorder()
	f.handler.ServeHTTP(w, r)
	return w
}
func uploadBody(data []byte) map[string]any {
	digest := sha256.Sum256(data)
	return map[string]any{"filename": "input.pdf", "size": len(data), "mime": "application/pdf", "sha256": hex.EncodeToString(digest[:])}
}
func putUploadPart(t *testing.T, p objectstore.Part, data []byte) int {
	t.Helper()
	req, _ := http.NewRequest("PUT", p.URL, bytes.NewReader(data))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode
}
func readUpload(t *testing.T, w *httptest.ResponseRecorder, status int) uploadWire {
	t.Helper()
	return decodeDiscovery[uploadWire](t, w, status)
}

func TestUploadRealConcurrentCreationActorIsolationAndQuota(t *testing.T) {
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	data := []byte("full input digest")
	body := uploadBody(data)
	// One page is a real held reservation, not the old check-only quota path.
	_, err := f.server.store.Pool().Exec(context.Background(), `INSERT INTO control.quotas(organization_id,pages_limit) VALUES($1,1)`, f.org)
	if err != nil {
		t.Fatal(err)
	}
	const n = 12
	results := make(chan *httptest.ResponseRecorder, n)
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "stable-create", body)
		}()
	}
	wg.Wait()
	close(results)
	id := ""
	for w := range results {
		if w.Code != 200 && w.Code != 201 && w.Code != 202 {
			t.Fatalf("create status %d: %s", w.Code, w.Body.String())
		}
		var u uploadWire
		if err := json.Unmarshal(w.Body.Bytes(), &u); err != nil {
			t.Fatal(err)
		}
		if id != "" && u.ID != id {
			t.Fatal("created multiple logical uploads")
		}
		id = u.ID
	}
	u := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/reconcile", f.aliceToken, "stable-create", nil), 200)
	if u.ID != id || u.AllocationState != "ready" || u.InputState != "waiting_input" || len(u.Parts) != 1 {
		t.Fatalf("bad receipt %+v", u)
	}
	ids, err := f.server.objects.FindMultipart(context.Background(), u.ObjectKey)
	if err != nil || len(ids) != 1 {
		t.Fatalf("physical multipart duplicates %v %v", ids, err)
	}
	var count, held int
	if err = f.server.store.Pool().QueryRow(context.Background(), `SELECT count(*),sum(reserved_pages) FROM control.upload_sessions WHERE organization_id=$1`, f.org).Scan(&count, &held); err != nil || count != 1 || held != 1 {
		t.Fatalf("reservation %d/%d %v", count, held, err)
	}
	changed := uploadBody([]byte("different input"))
	if w := uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "stable-create", changed); w.Code != 409 {
		t.Fatalf("changed content not conflict %d", w.Code)
	}
	if w := uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "another-create", body); w.Code != 402 {
		t.Fatalf("did not hold quota %d", w.Code)
	}
	// Admin is another actor: it must not receive Alice's object key or URLs.
	for _, path := range []string{"/api/uploads/reconcile", "/api/uploads/" + id} {
		w := uploadRequest(t, f, "GET", path, f.adminToken, "stable-create", nil)
		if w.Code != 404 || strings.Contains(w.Body.String(), u.ObjectKey) || strings.Contains(w.Body.String(), "X-Amz") {
			t.Fatalf("actor leakage %s", w.Body.String())
		}
	}
	_, err = f.server.store.Pool().Exec(context.Background(), `UPDATE control.quotas SET pages_limit=2 WHERE organization_id=$1`, f.org)
	if err != nil {
		t.Fatal(err)
	}
	other := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.adminToken, "stable-create", body), 201)
	if other.ID == id || other.ObjectKey == u.ObjectKey {
		t.Fatal("cross actor idempotency reuse")
	}
}

func TestUploadRealLostCreateReceiptResumesWithoutReallocating(t *testing.T) {
	f, drop, _ := uploadRealFixture(t, 15*time.Minute)
	drop.Store(1)
	body := uploadBody([]byte("recover allocation"))
	lost := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "lost-create", body), 202)
	if lost.AllocationState != "unknown" || len(lost.Parts) != 0 || lost.InputState != "waiting_input" {
		t.Fatalf("unknown was fabricated as success %+v", lost)
	}
	// A restarted HTTP entry uses only the persisted store and S3 state.
	restarted := *f.server
	f.handler = restarted.Routes()
	found := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/reconcile", f.aliceToken, "lost-create", nil), 200)
	if found.ID != lost.ID || found.AllocationState != "ready" || len(found.Parts) != 1 {
		t.Fatalf("could not recover %+v", found)
	}
	again := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "lost-create", body), 200)
	if again.ID != lost.ID {
		t.Fatal("created another logical upload")
	}
	ids, err := f.server.objects.FindMultipart(context.Background(), found.ObjectKey)
	if err != nil || len(ids) != 1 {
		t.Fatalf("new multipart after lost receipt %v %v", ids, err)
	}
}

func TestUploadRealUnconfirmedAndAmbiguousClaimsNeverRecreate(t *testing.T) {
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	ctx := context.Background()
	for _, ambiguous := range []bool{false, true} {
		key := auth.NewID()
		digest := "sha256:" + strings.Repeat("a", 64)
		sha := strings.Repeat("b", 64)
		partSize := int64(5 << 20)
		candidate := &store.UploadSession{OrganizationID: f.org, ActorID: f.alice.ID, ActorKind: "user", ObjectKey: "uploads/" + f.org + "/" + auth.NewID(), Filename: "input.pdf", MIME: "application/pdf", DeclaredSize: 20, DeclaredSHA256: &sha, ExpiresAt: time.Now().Add(time.Hour), CreateIdempotencyKey: &key, RequestDigest: &digest, PartSize: &partSize}
		u, _, err := f.server.store.ClaimUpload(ctx, candidate, 1)
		if err != nil {
			t.Fatal(err)
		}
		started, err := f.server.store.StartUploadAllocation(ctx, f.org, u.ID)
		if err != nil || !started {
			t.Fatal("claim failed")
		}
		want := 0
		if ambiguous {
			want = 2
			for i := 0; i < 2; i++ {
				if _, err := f.server.objects.BeginMultipart(ctx, u.ObjectKey, u.MIME); err != nil {
					t.Fatal(err)
				}
			}
		}
		for i := 0; i < 3; i++ {
			out := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/reconcile", f.aliceToken, key, nil), 200)
			if out.AllocationState != "unknown" || len(out.Parts) > 0 {
				t.Fatalf("unknown became runnable %+v", out)
			}
		}
		ids, err := f.server.objects.FindMultipart(ctx, u.ObjectKey)
		if err != nil || len(ids) != want {
			t.Fatalf("reconcile allocated multipart %d wanted %d: %v", len(ids), want, err)
		}
	}
}

func TestUploadRealExpiredPresignResumeCompleteLossAndFullDigest(t *testing.T) {
	f, _, dropComplete := uploadRealFixture(t, time.Second)
	data := bytes.Repeat([]byte("PDF synthetic input\n"), 300000) // >5 MiB, actually uses two parts.
	body := uploadBody(data)
	u := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "resume", body), 201)
	if len(u.Parts) != 2 {
		t.Fatalf("wanted two parts, got %d", len(u.Parts))
	}
	if status := putUploadPart(t, u.Parts[0], data[:5<<20]); status != 200 {
		t.Fatalf("first part: %d", status)
	}
	if w := uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "finalize-stable", map[string]any{"engine": "borndigital"}); w.Code != 409 {
		t.Fatalf("incomplete upload was merged: %d %s", w.Code, w.Body.String())
	}
	time.Sleep(2100 * time.Millisecond)
	if status := putUploadPart(t, u.Parts[1], data[5<<20:]); status != 403 {
		t.Fatalf("old presign did not expire: %d", status)
	}
	// Restart changes default geometry; persisted part size must remain 5MiB.
	f.server.cfg.UploadPartSize = 16 << 20
	resumed := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/reconcile", f.aliceToken, "resume", nil), 200)
	if len(resumed.Completed) != 1 || resumed.Completed[0].PartNumber != 1 || len(resumed.Parts) != 1 || resumed.Parts[0].PartNumber != 2 {
		t.Fatalf("lost completed part %+v", resumed)
	}
	if status := putUploadPart(t, resumed.Parts[0], data[5<<20:]); status != 200 {
		t.Fatalf("resume part: %d", status)
	}
	dropComplete.Store(1)
	finalized := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "finalize-stable", map[string]any{"engine": "borndigital"}), 202)
	if finalized.Status != "verifying" || finalized.InputState != "content_verifying" {
		t.Fatalf("accepted without verification %+v", finalized)
	}
	// Engine/options are part of finalization identity, ETags are transport only.
	if w := uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "finalize-stable", map[string]any{"engine": "different"}); w.Code != 409 {
		t.Fatalf("finalize digest not bound: %d", w.Code)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.server.verifyUploads(ctx)
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
		row, err := f.server.store.UploadSession(context.Background(), f.org, u.ID)
		if err != nil {
			t.Fatal(err)
		}
		if row.Status == "ready" {
			break
		}
	}
	ready := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/reconcile", f.aliceToken, "resume", nil), 200)
	if ready.Status != "ready" || ready.InputState != "content_verified" || ready.VerifiedSHA256 != body["sha256"] {
		t.Fatalf("full object not verified %+v", ready)
	}
	readUpload(t, uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "finalize-stable", map[string]any{"engine": "borndigital"}), 202)
	var count int
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT count(*) FROM control.control_outbox WHERE organization_id=$1 AND type='DocumentSubmitted' AND payload->>'upload_id'=$2`, f.org, u.ID).Scan(&count); err != nil || count != 1 {
		t.Fatalf("duplicate task event %d %v", count, err)
	}
}

func TestUploadRealDigestMismatchIsNotReady(t *testing.T) {
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	data := []byte("actual bytes")
	body := uploadBody([]byte("wrong digest"))
	body["size"] = len(data)
	u := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "mismatch", body), 201)
	if status := putUploadPart(t, u.Parts[0], data); status != 200 {
		t.Fatalf("part %d", status)
	}
	readUpload(t, uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "", nil), 202)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go f.server.verifyUploads(ctx)
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		time.Sleep(100 * time.Millisecond)
		row, err := f.server.store.UploadSession(context.Background(), f.org, u.ID)
		if err != nil {
			t.Fatal(err)
		}
		if row.Status == "failed" {
			break
		}
	}
	out := readUpload(t, uploadRequest(t, f, "GET", "/api/uploads/"+u.ID, f.aliceToken, "", nil), 200)
	if out.Status != "failed" || out.VerifiedSHA256 != "" {
		t.Fatalf("mismatch admitted %+v", out)
	}
	var count int
	if err := f.server.store.Pool().QueryRow(context.Background(), `SELECT count(*) FROM control.control_outbox WHERE organization_id=$1`, f.org).Scan(&count); err != nil || count != 0 {
		t.Fatalf("invalid content created task %d %v", count, err)
	}
}

func TestUploadRealCreationKeysAlsoIsolateOrganizationAndActorKind(t *testing.T) {
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	other := discoveryPGFixture(t)
	key := "same-key"
	digest := "sha256:" + strings.Repeat("a", 64)
	sha := strings.Repeat("b", 64)
	part := int64(5 << 20)
	seen := map[string]bool{}
	for _, scope := range []struct{ org, kind string }{{f.org, "user"}, {other.org, "user"}, {f.org, "api_key"}} {
		candidate := &store.UploadSession{OrganizationID: scope.org, ActorID: f.alice.ID, ActorKind: scope.kind, ObjectKey: "uploads/" + scope.org + "/" + auth.NewID(), Filename: "same.pdf", MIME: "application/pdf", DeclaredSize: 10, DeclaredSHA256: &sha, ExpiresAt: time.Now().Add(time.Hour), CreateIdempotencyKey: &key, RequestDigest: &digest, PartSize: &part}
		row, created, err := f.server.store.ClaimUpload(context.Background(), candidate, 1)
		if err != nil || !created || seen[row.ID] {
			t.Fatalf("scope collision %+v %v", scope, err)
		}
		seen[row.ID] = true
		recovered, err := f.server.store.UploadByCreationKey(context.Background(), scope.org, scope.kind, f.alice.ID, key)
		if err != nil || recovered.ID != row.ID {
			t.Fatalf("scope lookup crossed boundary %+v %v", scope, err)
		}
	}
}
