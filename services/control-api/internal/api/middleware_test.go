package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
)

func TestWikiGenerationSharesRateBudgetWithoutThrottlingReadsOrPublish(t *testing.T) {
	s := &Server{cfg: &config.Config{KnowledgeRatePerMin: 1}, limiter: ratelimit.NewMemory()}
	calls := 0
	handler := s.domainThrottle(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusNoContent)
	}))
	steps := []struct {
		method string
		path   string
		actor  string
		status int
	}{
		{http.MethodGet, "/api/wikis", "reviewer", http.StatusNoContent},
		{http.MethodPost, "/api/wikis/wiki-a/publish", "reviewer", http.StatusNoContent},
		{http.MethodPost, "/api/wikis", "reviewer", http.StatusNoContent},
		{http.MethodPost, "/api/wikis/wiki-a/revisions", "reviewer", http.StatusTooManyRequests},
		{http.MethodPost, "/api/wikis/wiki-b/revisions", "reviewer", http.StatusTooManyRequests},
		{http.MethodGet, "/api/wikis/wiki-a", "reviewer", http.StatusNoContent},
		{http.MethodPost, "/api/wikis", "another-reviewer", http.StatusNoContent},
	}
	for _, step := range steps {
		request := httptest.NewRequest(step.method, step.path, nil)
		request = request.WithContext(identity.With(request.Context(), &identity.Actor{ID: step.actor}))
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != step.status {
			t.Fatalf("%s %s actor=%s: status %d, want %d; body=%s", step.method, step.path,
				step.actor, response.Code, step.status, response.Body.String())
		}
	}
	if calls != 5 {
		t.Fatalf("blocked generation reached downstream: calls=%d, want 5", calls)
	}
}
