package api

import (
	"net"
	"net/http"
	"strings"
)

// clientIPVia 取调用方 IP。
//
// **只在信任反向代理时才认 X-Forwarded-For**：RemoteAddr 对端必须在
// config.TrustedProxies 里（缺省回环加私有网段，见 config），否则直接用 RemoteAddr。
// 不设条件地认最左一跳等于让攻击者按请求轮换 IP，登录/注册限速形同虚设。
// 这里认最左一跳，是因为部署形态是"前面有一层入口网关"；直接暴露到公网时
// 这个头可以被伪造 —— 那时限速会被绕过，所以部署文档里写清了
// 必须由入口网关重写该头（infra/ 的 nginx/ingress 配置里有）。
// limitByIP 是唯一生产调用方。
func clientIPVia(r *http.Request, trusted interface {
	Contains(net.IP) bool
},
) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	host = strings.TrimSpace(strings.Trim(host, "[]"))
	if trusted.Contains(net.ParseIP(host)) {
		if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
			if i := strings.IndexByte(xff, ','); i > 0 {
				xff = xff[:i]
			}
			if ip := strings.TrimSpace(xff); ip != "" {
				return ip
			}
		}
	}
	if h2, _, err2 := net.SplitHostPort(r.RemoteAddr); err2 == nil {
		return h2
	}
	return r.RemoteAddr
}
