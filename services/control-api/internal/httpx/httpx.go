// Package httpx 是 HTTP 层的公共件：JSON 读写、请求 ID、CORS、恢复、
// 以及**剥掉客户端伪造的内部头**。
//
// 路由用标准库 `net/http.ServeMux`（Go 1.22 起支持 `METHOD /path/{id}` 模式），
// 不引入路由框架 —— 控制面的路由数量有限，而每多一个框架就多一层
// "它到底怎么匹配的"。
package httpx

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/auth"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

// Handler 是可以返回错误的 handler —— 错误统一由 Wrap 写成契约错误体。
// 标准库的签名逼着每个分支自己 `http.Error` + `return`，漏一个 return
// 就会在写完响应后继续执行（这是 Go HTTP 代码最经典的 bug）。
type Handler func(http.ResponseWriter, *http.Request) error

func Wrap(h Handler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := h(w, r); err != nil {
			apierr.Write(w, r, err)
		}
	}
}

// JSON 写一个 200 之外可指定状态码的 JSON 响应。
func JSON(w http.ResponseWriter, status int, body any) error {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	return json.NewEncoder(w).Encode(body)
}

// DecodeJSON 读请求体。
//
// **必须限长**：不限的话一个 2GB 的 JSON body 就能把进程打死，
// 而这条路径上没有任何东西会替你拦（不变式 6 的一个小分支）。
// 也拒绝未知字段：多写一个字段却被静默忽略，是配置类 bug 的常见来源。
func DecodeJSON(r *http.Request, dst any) error {
	const maxBody = 1 << 20 // 1MiB —— 控制面的请求体都是小 JSON，文件走直传
	dec := json.NewDecoder(io.LimitReader(r.Body, maxBody))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return apierr.BadRequest("invalid_json", "请求体不是合法 JSON 或含未知字段").WithCause(err)
	}
	return nil
}

// ---------------------------------------------------------------- 中间件

type Middleware func(http.Handler) http.Handler

func Chain(h http.Handler, mws ...Middleware) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// RequestID 给每个请求一个 ID 并回写响应头。
// 客户端传来的 X-Request-Id **会被采纳**（方便端到端串联），但会被限长与过滤 ——
// 它会进日志，而日志注入是真实存在的攻击面。
func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := sanitizeID(r.Header.Get(identity.HeaderRequestID))
		if id == "" {
			id = auth.NewID()
		}
		r.Header.Set(identity.HeaderRequestID, id)
		w.Header().Set(identity.HeaderRequestID, id)
		next.ServeHTTP(w, r)
	})
}

func sanitizeID(s string) string {
	if len(s) > 64 {
		s = s[:64]
	}
	var b strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') ||
			r == '-' || r == '_' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// StripInboundIdentity 无条件删掉客户端传来的内部头。
//
// **这是整个服务最重要的一行防护。** 少了它，任何人都能发一个
// `X-DDP-Role: admin` 或 `X-DDP-Organization: <别人的组织>`，
// 而上游会完全正常地接受 —— 没有报错、没有异常日志，就是越权了。
// `TestClientCannotForgeIdentity` 钉着它。
func StripInboundIdentity(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for _, h := range identity.Inbound {
			r.Header.Del(h)
		}
		next.ServeHTTP(w, r)
	})
}

// Recover 把 panic 变成 500 而不是断开连接。
func Recover(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				apierr.Write(w, r, apierr.Internal("internal server error"))
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// CORS 按白名单放行。
// **不用 `*`**：配合 credentials 时浏览器会直接拒绝，而且那等于放弃同源保护。
func CORS(origins []string) Middleware {
	allowed := map[string]bool{}
	for _, o := range origins {
		allowed[strings.TrimSpace(o)] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if origin != "" && allowed[origin] {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				w.Header().Set("Access-Control-Allow-Credentials", "true")
				w.Header().Set("Vary", "Origin")
				w.Header().Set("Access-Control-Allow-Headers",
					"Authorization, Content-Type, "+identity.HeaderRequestID+", "+identity.HeaderIdempotency)
				w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
			}
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// SecurityHeaders 是几条与内容无关的通用防护。
func SecurityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 上传的原文件是不可信输入 —— 浏览器不许自己猜类型
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		next.ServeHTTP(w, r)
	})
}

// StatusRecorder 让日志与 metrics 拿得到状态码。
type StatusRecorder struct {
	http.ResponseWriter
	Status int
	Bytes  int64
}

func (s *StatusRecorder) WriteHeader(code int) {
	s.Status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *StatusRecorder) Write(b []byte) (int, error) {
	if s.Status == 0 {
		s.Status = http.StatusOK
	}
	n, err := s.ResponseWriter.Write(b)
	s.Bytes += int64(n)
	return n, err
}

// Flush 必须透传：**少了它 SSE 会整段缓冲住**，
// 客户端要等到响应结束才一次性收到全部事件 —— 表现为"问答一直没反应"，
// 而不是任何形式的报错。
func (s *StatusRecorder) Flush() {
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Unwrap 让 http.ResponseController 能拿到底层 writer
// （设置写超时、劫持连接都要它）。
func (s *StatusRecorder) Unwrap() http.ResponseWriter { return s.ResponseWriter }

// Elapsed 是给日志用的计时器。
func Elapsed(start time.Time) float64 { return time.Since(start).Seconds() }
