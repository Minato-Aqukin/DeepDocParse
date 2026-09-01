package api

import "testing"

// TestSafeRedirectBlocksOpenRedirect：开放重定向是钓鱼的标准入口 ——
// 用户带着刚签发的有效会话被跳到攻击者的域名。
func TestSafeRedirectBlocksOpenRedirect(t *testing.T) {
	bad := []string{
		"https://evil.example/steal",
		"//evil.example/steal",
		"http://evil.example",
		"javascript:alert(1)",
		"",
		"evil.example/path",
	}
	for _, in := range bad {
		if got := safeRedirect(in); got != "/" {
			t.Fatalf("safeRedirect(%q) = %q，应当退回 /", in, got)
		}
	}
	for _, in := range []string{"/", "/documents", "/documents?id=1"} {
		if got := safeRedirect(in); got != in {
			t.Fatalf("safeRedirect(%q) = %q，站内路径应当原样保留", in, got)
		}
	}
}

// TestIntQueryClamps：limit=99999 的意图是"给我尽量多"，
// 但**必须夹**在上限内 —— 不夹的话它是一个 DoS 入口。
func TestIntQueryClamps(t *testing.T) {
	r := newRequest("/api/audit?limit=99999")
	if got := intQuery(r, "limit", 100, 1, 1000); got != 1000 {
		t.Fatalf("limit 没被夹到上限：%d", got)
	}
	r = newRequest("/api/audit?limit=-5")
	if got := intQuery(r, "limit", 100, 1, 1000); got != 1 {
		t.Fatalf("limit 没被夹到下限：%d", got)
	}
	r = newRequest("/api/audit?limit=notanumber")
	if got := intQuery(r, "limit", 100, 1, 1000); got != 100 {
		t.Fatalf("非法值应当退回默认：%d", got)
	}
}
