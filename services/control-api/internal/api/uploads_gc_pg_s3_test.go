package api

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/objectstore"
)

// This opt-in drill exercises control's claim, corpus' actual HTTP collector and S3.
func TestTerminalUploadRealReclamation(t *testing.T) {
	corpus := os.Getenv("CLEANUP_TEST_CORPUS_URL")
	if corpus == "" {
		t.Skip("CLEANUP_TEST_CORPUS_URL required for isolated cleanup drill")
	}
	f, _, _ := uploadRealFixture(t, 15*time.Minute)
	ctx := context.Background()
	objects, err := objectstore.Open(ctx, objectstore.Config{
		Endpoint: os.Getenv("UPLOAD_TEST_S3_ENDPOINT"), PublicEndpoint: os.Getenv("UPLOAD_TEST_S3_ENDPOINT"),
		AccessKey: os.Getenv("UPLOAD_TEST_S3_ACCESS_KEY"), SecretKey: os.Getenv("UPLOAD_TEST_S3_SECRET_KEY"),
		Bucket: "cleanup-drill", Region: "us-east-1", PresignTTL: 15 * time.Minute,
	})
	if err != nil {
		t.Fatal(err)
	}
	f.server.objects = objects
	f.server.cfg.CorpusURL = corpus
	f.server.cfg.ServiceToken = "cleanup-service-token"
	for _, status := range []string{"failed", "expired", "rejected"} {
		data := []byte("synthetic cleanup input " + status)
		u := readUpload(t, uploadRequest(t, f, "POST", "/api/uploads", f.aliceToken, "cleanup-"+status, uploadBody(data)), 201)
		if got := putUploadPart(t, u.Parts[0], data); got != 200 {
			t.Fatalf("part status %d", got)
		}
		if w := uploadRequest(t, f, "POST", "/api/uploads/"+u.ID+"/finalize", f.aliceToken, "cleanup-finalize-"+status, map[string]any{"engine": "borndigital"}); w.Code != 202 {
			t.Fatalf("finalize %d", w.Code)
		}
		if _, _, err := objects.Stat(ctx, u.ObjectKey); err != nil {
			t.Fatalf("original missing before GC: %v", err)
		}
		if status == "rejected" {
			digest, _, err := objects.Digest(ctx, u.ObjectKey)
			if err != nil {
				t.Fatal(err)
			}
			if err := f.server.store.MarkUploadVerified(ctx, f.org, u.ID, digest); err != nil {
				t.Fatal(err)
			}
			if _, err := f.server.store.Pool().Exec(ctx, `UPDATE control.control_outbox SET rejected_at=now()-interval '3 hours',last_error='invalid_upload_target' WHERE organization_id=$1 AND payload->>'upload_id'=$2`, f.org, u.ID); err != nil {
				t.Fatal(err)
			}
		} else {
			if _, err := f.server.store.Pool().Exec(ctx, `UPDATE control.upload_sessions SET status=$2 WHERE id=$1`, u.ID, status); err != nil {
				t.Fatal(err)
			}
		}
		// A persisted abandoned multipart receipt is reclaimed too.
		if _, err := objects.BeginMultipart(ctx, u.ObjectKey, "application/pdf"); err != nil {
			t.Fatal(err)
		}
		if _, err := f.server.store.Pool().Exec(ctx, `UPDATE control.upload_sessions SET expires_at=now()-interval '3 hours',updated_at=now()-interval '3 hours' WHERE id=$1`, u.ID); err != nil {
			t.Fatal(err)
		}
		n, err := f.server.reclaimUploads(ctx)
		if err != nil || n != 1 {
			t.Fatalf("%s cleanup %d %v", status, n, err)
		}
		if _, _, err := objects.Stat(ctx, u.ObjectKey); err == nil {
			t.Fatal("terminal original still readable")
		}
		ids, err := objects.FindMultipart(ctx, u.ObjectKey)
		if err != nil || len(ids) != 0 {
			t.Fatalf("multipart not reclaimed: %v %v", ids, err)
		}
		var reclaimed bool
		if err := f.server.store.Pool().QueryRow(ctx, `SELECT reclaimed_at IS NOT NULL FROM control.upload_sessions WHERE id=$1`, u.ID).Scan(&reclaimed); err != nil || !reclaimed {
			t.Fatalf("no durable receipt %v", err)
		}
		t.Logf("%s: original removed, multipart removed, durable receipt retained", status)
	}
}
