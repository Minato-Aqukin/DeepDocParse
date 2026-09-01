// Package auth 管三种凭据：密码、会话 JWT、API key。
//
// 三者的成本函数**故意不同**，理由见各自的注释 —— 这是旧系统里
// 一条正确的设计，原样继承。
package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/crypto/bcrypt"
)

// ---------------------------------------------------------------- 密码

// HashPassword 用 bcrypt。登录是低频操作，成本函数拖慢的是攻击者。
func HashPassword(plain string, cost int) (string, error) {
	if len(plain) < 8 {
		return "", errors.New("密码至少 8 位")
	}
	// bcrypt 只看前 72 字节，更长的部分被静默丢弃 —— 直接拒绝，
	// 免得用户以为自己设了一个 100 位的强密码
	if len(plain) > 72 {
		return "", errors.New("密码不能超过 72 字节（bcrypt 的硬性上限，更长的部分会被静默丢弃）")
	}
	h, err := bcrypt.GenerateFromPassword([]byte(plain), cost)
	return string(h), err
}

// VerifyPassword 是常数时间的（bcrypt 自己保证）。
//
// **hash 为空时也要走一遍假比对**：OIDC 用户没有密码哈希，
// 直接 return false 会让"这个用户名存在但只能 OIDC 登录"与
// "这个用户名不存在"产生可测量的耗时差 —— 那就是一个用户名枚举接口。
func VerifyPassword(hash, plain string) bool {
	if hash == "" {
		_ = bcrypt.CompareHashAndPassword(dummyHash, []byte(plain))
		return false
	}
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(plain)) == nil
}

// 一个固定的 bcrypt 哈希，只用来烧掉与真实比对相当的时间。
var dummyHash = []byte("$2a$12$C6UzMDM.H6dfI/f/IKcEe.rWfV0Uk4nkuHfDMpTM0nHzR9J5nOd6q")

// ---------------------------------------------------------------- 会话

type Claims struct {
	OrganizationID string `json:"org"`
	Role           string `json:"role"`
	jwt.RegisteredClaims
}

type Sessions struct {
	secret []byte
	ttl    time.Duration
}

func NewSessions(secret string, ttl time.Duration) *Sessions {
	return &Sessions{secret: []byte(secret), ttl: ttl}
}

func (s *Sessions) Issue(userID, orgID, role string) (string, time.Duration, error) {
	now := time.Now()
	claims := Claims{
		OrganizationID: orgID,
		Role:           role,
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   userID,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(s.ttl)),
			ID:        randomHex(8),
		},
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString(s.secret)
	return token, s.ttl, err
}

// Verify 解析并校验会话。
//
// **必须锁定签名算法**：不锁的话 `alg: none` 与 HS/RS 混淆是 JWT 最经典的
// 两个绕过。jwt/v5 默认已经不接受 none，这里再显式钉一次 —— 显式的东西
// 才挡得住"以后有人加一个 RS256 支持"。
func (s *Sessions) Verify(token string) (*Claims, error) {
	claims := &Claims{}
	_, err := jwt.ParseWithClaims(token, claims, func(t *jwt.Token) (any, error) {
		if t.Method.Alg() != jwt.SigningMethodHS256.Alg() {
			return nil, fmt.Errorf("拒绝签名算法 %s", t.Method.Alg())
		}
		return s.secret, nil
	}, jwt.WithValidMethods([]string{jwt.SigningMethodHS256.Alg()}))
	if err != nil {
		return nil, err
	}
	if claims.Subject == "" || claims.OrganizationID == "" {
		return nil, errors.New("会话缺 subject 或组织")
	}
	return claims, nil
}

// ---------------------------------------------------------------- API key

const KeyPrefix = "sk-"

// NewAPIKey 生成一个新 key，返回 (明文, 展示前缀, sha256)。
// 明文**只在创建时返回一次**，库里只存哈希。
func NewAPIKey() (plain, prefix, hash string) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		panic("crypto/rand 不可用：" + err.Error()) // 没有随机数就没有安全可言，不要降级
	}
	plain = KeyPrefix + base64.RawURLEncoding.EncodeToString(raw)
	// 展示用前缀：足够在列表里认出是哪一把，又不足以推出完整 key
	prefix = plain[:min(len(plain), 11)]
	return plain, prefix, HashAPIKey(plain)
}

// HashAPIKey 用 sha256 而不是 bcrypt。
//
// **每个对外请求都要验一次 key**，bcrypt 的成本函数会直接压垮代理路径
// （旧系统实测过）。key 本身是 32 字节随机串，没有字典可查、不存在弱口令，
// 所以慢哈希在这里买不到任何东西。
func HashAPIKey(plain string) string {
	sum := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}

// LooksLikeAPIKey 只做形状判断，用来决定走哪条鉴权分支。
func LooksLikeAPIKey(token string) bool {
	return strings.HasPrefix(token, KeyPrefix)
}

// ConstantTimeEqual 给服务凭据比对用。
func ConstantTimeEqual(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic("crypto/rand 不可用：" + err.Error())
	}
	return hex.EncodeToString(b)
}

// NewID 是全服务的 id 生成器：32 位十六进制，与 Python 侧 `uuid4().hex` 同形状。
// **形状必须一致**：迁移器要在两边对账，长度不同会让外键比对全部落空。
func NewID() string { return randomHex(16) }

// NewToken 生成文件凭证一类的长随机串。
func NewToken() string { return randomHex(32) }
