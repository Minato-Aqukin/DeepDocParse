package objectstore

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// 内外两侧的 **scheme** 可以不同：内网走回环明文，公网由隧道 / 反代终结 TLS。
//
// 这条守的是一处不会报错的失效 —— 只有一个 Secure 开关时，给浏览器的预签名
// URL 会签成 `http://`，而页面本身是 https 打开的，浏览器按混合内容直接拦掉
// 上传与预览：**服务端零报错，健康检查全绿，只有前端不能用**。
// 反过来把唯一那个开关打开，则内网 client 会去 https 连回环，
// 启动自检就连不上对象存储。
func TestPublicSecureOnlyAffectsTheBrowserFacingClient(t *testing.T) {
	// Open() 会真发一次 HEAD /{bucket} 探桶，拿一个假 endpoint 顶住
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	internalHost := strings.TrimPrefix(srv.URL, "http://")

	ctx := context.Background()
	s, err := Open(ctx, Config{
		Endpoint:       internalHost,
		PublicEndpoint: "ddp.example.com",
		AccessKey:      "ak",
		SecretKey:      "sk",
		Bucket:         "deepdocparse",
		Secure:         false,
		PublicSecure:   true,
		Region:         "us-east-1",
		PresignTTL:     15 * time.Minute,
	})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}

	pub, _, err := s.PresignGet(ctx, "objects/a", "a.pdf", "application/pdf", "inline")
	if err != nil {
		t.Fatalf("PresignGet: %v", err)
	}
	if !strings.HasPrefix(pub, "https://ddp.example.com/") {
		t.Fatalf("给浏览器的预签名 URL 应当是 https 的公网地址，实际 %s", pub)
	}

	internal, _, err := s.PresignGetInternal(ctx, "objects/a", "a.pdf", "application/pdf", "inline")
	if err != nil {
		t.Fatalf("PresignGetInternal: %v", err)
	}
	if !strings.HasPrefix(internal, "http://"+internalHost+"/") {
		t.Fatalf("给服务的预签名 URL 应当留在内网明文地址上，实际 %s", internal)
	}
}

// 不设 PublicSecure 时行为与从前逐字一致：两侧同 scheme，
// 并且 endpoint 相同时仍然复用同一个 client。
func TestPublicSecureDefaultsToTheInternalScheme(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	internalHost := strings.TrimPrefix(srv.URL, "http://")

	ctx := context.Background()
	s, err := Open(ctx, Config{
		Endpoint:   internalHost,
		AccessKey:  "ak",
		SecretKey:  "sk",
		Bucket:     "deepdocparse",
		Region:     "us-east-1",
		PresignTTL: 15 * time.Minute,
	})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if s.client != s.publicClient {
		t.Fatal("endpoint 与 scheme 都相同时不该多建一个 client")
	}
	pub, _, err := s.PresignGet(ctx, "objects/a", "a.pdf", "application/pdf", "inline")
	if err != nil {
		t.Fatalf("PresignGet: %v", err)
	}
	if !strings.HasPrefix(pub, "http://") {
		t.Fatalf("未开 PublicSecure 时应当仍是 http，实际 %s", pub)
	}
}

// 内外**同一个 host、只有 scheme 不同**：两边都经同一层反代时就是这个形状
// （公网 https://ddp.example.com/{桶}/{键}，内网 http://同一个 host）。
//
// 这条补的是一个覆盖洞：上面两条都设了不同的 PublicEndpoint，于是
// `PublicEndpoint != Endpoint` 那个子句先命中 —— 把这次新加的
// `|| c.PublicSecure != c.Secure` 整条删掉，那两条仍然全绿。
func TestPublicSecureAloneSplitsTheClients(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	host := strings.TrimPrefix(srv.URL, "http://")

	ctx := context.Background()
	s, err := Open(ctx, Config{
		Endpoint: host,
		// **故意留空**：留空时公网那一侧要回落到内网 endpoint，只换 scheme
		PublicEndpoint: "",
		AccessKey:      "ak",
		SecretKey:      "sk",
		Bucket:         "deepdocparse",
		Secure:         false,
		PublicSecure:   true,
		Region:         "us-east-1",
		PresignTTL:     15 * time.Minute,
	})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if s.client == s.publicClient {
		t.Fatal("scheme 不同就必须是两个 client")
	}
	pub, _, err := s.PresignGet(ctx, "objects/a", "a.pdf", "application/pdf", "inline")
	if err != nil {
		t.Fatalf("PresignGet: %v", err)
	}
	if !strings.HasPrefix(pub, "https://"+host+"/") {
		t.Fatalf("公网那条应当是同 host 的 https，实际 %s", pub)
	}
	internal, _, err := s.PresignGetInternal(ctx, "objects/a", "a.pdf", "application/pdf", "inline")
	if err != nil {
		t.Fatalf("PresignGetInternal: %v", err)
	}
	if !strings.HasPrefix(internal, "http://"+host+"/") {
		t.Fatalf("内网那条应当留在 http，实际 %s", internal)
	}
}
