package api

import (
	"net/http"
	"testing"

	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/apierr"
	"github.com/Minato-Aqukin/deepdocparse/services/control-api/internal/rbac"
)

// contributor 打一串 read 进来，全局 AllScopes 不能被改写，
// 后续缺省签发不受影响。
func TestResolveKeyScopesContributorPoisonLeavesAllScopesIntact(t *testing.T) {
	before := append([]rbac.Scope(nil), rbac.AllScopes...)
	got, err := resolveKeyScopes(rbac.Contributor,
		[]string{"read", "read", "read", "read", "read", "read", "read"})
	if err != nil {
		t.Fatalf("合法 scope 被拒：%v", err)
	}
	for i := range got {
		got[i] = rbac.Scope("xxx")
	}
	for i, s := range rbac.AllScopes {
		if s != before[i] {
			t.Fatalf("AllScopes 被污染：%v", rbac.AllScopes)
		}
	}
	def, err := resolveKeyScopes(rbac.Contributor, nil)
	if err != nil {
		t.Fatalf("缺省签发被拒：%v", err)
	}
	if len(def) != len(before) {
		t.Fatalf("缺省作用域长度 = %d，应为 %d", len(def), len(before))
	}
}

// 未知 scope 字符串 400，不能走到存储。
func TestResolveKeyScopesRejectsUnknownScope(t *testing.T) {
	_, err := resolveKeyScopes(rbac.Contributor, []string{"read", "nope"})
	apiErr, ok := err.(*apierr.Error)
	if !ok {
		t.Fatalf("错误类型 = %T，应为 *apierr.Error", err)
	}
	if apiErr.Status != http.StatusBadRequest || apiErr.Code != "bad_scope" {
		t.Fatalf("状态 = %d/%q，应为 400/bad_scope", apiErr.Status, apiErr.Code)
	}
}

// viewer 发 parse 照样 403，不能绕过角色。
func TestResolveKeyScopesViewerCannotEscalate(t *testing.T) {
	_, err := resolveKeyScopes(rbac.Viewer, []string{"parse"})
	apiErr, ok := err.(*apierr.Error)
	if !ok {
		t.Fatalf("错误类型 = %T，应为 *apierr.Error", err)
	}
	if apiErr.Status != http.StatusForbidden || apiErr.Code != "scope_escalation" {
		t.Fatalf("状态 = %d/%q，应为 403/scope_escalation", apiErr.Status, apiErr.Code)
	}
}

// rate_limit <=0 直接 400。
func TestResolveKeyRateLimitRejectsNonPositive(t *testing.T) {
	for _, want := range []int{0, -1} {
		_, err := resolveKeyRateLimit(rbac.Contributor, 60, &want)
		apiErr, ok := err.(*apierr.Error)
		if !ok {
			t.Fatalf("错误类型 = %T，应为 *apierr.Error", err)
		}
		if apiErr.Status != http.StatusBadRequest || apiErr.Code != "bad_rate_limit" {
			t.Fatalf("rate=%d：状态 = %d/%q，应为 400/bad_rate_limit", want, apiErr.Status, apiErr.Code)
		}
	}
}

// 不能管理组织的成员超上限夹到 DefaultRatePerMin，能管理的放行。
func TestResolveKeyRateLimitClampsNonAdminOnly(t *testing.T) {
	over := 1000000000
	got, err := resolveKeyRateLimit(rbac.Contributor, 60, &over)
	if err != nil {
		t.Fatalf("contributor 超上限被拒而不是夹住：%v", err)
	}
	if got != 60 {
		t.Fatalf("contributor rate = %d，应夹到 60", got)
	}
	got, err = resolveKeyRateLimit(rbac.Admin, 60, &over)
	if err != nil {
		t.Fatalf("admin 超上限被拒：%v", err)
	}
	if got != over {
		t.Fatalf("admin rate = %d，应保持 %d", got, over)
	}
	got, err = resolveKeyRateLimit(rbac.Contributor, 60, nil)
	if err != nil || got != 60 {
		t.Fatalf("缺省 rate = %d/%v，应为 60/nil", got, err)
	}
}
