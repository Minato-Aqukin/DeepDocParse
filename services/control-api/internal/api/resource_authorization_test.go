package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/httpx"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

func TestFileAuthorizationRechecksACLAndNeverFollowsRedirect(t *testing.T) {
	var status atomic.Int32
	status.Store(200)
	var calls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/internal/file-access/doc1" || r.URL.Query().Get("resource_id") != "asset1" {
			t.Errorf("wrong resource context: %s", r.URL)
		}
		if r.Header.Get("Authorization") != "Bearer internal-secret" || r.Header.Get(identity.HeaderUser) != "owner1" || r.Header.Get(identity.HeaderActor) != "key1" {
			t.Error("lost authenticated principal")
		}
		if status.Load() == 302 {
			w.Header().Set("Location", "/should-not-follow")
		}
		w.WriteHeader(int(status.Load()))
		_, _ = fmt.Fprint(w, `{"document_id":"doc1","resource_id":"asset1","object_key":"uploads/1","mime":"application/pdf"}`)
	}))
	defer target.Close()
	up, _ := proxy.New("corpus", target.URL, "internal-secret")
	s := &Server{cfg: &config.Config{CorpusURL: target.URL, ServiceToken: "internal-secret"}, corpus: up}
	actor := &identity.Actor{Kind: identity.KindAPIKey, ID: "key1", UserID: "owner1", OrganizationID: "org1", Role: rbac.Contributor}
	access, err := s.documentAccess(context.Background(), actor, "doc1", "asset1")
	if err != nil || access.ResourceID != "asset1" {
		t.Fatalf("first access: %+v %v", access, err)
	}
	for _, c := range []struct{ upstream, expected int }{{403, 404}, {404, 404}, {409, 409}, {302, 502}, {500, 502}} {
		status.Store(int32(c.upstream))
		_, err := s.documentAccess(context.Background(), actor, "doc1", "asset1")
		var api *apierr.Error
		if !errors.As(err, &api) || api.Status != c.expected {
			t.Errorf("upstream %d gave %v", c.upstream, err)
		}
	}
	if calls.Load() != 6 {
		t.Fatalf("stale allow cache or redirect: %d calls", calls.Load())
	}
}

func TestUserOwnerHeaderCannotBeForged(t *testing.T) {
	req := httptest.NewRequest("GET", "/", nil)
	req.Header.Set(identity.HeaderUser, "victim")
	httpx.StripInboundIdentity(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		if r.Header.Get(identity.HeaderUser) != "" {
			t.Fatal("client owner header survived")
		}
		(&identity.Actor{ID: "key", UserID: "actual-owner", Kind: identity.KindAPIKey}).Apply(r, "control-api")
		if r.Header.Get(identity.HeaderUser) != "actual-owner" {
			t.Fatal("key owner not forwarded")
		}
	})).ServeHTTP(httptest.NewRecorder(), req)
}

func TestFileAuthorizationRejectsWrongVersionContext(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = fmt.Fprint(w, `{"document_id":"doc1","resource_id":"another-asset","object_key":"uploads/1"}`)
	}))
	defer target.Close()
	up, _ := proxy.New("corpus", target.URL, "secret")
	s := &Server{cfg: &config.Config{CorpusURL: target.URL, ServiceToken: "secret"}, corpus: up}
	_, err := s.documentAccess(context.Background(), &identity.Actor{ID: "u", UserID: "u", Kind: identity.KindUser}, "doc1", "asset1")
	var api *apierr.Error
	if !errors.As(err, &api) || api.Code != "authorization_invalid" {
		t.Fatalf("mismatched identity accepted: %v", err)
	}
}

func TestFileAuthorizationReturnsSourceUnavailable(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusGone)
		_, _ = fmt.Fprint(w, `{"error":{"code":"source_unavailable","type":"invalid_request_error","message":"fixed original is unavailable"}}`)
	}))
	defer target.Close()
	up, _ := proxy.New("corpus", target.URL, "secret")
	s := &Server{cfg: &config.Config{CorpusURL: target.URL, ServiceToken: "secret"}, corpus: up}
	actor := &identity.Actor{ID: "u", UserID: "u", Kind: identity.KindUser}
	recorder := httptest.NewRecorder()
	httpx.Wrap(func(_ http.ResponseWriter, r *http.Request) error {
		_, err := s.documentAccess(r.Context(), actor, "doc1", "asset1")
		return err
	}).ServeHTTP(recorder, httptest.NewRequest("GET", "/api/documents/doc1/download-url", nil))
	var response struct {
		Error apierr.Error `json:"error"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusGone || response.Error.Code != "source_unavailable" || response.Error.Type != apierr.TypeInvalidRequest {
		t.Fatalf("lost source refusal: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
}
