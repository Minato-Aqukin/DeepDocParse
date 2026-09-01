package config_test

import (
	"strings"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/config"
)

func good(t *testing.T) {
	t.Helper()
	t.Setenv("JWT_SECRET", strings.Repeat("a", 48))
	t.Setenv("SERVICE_TOKEN", strings.Repeat("b", 32))
	t.Setenv("OBJECT_SECRET_KEY", "not-the-default")
}

// TestPlaceholderSecretsAreRejected：带着 change-me 跑起来的话鉴权形同虚设，
// 而**运行时不会有任何报错** —— 这个项目在 JWT_SECRET 与 SERVICE_TOKEN 上
// 各踩过一次，所以它必须是"拒绝启动"而不是警告。
func TestPlaceholderSecretsAreRejected(t *testing.T) {
	for _, key := range []string{"JWT_SECRET", "SERVICE_TOKEN"} {
		t.Run(key, func(t *testing.T) {
			good(t)
			t.Setenv(key, "change-me")
			if _, err := config.Load(); err == nil {
				t.Fatalf("%s 是占位值却启动成功了", key)
			}
		})
	}
}

// TestShortJWTSecretRejected：32 字节以下的 HS256 密钥是可暴力的。
func TestShortJWTSecretRejected(t *testing.T) {
	good(t)
	t.Setenv("JWT_SECRET", "short-secret")
	if _, err := config.Load(); err == nil {
		t.Fatal("短密钥被接受了")
	}
}

// TestInsecureDefaultsEscapeHatchIsExplicit：逃生口必须显式且留痕。
func TestInsecureDefaultsEscapeHatchIsExplicit(t *testing.T) {
	t.Setenv("JWT_SECRET", "change-me")
	t.Setenv("SERVICE_TOKEN", "change-me")
	t.Setenv("ALLOW_INSECURE_DEFAULTS", "true")
	if _, err := config.Load(); err != nil {
		t.Fatalf("显式开了逃生口仍然拒绝启动：%v", err)
	}
}

// TestPartSizeFloor：S3 兼容实现要求非最后一片 >= 5MiB。
// 写小了会**在 complete multipart 时才报错** —— 那时文件已经传完了。
func TestPartSizeFloor(t *testing.T) {
	good(t)
	t.Setenv("UPLOAD_PART_SIZE", "1048576")
	if _, err := config.Load(); err == nil {
		t.Fatal("小于 5MiB 的分片大小被接受了")
	}
}

// TestPresignTTLCeiling：签名 URL 泄露的代价与 TTL 成正比。
func TestPresignTTLCeiling(t *testing.T) {
	good(t)
	t.Setenv("PRESIGN_TTL_SECONDS", "86400")
	if _, err := config.Load(); err == nil {
		t.Fatal("24 小时的预签名 TTL 被接受了")
	}
}

// TestMIMEAllowlistIsAllowlist：**白名单而不是黑名单**。
// 上传 text/html 并 inline 打开就是本站同源 XSS。
func TestMIMEAllowlistIsAllowlist(t *testing.T) {
	good(t)
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.MIMEAllowed("application/pdf") {
		t.Fatal("默认白名单里应当有 application/pdf")
	}
	// 带参数的 content-type 也要认得出来
	if !cfg.MIMEAllowed("application/pdf; charset=binary") {
		t.Fatal("带参数的 MIME 应当被正确解析")
	}
	for _, bad := range []string{"text/html", "image/svg+xml", "application/octet-stream", ""} {
		if cfg.MIMEAllowed(bad) {
			t.Fatalf("%q 不该在白名单里", bad)
		}
	}
}

func TestEmptyMIMEAllowlistRejected(t *testing.T) {
	good(t)
	t.Setenv("ALLOWED_UPLOAD_MIME", "")
	// 空串会走 fallback，所以用一个只有分隔符的值来制造真正的空列表
	t.Setenv("ALLOWED_UPLOAD_MIME", ",, ,")
	if _, err := config.Load(); err == nil {
		t.Fatal("空白名单被接受了 —— 那等于禁止一切上传，且不会有人发现")
	}
}

func TestBadRegistrationModeRejected(t *testing.T) {
	good(t)
	t.Setenv("REGISTRATION_MODE", "sometimes")
	if _, err := config.Load(); err == nil {
		t.Fatal("非法的 REGISTRATION_MODE 被接受了")
	}
}
