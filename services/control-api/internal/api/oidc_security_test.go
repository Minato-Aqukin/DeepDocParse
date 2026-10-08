package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/coreos/go-oidc/v3/oidc"
	"golang.org/x/oauth2"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/ratelimit"
)

// TestValidNextRejectsBackslashAndScheme：`/\evil.example`、
// `//evil`、绝对 URL、反斜杠一律拒掉；站内路径原样放行。
// 回归锚：旧 safeRedirect 只看 Parse+前缀，`/\evil.example` 能穿过去，
// 浏览器把 `\` 当 `/` 直接跳到攻击者域名。
func TestValidNextRejectsBackslashAndScheme(t *testing.T) {
	bad := []string{
		"/\\evil.example",
		"//evil.example/steal",
		"https://evil.example/steal",
		"http://evil.example",
		"javascript:alert(1)",
		"",
		"evil.example/path",
		"documents",
		"/documents\\..\\evil",
		"\\/evil.example",
	}
	for _, in := range bad {
		if validNext(in) {
			t.Fatalf("validNext(%q) = true，应当拒绝", in)
		}
		if got := safeRedirect(in); got != "/" {
			t.Fatalf("safeRedirect(%q) = %q，应当退回 /", in, got)
		}
	}
	for _, in := range []string{"/", "/documents", "/documents?id=1", "/a/b/c?x=1&y=2"} {
		if !validNext(in) {
			t.Fatalf("validNext(%q) = false，站内路径应当放行", in)
		}
		if got := safeRedirect(in); got != in {
			t.Fatalf("safeRedirect(%q) = %q，站内路径应当原样保留", in, got)
		}
	}
}

// TestOIDCLoginRejectsBadRedirectBeforeCookie：非法 redirect_uri
// 在 login 直接 400，不能先写 ddp_oidc_next cookie 再说。
// 用配好 stub OIDC 的 Server 跑真正的 handleOIDCLogin ——
// 顺序一半不再是空断言：坏跳转必须 400 bad_redirect，好跳转必须 302 且写 cookie。
func TestOIDCLoginRejectsBadRedirectBeforeCookie(t *testing.T) {
	s := &Server{
		cfg:      &config.Config{DefaultRole: "contributor", JWTSecret: strings.Repeat("s", 32)},
		sessions: auth.NewSessions(strings.Repeat("s", 32), 3600e9),
		limiter:  ratelimit.NewMemory(),
		oidc: &OIDC{
			verifier: stubVerifier{fn: func(ctx context.Context, raw string) (*oidc.IDToken, error) {
				return &oidc.IDToken{}, nil
			}},
			oauth: oauth2.Config{
				ClientID: "test-client", ClientSecret: "test-secret",
				Endpoint: oauth2.Endpoint{AuthURL: "https://idp.example/auth", TokenURL: "https://idp.example/token"},
			},
		},
	}
	for _, next := range []string{"/\\evil.example", "//evil.example/x", "https://evil.example"} {
		r := httptest.NewRequest("GET", "/api/auth/oidc/login?redirect_uri="+url.QueryEscape(next), nil)
		w := httptest.NewRecorder()
		err := s.handleOIDCLogin(w, r)
		var apiErr *apierr.Error
		if !isAPIError(err, &apiErr) || apiErr.Code != "bad_redirect" {
			t.Fatalf("bad redirect %q must be 400 bad_redirect, got: %v", next, err)
		}
		for _, c := range w.Result().Cookies() {
			if c.Name == "ddp_oidc_next" && c.Value != "" {
				t.Fatalf("bad redirect %q wrote ddp_oidc_next cookie", next)
			}
		}
	}
	// 好跳转：302 且 cookie 与重定向参数都写 —— 证明上面的 400 不是"什么都不做"。
	r := httptest.NewRequest("GET", "/api/auth/oidc/login?redirect_uri="+url.QueryEscape("/workbench/docs"), nil)
	w := httptest.NewRecorder()
	if err := s.handleOIDCLogin(w, r); err != nil {
		t.Fatalf("good redirect must pass: %v", err)
	}
	if w.Code != http.StatusFound {
		t.Fatalf("good redirect status=%d want 302", w.Code)
	}
	found := false
	for _, c := range w.Result().Cookies() {
		if c.Name == "ddp_oidc_next" && c.Value == "/workbench/docs" {
			found = true
		}
	}
	if !found {
		t.Fatal("good redirect did not write ddp_oidc_next cookie")
	}
}

