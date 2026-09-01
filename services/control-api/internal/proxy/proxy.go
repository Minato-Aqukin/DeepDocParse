// Package proxy 是控制面到上游服务的反向代理。
//
// # 为什么不用 httputil.NewSingleHostReverseProxy 的默认配置
//
// 默认配置会**缓冲响应体**，而这条路径上跑着 SSE 流式问答：
// 缓冲的表现不是报错，是"问答一直没反应"，直到整个回答生成完才一次性出现。
// 风险台账里「代理破坏 SSE/取消 -> 计量错、任务泄漏」说的就是它。
//
// 所以这里显式做了三件事：
//  1. `FlushInterval = -1`（每次写立即 flush）
//  2. 不设 ResponseHeaderTimeout 的上限到流式路径（长连接是正常的）
//  3. 客户端断开时**取消上游请求**，否则上游会继续跑完整个生成、
//     白烧 GPU，而计量已经记不到人头上了
package proxy

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
)

// Upstream 是一个可代理的上游。
type Upstream struct {
	Name   string
	Target *url.URL
	proxy  *httputil.ReverseProxy
	// 服务凭据。生产优先 mTLS 或短期服务 token；这是最低限度的那一层
	token string
	// 只给守卫用例读。**行为测试测不出 Proxy 是不是 nil** ——
	// Go 的 httpproxy 对 loopback 地址一律绕过代理，而单测只能用 loopback，
	// 所以那条只能做成结构断言。理由写在 proxy_test.go 里
	transport *http.Transport
}

// Transport 暴露给守卫用例。生产代码不要用它。
func (u *Upstream) Transport() *http.Transport { return u.transport }

// 逐跳头：**必须过滤**。把 Connection / Upgrade 这类头原样转发给上游，
// 轻则上游困惑，重则连接复用出错。RFC 7230 §6.1。
var hopByHop = []string{
	"Connection", "Proxy-Connection", "Keep-Alive", "Proxy-Authenticate",
	"Proxy-Authorization", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

func New(name, target, token string) (*Upstream, error) {
	u, err := url.Parse(target)
	if err != nil {
		return nil, err
	}
	up := &Upstream{Name: name, Target: u, token: token}

	transport := &http.Transport{
		Proxy: nil, // **不读环境代理变量**：带 SOCKS/HTTP 代理的机器会把
		// http://127.0.0.1:9000 这种内网调用也塞进代理，表现是**卡住而不是报错**。
		// 三个 Python 服务的 httpx 都写着 trust_env=False，这里是同一条
		DialContext: (&net.Dialer{
			Timeout:   5 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		MaxIdleConns:        200,
		MaxIdleConnsPerHost: 50,
		IdleConnTimeout:     90 * time.Second,
		// **不设 ResponseHeaderTimeout**：解析任务的首字节可能要等很久，
		// 而超时会让它表现为 502 而不是"还在跑"
		ExpectContinueTimeout: time.Second,
		ForceAttemptHTTP2:     true,
	}

	up.transport = transport
	up.proxy = &httputil.ReverseProxy{
		Transport: transport,
		// -1 = 每次 Write 立即 flush。SSE 的命门
		FlushInterval: -1,
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(u)
			// SetXForwarded 会设 X-Forwarded-For/Proto/Host —— 上游据此拼绝对 URL
			pr.SetXForwarded()
			for _, h := range hopByHop {
				pr.Out.Header.Del(h)
			}
			// 服务凭据换上我们自己的：客户端的 Authorization 到此为止，
			// **绝不能透传给上游** —— 上游不认识用户 key，透传只会让它把
			// 一个 sk- 当成 service token 去比对
			pr.Out.Header.Set("Authorization", "Bearer "+up.token)
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// 客户端主动断开不是错误 —— 记成 502 会把 SSE 的正常结束
			// 变成一片假告警
			if errors.Is(err, context.Canceled) {
				slog.DebugContext(r.Context(), "client aborted", "upstream", name)
				return
			}
			slog.ErrorContext(r.Context(), "upstream unreachable",
				"upstream", name, "path", r.URL.Path, "err", err)
			apierr.Write(w, r, apierr.New(http.StatusBadGateway,
				apierr.TypeUpstream, "upstream_unreachable",
				"上游服务 "+name+" 不可达").WithCause(err))
		},
	}
	return up, nil
}

// ServeHTTP 转发一次请求，带上 actor 上下文。
//
// prefixTrim 非空时会从路径前缀里去掉它（例如 `/mcp` -> `/`）。
func (u *Upstream) ServeHTTP(w http.ResponseWriter, r *http.Request, prefixTrim string) {
	actor := identity.From(r.Context())
	if actor == nil {
		// 没有 actor 就转发，等于把鉴权中间件漏挂当成"匿名也行"。
		// 宁可 500 —— 这是配置错，必须在测试环境就炸出来
		apierr.Write(w, r, apierr.Internal("proxy 上缺少 actor 上下文（鉴权中间件没挂）"))
		return
	}
	if prefixTrim != "" && strings.HasPrefix(r.URL.Path, prefixTrim) {
		trimmed := strings.TrimPrefix(r.URL.Path, prefixTrim)
		if trimmed == "" {
			trimmed = "/"
		}
		r.URL.Path = trimmed
	}
	actor.Apply(r, "control-api")
	u.proxy.ServeHTTP(w, r)
}
