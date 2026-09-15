package api

import (
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/proxy"
)

// P5 协调者入口前缀必须被转发，且沿用入口的身份剥离/注入边界。
//
// corpusPrefixes 漏一行的失败模式是 404（不是转发到错的地方）—— 而那个
// 404 与"corpus-api 根本没实现这个端点"在用户眼里一模一样。所以这里逐条
// 打真实会话请求，确认它们**到了语料 API**、带着 control-api 注入的 actor 头，
// 而客户端伪造的同类头一个都没活下来。
func TestCoordinatorPrefixesForwardAuthenticatedActorAndStripForgedIdentity(t *testing.T) {
	f := discoveryPGFixture(t)
	type forwarded struct {
		path, authorization string
		actor, actorKind    string
		organization, role  string
		user                string
	}
	var mu sync.Mutex
	var calls []forwarded
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		calls = append(calls, forwarded{
			path:          r.URL.Path,
			authorization: r.Header.Get("Authorization"),
			actor:         r.Header.Get(identity.HeaderActor),
			actorKind:     r.Header.Get(identity.HeaderActorKind),
			organization:  r.Header.Get(identity.HeaderOrganization),
			role:          r.Header.Get(identity.HeaderRole),
			user:          r.Header.Get(identity.HeaderUser),
		})
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer target.Close()
	f.server.cfg.CorpusURL = target.URL
	f.server.corpus, _ = proxy.New("corpus", target.URL, f.server.cfg.ServiceToken)

	paths := []struct{ method, path string }{
		{http.MethodPost, "/api/v1/task-intents"},
		{http.MethodPost, "/api/v1/task-plans"},
		{http.MethodPost, "/api/v1/task-plans/root-1/approve"},
		{http.MethodPost, "/api/v1/tasks"},
		{http.MethodGet, "/api/v1/tasks"},
		{http.MethodGet, "/api/v1/tasks/root-1"},
		{http.MethodGet, "/api/v1/tasks/root-1/coverage"},
		{http.MethodGet, "/api/v1/tasks/root-1/events"},
		{http.MethodPost, "/api/v1/tasks/root-1/resume"},
		{http.MethodPost, "/api/v1/tasks/root-1/cancel"},
		{http.MethodPost, "/api/v1/deliveries/delivery-1/ack"},
	}
	for _, item := range paths {
		// requestDiscovery 会在每个请求上伪造身份头 —— 正是要剥离的那组。
		w := requestDiscovery(t, f.handler, item.method, item.path, f.aliceToken,
			map[string]any{})
		if w.Code != http.StatusOK {
			t.Fatalf("%s %s 没有转发给语料 API：%d %s",
				item.method, item.path, w.Code, w.Body.String())
		}
	}
	if len(calls) != len(paths) {
		t.Fatalf("语料 API 只收到 %d/%d 条协调者请求", len(calls), len(paths))
	}
	for i, call := range calls {
		if call.path != paths[i].path {
			t.Fatalf("第 %d 条转到了 %s，期望 %s", i, call.path, paths[i].path)
		}
		if call.authorization != "Bearer "+f.server.cfg.ServiceToken {
			t.Fatalf("%s 丢了服务凭据：%q", call.path, call.authorization)
		}
		if call.actor != f.alice.ID || call.actorKind != "user" ||
			call.organization != f.org || call.role != "contributor" ||
			call.user != f.alice.ID {
			t.Fatalf("%s 的 actor 头不是入口注入的真实身份：%+v", call.path, call)
		}
	}
	// 未登记前缀保持 404 —— 失败模式是"没转发"，不是"转到了别的地方"。
	miss := requestDiscovery(t, f.handler, http.MethodGet, "/api/v1/deliveries-unknown/x",
		f.aliceToken, nil)
	if miss.Code != http.StatusNotFound {
		t.Fatalf("未登记前缀被转发或换了错误：%d", miss.Code)
	}
}
