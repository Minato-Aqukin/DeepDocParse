package objectstore

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"
)

// A licensed offline copy's URL must not outlive the licence: a URL signed one second
// before the term ends would otherwise keep serving the original for the full TTL.
func TestPresignedGetNeverOutlivesItsDeadline(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	ctx := context.Background()
	s, err := Open(ctx, Config{
		Endpoint: strings.TrimPrefix(srv.URL, "http://"), AccessKey: "ak", SecretKey: "sk",
		Bucket: "deepdocparse", Region: "us-east-1", PresignTTL: 15 * time.Minute,
	})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	expiresOf := func(raw string) int {
		t.Helper()
		parsed, err := url.Parse(raw)
		if err != nil {
			t.Fatalf("parse %q: %v", raw, err)
		}
		seconds, err := strconv.Atoi(parsed.Query().Get("X-Amz-Expires"))
		if err != nil {
			t.Fatalf("X-Amz-Expires in %q: %v", raw, err)
		}
		return seconds
	}
	sign := map[string]func(time.Time) (string, time.Time, error){
		"browser": func(notAfter time.Time) (string, time.Time, error) {
			return s.PresignGet(ctx, "bundles/v/source.bin", "a.pdf", "application/pdf", "attachment", notAfter)
		},
		"internal": func(notAfter time.Time) (string, time.Time, error) {
			return s.PresignGetInternal(ctx, "bundles/v/source.bin", "a.pdf", "application/pdf", "attachment", notAfter)
		},
	}
	for name, presign := range sign {
		t.Run(name, func(t *testing.T) {
			native, _, err := presign(time.Time{})
			if err != nil || expiresOf(native) != int((15*time.Minute).Seconds()) {
				t.Fatalf("no deadline keeps the configured TTL: %v %s", err, native)
			}
			soon := time.Now().Add(30 * time.Second)
			capped, expires, err := presign(soon)
			if err != nil {
				t.Fatalf("presign before the deadline: %v", err)
			}
			if got := expiresOf(capped); got > 30 || got < 28 {
				t.Fatalf("URL lifetime %ds must end with the licence (≤30s)", got)
			}
			if expires.After(soon.Add(time.Second)) {
				t.Fatalf("reported expiry %s is after the deadline %s", expires, soon)
			}
			if _, _, err := presign(time.Now().Add(-time.Second)); !errors.Is(err, ErrNotAfterPassed) {
				t.Fatalf("an ended licence must not be signed, got %v", err)
			}
		})
	}
}
