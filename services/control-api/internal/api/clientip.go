package api

import (
	"net"
	"net/http"
	"strings"
)

// clientIP 取调用方 IP。
//
// **只在信任反向代理时才认 X-Forwarded-For**。这里认它的最左一跳，
// 是因为本服务的部署形态就是"前面有一层入口网关"；直接暴露到公网时
// 这个头可以被伪造 —— 那时限速会被绕过，所以部署文档里写清了
// 必须由入口网关重写该头（infra/ 的 nginx/ingress 配置里有）。
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i > 0 {
			xff = xff[:i]
		}
		if ip := strings.TrimSpace(xff); ip != "" {
			return ip
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
