// Package config 是 control-api 的全部部署开关。
//
// 与两个 Python 服务一样的约定：**占位密钥拒绝启动**。
// 带着 change-me 跑起来的话鉴权形同虚设，而运行时不会有任何报错 ——
// 这个项目在 JWT_SECRET 与 SERVICE_TOKEN 上各踩过一次。
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	// ---- 监听 ----
	// 监听地址。容器里通常保持 :8080，对外端口由编排层映射
	Addr string

	// ---- 数据库 ----
	// control schema 的连接串。**必须用 ddp_control 角色连** —— 它对 corpus
	// 一个字都写不了，这是"一个数据对象只能有一个写入所有者"的物理保障
	// （database/control/0002_roles.sql）
	DatabaseURL string
	// 连接池上限。**这是全站雪崩的一个入口**：每个副本都会占满它，
	// 副本数 × MaxConns 必须小于 PG 的 max_connections，否则扩容会打挂数据库
	DBMaxConns int32
	// 常驻的空闲连接数。设 0 会让每个突发请求都先付一次建连的钱
	DBMinConns int32

	// ---- 会话与凭据 ----
	// 签发/校验用户会话的密钥。**占位值或短于 32 字节会拒绝启动**：
	// 它是 change-me 等于任何人都能给任意 user_id 伪造一个有效会话，且运行时零报错
	JWTSecret string
	// 登录态有效期
	JWTTTL time.Duration
	// bcrypt 成本。登录是低频操作，成本函数拖慢的是攻击者；
	// 调低于 10 基本等于没有慢哈希
	BcryptCost int
	// 注册模式：open（任何人可注册）/ invite（仅 admin 添加）/ closed（只走 OIDC）。
	// 企业部署应当是 closed 或 invite
	RegistrationMode string
	// 新成员的缺省角色。**第一个注册的用户无视这一项直接成为 admin** ——
	// 否则一个全新部署会没有任何人能管理它
	DefaultRole string

	// ---- OIDC（企业登录，可选）----
	// IdP 的 issuer。留空 = 不启用 OIDC，只用本地账号
	OIDCIssuer string
	// OIDC 客户端 ID
	OIDCClientID string
	// OIDC 客户端密钥
	OIDCClientSecret string
	// 回调地址，必须与 IdP 上登记的一致
	OIDCRedirectURL string

	// ---- 上游服务 ----
	// 语料 API（services/corpus-api）
	CorpusURL string
	// 模型网关（services/model-gateway）
	GatewayURL string
	// 语料级 MCP（services/mcp）
	MCPURL string
	// 服务间凭据。**占位值会拒绝启动**：它是内网服务面唯一的鉴权，
	// 留着 change-me 等于 /v1/* 与语料 API 无鉴权开放。
	// 生产优先 mTLS 或短期服务 token，这是最低限度的那一层
	ServiceToken string

	// ---- 对象存储 ----
	// 服务自己访问对象存储的地址（内网）
	ObjectEndpoint string
	// **浏览器可达**的地址，预签名 URL 用它签。与上面用同一个的话，
	// 容器里签出来的 URL 里带着 `minio:9000`，浏览器一访问就是 DNS 失败 ——
	// 而这只在真部署里才暴露
	ObjectPublicHost string
	// 对象存储访问密钥
	ObjectAccessKey string
	// 对象存储密钥。**默认值 minioadmin 会拒绝启动**
	ObjectSecretKey string
	// 桶名
	ObjectBucket string
	// 走 https 则置 true
	ObjectSecure bool
	// 预签名有效期。**泄露一个签名 URL 的代价与它的 TTL 成正比**，
	// 所以超过 2 小时会拒绝启动
	PresignTTL time.Duration

	// ---- 上传 ----
	// 单文件上限
	MaxUploadBytes int64
	// multipart 分片大小。**必须 >= 5MiB**（S3 兼容实现的硬性下限）——
	// 写小了会在 complete multipart 时才报错，那时文件已经传完了
	UploadPartSize int64
	// 上传会话有效期，过期未完成会被标成 expired（**不删对象**，
	// 回收交给带宽限期的 GC）
	UploadTTL time.Duration
	// 允许上传的 MIME **白名单**。空列表会拒绝启动。
	// 白名单而不是黑名单：上传 text/html 并 inline 打开就是本站同源 XSS
	AllowedMIME []string

	// ---- 限速 ----
	// 留空则退回单进程内存计数，并在启动日志里明确警告 ——
	// **多副本部署下那等于实际限速 = 配置值 × 副本数**
	RedisURL string
	// 新建 API key 的缺省限速（次/分钟）
	DefaultRatePerMin int
	// 登录/注册这类未鉴权端点的限速（次/分钟/IP）。
	// 目的不是精确公平，是让暴力破解从"几分钟"变成"几个月"
	LoginRatePerMin int
	// 问答限速（次/分钟/actor）。它会打一次 chat 模型 + 若干次视觉核对
	QARatePerMin int
	// 图谱/wiki 生成限速（次/分钟/actor）。**很贵**：一次要把几十条证据送进模型
	KnowledgeRatePerMin int
	// 批量抽取限速（次/分钟/actor）。一次 = N 个字段 × (检索 + 模型调用)
	ExtractRatePerMin int

	// ---- 其它 ----
	// 允许的浏览器来源。**不要用 `*`**：配合 credentials 时浏览器会直接拒绝，
	// 而且那等于放弃同源保护
	CORSOrigins []string
	// 本服务对外可达的地址，拼稳定文件 URL 用
	PublicBaseURL string
	// 显式跳过占位密钥检查。**只给一次性容器与 CI 用** ——
	// 逃生口必须显式且留痕
	AllowInsecureDefaults bool
	// outbox 投递间隔。调大会让"上传完到文档出现"的延迟变长
	OutboxInterval time.Duration
}

