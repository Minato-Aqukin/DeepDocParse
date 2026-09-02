package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/identity"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// corpus 侧的 `current_actor` 要求这四个头**一个都不能少**（缺任何一个 401），
// 而且这是故意的：缺头给默认值的话，"入口挂错中间件"会表现为
// "这个人突然只读了"，而不是一个能一眼看出的鉴权失败。
//
// 2026-09-02 第一次真起全栈时，outbox 投递只发了其中两个 —— 于是
// **每一条 DocumentSubmitted 永远投不出去**：投递器忠实重试、如实记
// "corpus-api 返回 401"、`/readyz` 也如实报 outbox stale。
// 一切都"正确地"坏着，而产品主链路（上传完 → 文档入库）一次都没通过。
//
// 单测碰不到它：corpus 那边的测试直接调消费函数，不经过 HTTP 头这一层；
// Go 这边当时没有任何测试。所以这条守卫钉在**发送侧**。
var requiredActorHeaders = []string{
	identity.HeaderOrganization,
	identity.HeaderActor,
	identity.HeaderActorKind,
	identity.HeaderRole,
}

func TestServiceActorSendsEveryHeaderCorpusRequires(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "http://corpus-api:8081/internal/events", nil)
	(&identity.Actor{
		Kind:           identity.KindService,
		ID:             "control-api",
		OrganizationID: "org-1",
		Role:           rbac.Admin,
	}).Apply(req, "control-api")

	for _, h := range requiredActorHeaders {
		if req.Header.Get(h) == "" {
			t.Errorf("缺少 %s —— corpus 会 401，而事件将永远投不出去", h)
		}
	}
	if got := req.Header.Get(identity.HeaderActorKind); got != "service" {
		t.Errorf("actor kind = %q，应为 service（/internal/* 只收服务身份）", got)
	}
	// role 必须是契约里的合法值，否则 corpus 回 403 unknown_role
	if role := contracts.Role(req.Header.Get(identity.HeaderRole)); !role.Valid() {
		t.Errorf("role = %q 不是已知角色", role)
	}
}
