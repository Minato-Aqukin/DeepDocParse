package rbac

import (
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/contracts"
)

// TestRankCoversContractRoles 钉住"契约里有几个角色，这里就得有几个 rank"。
//
// 少一个的后果不是编译错误 —— `rank[Role("superadmin")]` 取到零值 0，
// 于是那个角色比 viewer 还低，**它的人被挡在所有东西外面**，而日志里
// 只有一串 403。多一个的后果是代码里认识一个数据库不认识的角色。
//
// 变异确认过：从 rank 里删掉 Reviewer 这条会立刻红。
func TestRankCoversContractRoles(t *testing.T) {
	if len(contracts.RoleValues) == 0 {
		t.Fatal("契约里一个角色都没有 —— 生成物坏了，这条断言会恒真")
	}
	for _, role := range contracts.RoleValues {
		if _, ok := rank[Role(role)]; !ok {
			t.Errorf("契约里的角色 %q 在 rbac.rank 里没有排位", role)
		}
	}
	for role := range rank {
		if !contracts.Role(role).Valid() {
			t.Errorf("rbac.rank 里的 %q 不在契约里 —— 数据库不会接受它", role)
		}
	}
}

// TestRolesAreNotRewrittenLocally 确认四个常量确实来自契约。
//
// 直接比值就够了：有人把 `Role(contracts.RoleAdmin)` 改回 `"admin"` 字面量时
// 这条仍然绿 —— 所以它**不是**用来防手抄的（防手抄靠上面那条覆盖检查，
// 契约改名时手抄的那份会立刻缺排位）。这条防的是**抄错字**。
func TestRolesAreNotRewrittenLocally(t *testing.T) {
	for _, pair := range []struct {
		got  Role
		want contracts.Role
	}{
		{Viewer, contracts.RoleViewer},
		{Contributor, contracts.RoleContributor},
		{Reviewer, contracts.RoleReviewer},
		{Admin, contracts.RoleAdmin},
	} {
		if string(pair.got) != string(pair.want) {
			t.Errorf("角色常量与契约不符：%q != %q", pair.got, pair.want)
		}
	}
}
