package api

import (
	"net/http"
	"net/http/httptest"
)

func newRequest(target string) *http.Request {
	return httptest.NewRequest(http.MethodGet, target, nil)
}
