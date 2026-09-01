// Package rbac 是角色判断的唯一一处实现。
//
// 角色**有序**：viewer < contributor < reviewer < admin，所以鉴权是一次比大小，
// 而不是一张要维护的权限矩阵。加角色 = 在中间插一个 rank，
// 所有既有的 `AtLeast` 判断自动正确。
//
// **不要在 handler 里写 `if role == "admin"`**：那种写法在加了
// "superadmin" 之类的角色之后会静默地把新角色挡在外面。
package rbac

import (
	"fmt"
	"sort"
)

type Role string

const (
	Viewer      Role = "viewer"
	Contributor Role = "contributor"
	Reviewer    Role = "reviewer"
	Admin       Role = "admin"
)

// rank 必须与 database/control/0001_control_schema.sql 里的 control.roles 一致。
// 有一条守卫（TestRankMatchesMigration）逐条比对这两处 —— 漂开的表现是
// 数据库允许的角色在代码里判成"未知"，然后所有请求 403。
var rank = map[Role]int{
	Viewer:      10,
	Contributor: 20,
	Reviewer:    30,
	Admin:       40,
}

// Scope 是一个 API key 能调的平面。
type Scope string

const (
	ScopeRead       Scope = "read"
	ScopeParse      Scope = "parse"
	ScopeChat       Scope = "chat"
	ScopeEmbeddings Scope = "embeddings"
	ScopeExtract    Scope = "extract"
	ScopeRerank     Scope = "rerank"
	ScopeMCP        Scope = "mcp"
)

// AllScopes 保持稳定顺序，供 API key 创建时的"缺省给全部"用。
var AllScopes = []Scope{ScopeRead, ScopeParse, ScopeChat, ScopeEmbeddings, ScopeExtract, ScopeRerank, ScopeMCP}

// Parse 把字符串转成 Role；未知角色**报错而不是降级成 viewer**。
// 静默降级会让一个拼错的角色名表现为"这个人突然没权限了"，
// 而排查时看到的是一个合法的 viewer。
func Parse(s string) (Role, error) {
	r := Role(s)
	if _, ok := rank[r]; !ok {
		return "", fmt.Errorf("未知角色 %q（已知：%v）", s, Known())
	}
	return r, nil
}

// Known 返回按权限从低到高排序的全部角色。
func Known() []Role {
	out := make([]Role, 0, len(rank))
	for r := range rank {
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return rank[out[i]] < rank[out[j]] })
	return out
}

// Rank 暴露给守卫用例比对迁移文件。
func Rank(r Role) (int, bool) {
	v, ok := rank[r]
	return v, ok
}

// AtLeast 报告 r 是否至少有 need 的权限。
func (r Role) AtLeast(need Role) bool {
	have, ok := rank[r]
	if !ok {
		return false // 未知角色一律无权 —— 默认拒绝
	}
	want, ok := rank[need]
	return ok && have >= want
}

// 下面几个是**能力**而不是角色，handler 里应当问能力，不要问角色名。
// 加一种能力时，这里是唯一要改的地方。

func (r Role) CanUpload() bool    { return r.AtLeast(Contributor) }
func (r Role) CanDeleteDoc() bool { return r.AtLeast(Reviewer) }
func (r Role) CanReview() bool    { return r.AtLeast(Reviewer) }
func (r Role) CanManageOrg() bool { return r.AtLeast(Admin) }
func (r Role) CanReadAudit() bool { return r.AtLeast(Admin) }
func (r Role) CanIssueKeys() bool { return r.AtLeast(Contributor) }

// DefaultScopes 是该角色新建 API key 时的缺省作用域。
// viewer 只能拿只读 key —— 否则"只读成员"可以通过发一个 key 绕过自己的角色。
func (r Role) DefaultScopes() []Scope {
	if !r.AtLeast(Contributor) {
		return []Scope{ScopeRead}
	}
	return AllScopes
}

// AllowedScopes 报告该角色能否签发含 want 的 key。
func (r Role) AllowedScopes(want []Scope) error {
	allowed := map[Scope]bool{}
	for _, s := range r.DefaultScopes() {
		allowed[s] = true
	}
	for _, s := range want {
		if !allowed[s] {
			return fmt.Errorf("角色 %s 不能签发带 %s 作用域的 key", r, s)
		}
	}
	return nil
}
