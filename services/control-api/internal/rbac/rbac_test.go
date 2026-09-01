package rbac_test

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// TestRankMatchesMigration：代码里的 rank 与迁移文件里的 control.roles 必须一致。
//
// 漂开的表现是**数据库允许的角色在代码里判成"未知"，然后所有请求 403** ——
// 而 403 会被当成"权限配错了"去查，查不到根因。
func TestRankMatchesMigration(t *testing.T) {
	path := filepath.Join("..", "migrate", "sql", "0001_control_schema.sql")
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读不到迁移文件 %s：%v", path, err)
	}
	re := regexp.MustCompile(`\('(\w+)',\s*(\d+),`)
	matches := re.FindAllStringSubmatch(string(body), -1)
	if len(matches) < 4 {
		// 反哨兵：正则没匹配到就等于这条守卫恒真
		t.Fatalf("从迁移文件里只解析出 %d 条角色，正则可能失效了", len(matches))
	}
	for _, m := range matches {
		want, _ := strconv.Atoi(m[2])
		got, ok := rbac.Rank(rbac.Role(m[1]))
		if !ok {
			t.Fatalf("迁移里有角色 %s，代码里没有", m[1])
		}
		if got != want {
			t.Fatalf("角色 %s 的 rank 不一致：代码 %d，迁移 %d", m[1], got, want)
		}
	}
	if len(rbac.Known()) != len(matches) {
		t.Fatalf("角色数量不一致：代码 %d，迁移 %d", len(rbac.Known()), len(matches))
	}
}

func TestAtLeastIsOrdered(t *testing.T) {
	if !rbac.Admin.AtLeast(rbac.Viewer) {
		t.Fatal("admin 应当至少有 viewer 的权限")
	}
	if rbac.Viewer.AtLeast(rbac.Admin) {
		t.Fatal("viewer 不该有 admin 的权限")
	}
}

// TestUnknownRoleDeniesEverything：未知角色必须默认拒绝。
// 静默降级成 viewer 会让一个拼错的角色名表现为"这个人突然没权限了"，
// 而排查时看到的是一个合法的 viewer。
func TestUnknownRoleDeniesEverything(t *testing.T) {
	bogus := rbac.Role("superadmin")
	if bogus.AtLeast(rbac.Viewer) || bogus.CanUpload() || bogus.CanManageOrg() {
		t.Fatal("未知角色被放行了 —— 默认必须拒绝")
	}
	if _, err := rbac.Parse("superadmin"); err == nil {
		t.Fatal("Parse 应当拒绝未知角色而不是降级")
	}
}

// TestViewerCannotEscalateViaAPIKey：viewer 不能发一把能上传的 key，
// 否则"只读成员"可以通过签发 key 绕过自己的角色。
func TestViewerCannotEscalateViaAPIKey(t *testing.T) {
	if err := rbac.Viewer.AllowedScopes([]rbac.Scope{rbac.ScopeParse}); err == nil {
		t.Fatal("viewer 签发了带 parse 作用域的 key")
	}
	if err := rbac.Contributor.AllowedScopes([]rbac.Scope{rbac.ScopeParse}); err != nil {
		t.Fatalf("contributor 应当可以签发 parse key：%v", err)
	}
	scopes := rbac.Viewer.DefaultScopes()
	if len(scopes) != 1 || scopes[0] != rbac.ScopeRead {
		t.Fatalf("viewer 的缺省作用域应当只有 read，得到 %v", scopes)
	}
}
