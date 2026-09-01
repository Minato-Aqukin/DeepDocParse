package auth_test

import (
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
)

const secret = "0123456789abcdef0123456789abcdef"

// TestSessionRejectsNoneAlgorithm：`alg: none` 是 JWT 最经典的绕过。
// 不锁算法的话，攻击者可以自己签一个 subject=任意用户的 token。
func TestSessionRejectsNoneAlgorithm(t *testing.T) {
	s := auth.NewSessions(secret, time.Hour)

	claims := jwt.MapClaims{"sub": "victim", "org": "o1", "role": "admin",
		"exp": time.Now().Add(time.Hour).Unix()}
	forged, err := jwt.NewWithClaims(jwt.SigningMethodNone, claims).
		SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.Verify(forged); err == nil {
		t.Fatal("alg=none 的 token 被接受了")
	}
}

func TestSessionRoundTrip(t *testing.T) {
	s := auth.NewSessions(secret, time.Hour)
	token, ttl, err := s.Issue("u1", "o1", "admin")
	if err != nil {
		t.Fatal(err)
	}
	if ttl != time.Hour {
		t.Fatalf("ttl = %v", ttl)
	}
	claims, err := s.Verify(token)
	if err != nil {
		t.Fatal(err)
	}
	if claims.Subject != "u1" || claims.OrganizationID != "o1" {
		t.Fatalf("claims 不对：%+v", claims)
	}
}

func TestSessionRejectsForeignSecret(t *testing.T) {
	token, _, _ := auth.NewSessions(secret, time.Hour).Issue("u1", "o1", "admin")
	other := auth.NewSessions("ffffffffffffffffffffffffffffffff", time.Hour)
	if _, err := other.Verify(token); err == nil {
		t.Fatal("换了密钥仍然验过了")
	}
}

// TestVerifyPasswordAlwaysDoesWork：hash 为空时也要走一遍假比对。
//
// 直接 return false 会让"这个用户名存在但只能 OIDC 登录"与
// "这个用户名不存在"产生可测量的耗时差 —— 那就是一个用户名枚举接口。
func TestVerifyPasswordAlwaysDoesWork(t *testing.T) {
	const rounds = 5
	measure := func(hash string) time.Duration {
		start := time.Now()
		for i := 0; i < rounds; i++ {
			auth.VerifyPassword(hash, "some-password")
		}
		return time.Since(start) / rounds
	}
	real, _ := auth.HashPassword("some-password-x", 10)
	empty := measure("")
	present := measure(real)

	// 空 hash 的耗时必须与真实比对同一量级。差 10 倍以上就说明
	// 空分支被短路了（真实 bcrypt 至少是毫秒级，短路是纳秒级）
	if empty*10 < present {
		t.Fatalf("空 hash 走了短路：空 %v vs 真实 %v —— 这是用户名枚举面", empty, present)
	}
}

// TestPasswordLengthBounds：bcrypt 只看前 72 字节，更长的部分被静默丢弃。
// 不拒绝的话，用户以为自己设了一个 100 位的强密码。
func TestPasswordLengthBounds(t *testing.T) {
	if _, err := auth.HashPassword("short", 10); err == nil {
		t.Fatal("太短的密码被接受了")
	}
	if _, err := auth.HashPassword(strings.Repeat("a", 100), 10); err == nil {
		t.Fatal("超过 72 字节的密码被接受了 —— 超出部分会被 bcrypt 静默丢弃")
	}
}

func TestAPIKeyShape(t *testing.T) {
	plain, prefix, hash := auth.NewAPIKey()
	if !strings.HasPrefix(plain, auth.KeyPrefix) {
		t.Fatalf("key 没有 sk- 前缀：%q", plain)
	}
	if !auth.LooksLikeAPIKey(plain) {
		t.Fatal("LooksLikeAPIKey 认不出自己生成的 key")
	}
	if len(prefix) >= len(plain) {
		t.Fatal("展示前缀不该等于完整 key")
	}
	if hash != auth.HashAPIKey(plain) {
		t.Fatal("哈希不稳定")
	}
	// 两次生成必须不同 —— 随机源坏了会静默产出同一把 key
	other, _, _ := auth.NewAPIKey()
	if other == plain {
		t.Fatal("两次生成的 key 相同")
	}
}

func TestNewIDMatchesPythonShape(t *testing.T) {
	// 迁移器要在两边对账，长度不同会让外键比对全部落空
	if got := len(auth.NewID()); got != 32 {
		t.Fatalf("NewID 长度 %d，Python 侧 uuid4().hex 是 32", got)
	}
}
