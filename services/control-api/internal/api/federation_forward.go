package api

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"sync/atomic"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
)

// peerRequestBodyMaxBytes is the transport cap declared in federation-tasks-v1.yaml
// (x-ddp-peer-request-body-max-bytes). These routes are reachable without a session
// and corpus-api reads the whole body before it can check the node credential, so
// the public entry bounds it while streaming — chunked bodies included.
const peerRequestBodyMaxBytes int64 = 8 << 20

type peerBodyKey struct{}

// peerBody marks the request once the cap has been hit, so the proxy's error
// path can answer 413 instead of reporting an unreachable upstream.
type peerBody struct {
	io.ReadCloser
	tooLarge atomic.Bool
}

func (b *peerBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	var limit *http.MaxBytesError
	if errors.As(err, &limit) {
		b.tooLarge.Store(true)
	}
	return n, err
}

func peerBodyTooLarge() *apierr.Error {
	return apierr.New(http.StatusRequestEntityTooLarge, apierr.TypeInvalidRequest,
		"too_large", fmt.Sprintf("peer 请求体上限 %d 字节", peerRequestBodyMaxBytes))
}

// mountFederationCorpus exposes only the corpus-owned peer operations declared
// in federation-tasks-v1.yaml. Directory, trust and scope routes stay in control.
// Do not reuse proxy.Upstream.ServeHTTP: it injects the control service token and
// authenticated actor, neither of which is authority for a peer operation.
func (s *Server) mountFederationCorpus(mux *http.ServeMux) {
	forward := &httputil.ReverseProxy{
		FlushInterval: -1,
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(s.corpus.Target)
			pr.Out.URL.RawQuery = pr.In.URL.RawQuery
			// An allowlist drops every internal identity/service header (including
			// future additions), Authorization and session cookies. Read Out so
			// ReverseProxy's removal of hop-by-hop headers is not undone.
			for name := range pr.Out.Header {
				switch http.CanonicalHeaderKey(name) {
				case "X-Ddp-Node-Credential", "X-Ddp-Target-Node", "Idempotency-Key",
					"Content-Type", "Accept", "X-Request-Id":
				default:
					delete(pr.Out.Header, name)
				}
			}
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			if body, ok := r.Context().Value(peerBodyKey{}).(*peerBody); ok && body.tooLarge.Load() {
				apierr.Write(w, r, peerBodyTooLarge())
				return
			}
			if errors.Is(err, context.Canceled) {
				return
			}
			slog.ErrorContext(r.Context(), "peer corpus upstream unreachable", "path", r.URL.Path, "err", err)
			apierr.Write(w, r, apierr.New(http.StatusBadGateway, apierr.TypeUpstream,
				"upstream_unreachable", "corpus-api 不可达").WithCause(err))
		},
	}
	if s.corpus != nil {
		// Reuse the existing streaming transport: no environment proxies and no
		// response-header deadline on long-lived requests.
		forward.Transport = s.corpus.Transport()
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if s.corpus == nil {
			apierr.Write(w, r, apierr.New(http.StatusServiceUnavailable, apierr.TypeUpstream,
				"upstream_unavailable", "corpus-api 未配置"))
			return
		}
		if r.ContentLength > peerRequestBodyMaxBytes {
			apierr.Write(w, r, peerBodyTooLarge())
			return
		}
		if r.Body != nil && r.Body != http.NoBody {
			body := &peerBody{ReadCloser: http.MaxBytesReader(w, r.Body, peerRequestBodyMaxBytes)}
			r = r.WithContext(context.WithValue(r.Context(), peerBodyKey{}, body))
			r.Body = body
		}
		forward.ServeHTTP(w, r)
	})
	for _, route := range []string{
		"POST /api/v1/federation/probes",
		"GET /api/v1/federation/probes/{probe_id}",
		"POST /api/v1/federation/admissions",
		"POST /api/v1/federation/admissions/lookup",
		"GET /api/v1/federation/tasks/{executor_task_id}",
		"POST /api/v1/federation/tasks/{executor_task_id}/cancel",
		"POST /api/v1/federation/resources/locate",
		"POST /api/v1/federation/results/resolve",
		"GET /api/v1/federation/evidence-sets/{set_ref}",
		"GET /api/v1/federation/published-collections",
	} {
		mux.Handle(route, handler)
	}
}