const placeholder = "change-me"

func Load() (*Config, error) {
	c := &Config{
		Addr:                  env("CONTROL_ADDR", ":8080"),
		DatabaseURL:           env("CONTROL_DATABASE_URL", "postgres://ddp_control:ddp@127.0.0.1:15432/deepdocparse"),
		DBMaxConns:            int32(envInt("CONTROL_DB_MAX_CONNS", 20)),
		DBMinConns:            int32(envInt("CONTROL_DB_MIN_CONNS", 2)),
		JWTSecret:             env("JWT_SECRET", placeholder),
		JWTTTL:                time.Duration(envInt("JWT_TTL_MINUTES", 60*24*7)) * time.Minute,
		BcryptCost:            envInt("BCRYPT_COST", 12),
		RegistrationMode:      env("REGISTRATION_MODE", "open"),
		DefaultRole:           env("DEFAULT_MEMBER_ROLE", "contributor"),
		OIDCIssuer:            env("OIDC_ISSUER", ""),
		OIDCClientID:          env("OIDC_CLIENT_ID", ""),
		OIDCClientSecret:      env("OIDC_CLIENT_SECRET", ""),
		OIDCRedirectURL:       env("OIDC_REDIRECT_URL", ""),
		CorpusURL:             env("CORPUS_URL", "http://127.0.0.1:8081"),
		GatewayURL:            env("GATEWAY_URL", "http://127.0.0.1:9000"),
		MCPURL:                env("MCP_URL", "http://127.0.0.1:9100"),
		ServiceToken:          env("SERVICE_TOKEN", placeholder),
		ObjectEndpoint:        env("OBJECT_ENDPOINT", "127.0.0.1:19000"),
		ObjectPublicHost:      env("OBJECT_PUBLIC_ENDPOINT", "127.0.0.1:19000"),
		ObjectAccessKey:       env("OBJECT_ACCESS_KEY", "minioadmin"),
		ObjectSecretKey:       env("OBJECT_SECRET_KEY", "minioadmin"),
		ObjectBucket:          env("OBJECT_BUCKET", "deepdocparse"),
		ObjectSecure:          envBool("OBJECT_SECURE", false),
		PresignTTL:            time.Duration(envInt("PRESIGN_TTL_SECONDS", 900)) * time.Second,
		MaxUploadBytes:        int64(envInt("MAX_UPLOAD_BYTES", 200*1024*1024)),
		UploadPartSize:        int64(envInt("UPLOAD_PART_SIZE", 16*1024*1024)),
		UploadTTL:             time.Duration(envInt("UPLOAD_TTL_SECONDS", 24*3600)) * time.Second,
		AllowedMIME:           envList("ALLOWED_UPLOAD_MIME", "application/pdf"),
		RedisURL:              env("REDIS_URL", ""),
		DefaultRatePerMin:     envInt("DEFAULT_RATE_LIMIT_PER_MIN", 60),
		LoginRatePerMin:       envInt("LOGIN_RATE_LIMIT_PER_MIN", 10),
		QARatePerMin:          envInt("QA_RATE_PER_MIN", 20),
		KnowledgeRatePerMin:   envInt("KNOWLEDGE_RATE_PER_MIN", 2),
		ExtractRatePerMin:     envInt("EXTRACT_RATE_PER_MIN", 6),
		CORSOrigins:           envList("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"),
		PublicBaseURL:         env("PUBLIC_BASE_URL", "http://127.0.0.1:8080"),
		AllowInsecureDefaults: envBool("ALLOW_INSECURE_DEFAULTS", false),
		OutboxInterval:        time.Duration(envInt("OUTBOX_INTERVAL_SECONDS", 2)) * time.Second,
	}
	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) validate() error {
	var problems []string

	// 占位密钥。CI / 一次性容器可以用 ALLOW_INSECURE_DEFAULTS 显式跳过 ——
	// 逃生口必须显式且留痕，不能靠"本地就先这样吧"
	if !c.AllowInsecureDefaults {
		if c.JWTSecret == placeholder || len(c.JWTSecret) < 32 {
			problems = append(problems,
				"JWT_SECRET 还是占位值或短于 32 字节：任何人都能给任意 user_id 伪造会话，且运行时零报错")
		}
		if c.ServiceToken == placeholder || len(c.ServiceToken) < 16 {
			problems = append(problems,
				"SERVICE_TOKEN 还是占位值：内网服务面等于无鉴权开放")
		}
		if c.ObjectSecretKey == "minioadmin" {
			problems = append(problems, "OBJECT_SECRET_KEY 还是默认值")
		}
	}
	switch c.RegistrationMode {
	case "open", "invite", "closed":
	default:
		problems = append(problems, "REGISTRATION_MODE 只认 open / invite / closed，收到 "+c.RegistrationMode)
	}
	// 上传分片下限：S3 兼容实现要求非最后一片 >= 5MiB，写小了会在
	// complete multipart 时才报错 —— 那时文件已经传完了
	if c.UploadPartSize < 5*1024*1024 {
		problems = append(problems, "UPLOAD_PART_SIZE 必须 >= 5MiB（S3 兼容实现的硬性下限）")
	}
	if c.PresignTTL > 2*time.Hour {
		problems = append(problems, "PRESIGN_TTL_SECONDS 超过 2 小时：签名 URL 泄露的代价与 TTL 成正比")
	}
	if len(c.AllowedMIME) == 0 {
		problems = append(problems, "ALLOWED_UPLOAD_MIME 是空的 —— 空白名单等于禁止一切上传")
	}
	if len(problems) > 0 {
		return errors.New("配置不可用：\n  - " + strings.Join(problems, "\n  - "))
	}
	if c.AllowInsecureDefaults {
		fmt.Fprintln(os.Stderr, "[config] WARNING: ALLOW_INSECURE_DEFAULTS 已开启，占位密钥检查被跳过")
	}
	return nil
}

// MIMEAllowed 报告上传的 MIME 在不在白名单里。
// **白名单而不是黑名单**：上传 text/html 就是本站同源 XSS。
func (c *Config) MIMEAllowed(mime string) bool {
	mime = strings.ToLower(strings.TrimSpace(strings.SplitN(mime, ";", 2)[0]))
	for _, allowed := range c.AllowedMIME {
		if mime == strings.ToLower(strings.TrimSpace(allowed)) {
			return true
		}
	}
	return false
}

func env(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return fallback
}

func envList(key, fallback string) []string {
	raw := env(key, fallback)
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if p := strings.TrimSpace(part); p != "" {
			out = append(out, p)
		}
	}
	return out
}