// TestOIDCHandlersEmitAndEnforcePKCEAndNonce：login 重定向必须带上
// code_challenge（S256）与 nonce，callback 用 stub IdP 换 token、验 nonce ——
// nonce 对不上 401 bad_nonce。删掉生产配线（AuthCodeURL 参数、callback 比对）
// 任一处，这个测试就红：它跑的是真正的 handleOIDCLogin/handleOIDCCallback。
func TestOIDCHandlersEmitAndEnforcePKCEAndNonce(t *testing.T) {
	idp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse token form: %v", err)
		}
		// 授权码交换必须带 PKCE verifier —— login 存进 cookie 的那一个。
		if r.Form.Get("code_verifier") == "" {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"missing verifier"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"access_token":"stub-access","token_type":"Bearer","id_token":"stub-id-token"}`))
	}))
	defer idp.Close()

	newServer := func(verify func(ctx context.Context, raw string) (*oidc.IDToken, error)) *Server {
		return &Server{
			cfg:      &config.Config{DefaultRole: "contributor", JWTSecret: strings.Repeat("s", 32)},
			sessions: auth.NewSessions(strings.Repeat("s", 32), 3600e9),
			limiter:  ratelimit.NewMemory(),
			oidc: &OIDC{
				verifier: stubVerifier{fn: verify},
				oauth: oauth2.Config{
					ClientID: "test-client", ClientSecret: "test-secret",
					Endpoint: oauth2.Endpoint{AuthURL: idp.URL + "/auth", TokenURL: idp.URL + "/token"},
				},
			},
		}
	}

	// login：重定向 URL 带 code_challenge=S256 与 nonce，且 nonce 与 cookie 一致。
	s := newServer(nil)
	w := httptest.NewRecorder()
	if err := s.handleOIDCLogin(w, httptest.NewRequest(http.MethodGet, "/api/auth/oidc/login", nil)); err != nil {
		t.Fatalf("login: %v", err)
	}
	if w.Code != http.StatusFound {
		t.Fatalf("login status=%d want 302", w.Code)
	}
	target, err := url.Parse(w.Header().Get("Location"))
	if err != nil {
		t.Fatal(err)
	}
	q := target.Query()
	if q.Get("code_challenge") == "" || q.Get("code_challenge_method") != "S256" {
		t.Fatalf("login redirect lost PKCE: %s", target.String())
	}
	if q.Get("nonce") == "" {
		t.Fatalf("login redirect lost nonce: %s", target.String())
	}
	var state, verifier, nonce string
	for _, c := range w.Result().Cookies() {
		switch c.Name {
		case "ddp_oidc_state":
			state = c.Value
		case "ddp_oidc_verifier":
			verifier = c.Value
		case "ddp_oidc_nonce":
			nonce = c.Value
		}
	}
	if state == "" || verifier == "" || nonce == "" {
		t.Fatalf("login lost round-trip cookies: state=%q verifier-set=%v nonce-set=%v", state, verifier != "", nonce != "")
	}
	if q.Get("state") != state || q.Get("nonce") != nonce {
		t.Fatalf("redirect params not bound to cookies: %s", target.String())
	}
	if challenge := oauth2.S256ChallengeFromVerifier(verifier); q.Get("code_challenge") != challenge {
		t.Fatal("code_challenge is not S256 of the stored verifier")
	}

	callback := func(t *testing.T, srv *Server, state, nonce string) error {
		t.Helper()
		r := httptest.NewRequest(http.MethodGet, "/api/auth/oidc/callback?code=stub-code&state="+state, nil)
		r.AddCookie(&http.Cookie{Name: "ddp_oidc_state", Value: state})
		r.AddCookie(&http.Cookie{Name: "ddp_oidc_verifier", Value: verifier})
		r.AddCookie(&http.Cookie{Name: "ddp_oidc_nonce", Value: nonce})
		return srv.handleOIDCCallback(httptest.NewRecorder(), r)
	}

	// nonce 对不上：签名验过了也必须 401 bad_nonce —— 重放别的会话的 token 到此为止。
	s = newServer(func(ctx context.Context, raw string) (*oidc.IDToken, error) {
		return &oidc.IDToken{Nonce: "someone-elses-nonce"}, nil
	})
	err = callback(t, s, state, nonce)
	var apiErr *apierr.Error
	if !isAPIError(err, &apiErr) || apiErr.Code != "bad_nonce" || apiErr.Status != http.StatusUnauthorized {
		t.Fatalf("mismatched nonce must be 401 bad_nonce, got: %v", err)
	}

	// 空 nonce 同样 401 —— token 里没有 nonce 等于没绑这次会话。
	s = newServer(func(ctx context.Context, raw string) (*oidc.IDToken, error) {
		return &oidc.IDToken{Nonce: ""}, nil
	})
	err = callback(t, s, state, nonce)
	if !isAPIError(err, &apiErr) || apiErr.Code != "bad_nonce" {
		t.Fatalf("empty nonce must be bad_nonce, got: %v", err)
	}
}

type stubVerifier struct {
	fn func(ctx context.Context, raw string) (*oidc.IDToken, error)
}

func (v stubVerifier) Verify(ctx context.Context, raw string) (*oidc.IDToken, error) {
	return v.fn(ctx, raw)
}

func isAPIError(err error, target **apierr.Error) bool {
	if err == nil {
		return false
	}
	type causer interface{ Unwrap() error }
	for err != nil {
		if e, ok := err.(*apierr.Error); ok {
			*target = e
			return true
		}
		c, ok := err.(causer)
		if !ok {
			return false
		}
		err = c.Unwrap()
	}
	return false
}

// TestOIDCCookieHelpers：单次 cookie 缺失即错；clear 全清。
func TestOIDCCookieHelpers(t *testing.T) {
	r := httptest.NewRequest("GET", "/api/auth/oidc/callback?state=s", nil)
	if _, err := oidcCookie(r, "ddp_oidc_state"); err == nil {
		t.Fatal("缺 cookie 应当报错")
	}
	r.AddCookie(&http.Cookie{Name: "ddp_oidc_state", Value: "s"})
	v, err := oidcCookie(r, "ddp_oidc_state")
	if err != nil || v != "s" {
		t.Fatalf("取 cookie 失败：%v %q", err, v)
	}
	w := httptest.NewRecorder()
	clearOIDCCookies(w, r)
	cleared := map[string]bool{}
	for _, c := range w.Result().Cookies() {
		if c.MaxAge < 0 {
			cleared[c.Name] = true
		}
	}
	for _, name := range []string{"ddp_oidc_state", "ddp_oidc_verifier", "ddp_oidc_nonce", "ddp_oidc_next"} {
		if !cleared[name] {
			t.Fatalf("clearOIDCCookies 漏了 %s", name)
		}
	}
}
