package api

import (
	"net/http"
	"testing"
	"time"
)

// TestNoProxyEnvReads：铁律 8 —— OIDC 与内网投递的 Transport
// 必须 Proxy:nil（不读 HTTP(S)_PROXY），否则带代理的机器上内网调用
// 被塞进代理，表现是卡住而不是报错。
// 行为测试做不了（loopback 上代理测不出阻塞），故做结构断言。
func TestNoProxyEnvReads(t *testing.T) {
	oidc := oidcHTTPClient(15 * time.Second)
	tr, ok := oidc.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("oidc client transport 不是 *http.Transport：%T", oidc.Transport)
	}
	if tr.Proxy != nil {
		t.Fatal("oidcHTTPClient 读代理环境变量（铁律 8）")
	}
	if oidc.Timeout <= 0 {
		t.Fatal("oidcHTTPClient 没有超时")
	}
	bg := proxyTransport()
	if bg.Proxy != nil {
		t.Fatal("proxyTransport 读代理环境变量（铁律 8）")
	}
}
