#!/usr/bin/env bash
# 企业边界 5 的**物理**验证：对着真库确认两个服务角色互相写不了对方的表。
#
#   scripts/check_db_boundary.sh [psql-url]        # 缺省连 dev 的 postgres 容器
#
# ## 为什么这条必须对着真库跑
#
# "一个数据对象只能有一个写入所有者"这句话，此前只由一条**静态守卫**
# （scripts/check_data_ownership.py 扫源码里的表名）保障。
# 而 2026-09-02 发现：授权 SQL 写的是"corpus schema 存在才授权"，
# 语料表却在 public —— 那段 SQL 从未执行过，control-api 还用超级用户连库。
# 也就是说**规则在数据库层面从来没有生效**，而静态守卫一直是绿的
# （它扫的是源码，不是权限）。
#
# 静态守卫管"有没有人写了越界的 SQL"，这条管"越界的 SQL 会不会被数据库拒绝"。
# 两条都要有：前者防意图，后者防疏漏。
set -uo pipefail

URL_BASE="${1:-}"
CONTAINER="${DDP_PG_CONTAINER:-ddp-postgres-1}"
DB="${DDP_PG_DB:-deepdocparse}"

FAILED=0
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s —— %s\n' "$1" "$2"; FAILED=$((FAILED + 1)); }

# 以 role 身份跑一条 SQL，回显 stderr（权限错误在那里）
run_as() {
  local role="$1" sql="$2"
  if [ -n "$URL_BASE" ]; then
    psql "${URL_BASE/\/\/*@/\/\/$role@}" -tAc "$sql" 2>&1
  else
    docker exec "$CONTAINER" psql -U "$role" -d "$DB" -tAc "$sql" 2>&1
  fi
}

# 断言这条语句**必须被拒**。注意用 ROLLBACK 包住写操作，
# 万一权限真的开着，也不要把 dev 库改坏
denied() {
  local role="$1" sql="$2" label="$3"
  local out
  out=$(run_as "$role" "BEGIN; $sql; ROLLBACK;")
  if printf '%s' "$out" | grep -q "permission denied"; then
    pass "$label"
  else
    fail "$label" "没有被拒：${out:0:160}"
  fi
}

allowed() {
  local role="$1" sql="$2" label="$3"
  local out
  out=$(run_as "$role" "$sql")
  if printf '%s' "$out" | grep -q "permission denied\|ERROR"; then
    fail "$label" "本该允许却被拒：${out:0:160}"
  else
    pass "$label"
  fi
}

echo ">>> 数据所有权边界（对着真库）"

# ---- Go 碰不到语料 ----
denied ddp_control "UPDATE documents SET filename = 'x'" "ddp_control 写不了 documents"
denied ddp_control "SELECT count(*) FROM documents"      "ddp_control 读不了 documents"
denied ddp_control "INSERT INTO chunks (id) VALUES ('x')" "ddp_control 写不了 chunks"

# ---- Python 碰不到 control 的可写表 ----
denied ddp_corpus "UPDATE control.memberships SET role = 'admin'" "ddp_corpus 写不了 memberships"
denied ddp_corpus "UPDATE control.usage_ledger SET pages = 0"     "ddp_corpus 写不了 usage_ledger"
denied ddp_corpus "UPDATE control.api_keys SET revoked_at = NULL" "ddp_corpus 写不了 api_keys"

# ---- 审计只增不改，连服务自己都不给 ----
denied ddp_control "DELETE FROM control.audit_events"             "ddp_control 删不了审计"
denied ddp_control "UPDATE control.audit_events SET action = 'x'" "ddp_control 改不了审计"

# ---- 建表权限。**这一类比读写更要紧**：能在对方的 schema 里建表，
#      就能建一张同名表把对方的查询劫持过来（search_path 一变就中招）。
#      迁移由属主 `ddp` 跑，两个服务角色都不需要 DDL ----
denied ddp_control "CREATE TABLE public.boundary_probe (id int)"  "ddp_control 不能在 public 建表"
denied ddp_corpus  "CREATE TABLE control.boundary_probe (id int)" "ddp_corpus 不能在 control 建表"

# ---- 只读那两张是**穷举**的，不是"control 随便读" ----
denied ddp_corpus "SELECT count(*) FROM control.api_keys"     "ddp_corpus 读不到 control.api_keys"
denied ddp_corpus "SELECT count(*) FROM control.audit_events" "ddp_corpus 读不到 control.audit_events"

# ---- 该给的必须真的给了。**反哨兵**：全拒也可能只是角色连不上库，
#      那样上面每一条都会"通过"，而系统其实是坏的 ----
allowed ddp_corpus  "SELECT count(*) FROM control.users"         "ddp_corpus 读得到 control.users"
allowed ddp_corpus  "SELECT count(*) FROM control.organizations" "ddp_corpus 读得到 control.organizations"
allowed ddp_corpus  "SELECT count(*) FROM documents"             "ddp_corpus 读得到 documents"
allowed ddp_control "SELECT count(*) FROM control.api_keys"      "ddp_control 读得到 control.api_keys"
allowed ddp_control "INSERT INTO control.audit_events
                       (id, organization_id, actor_kind, action, detail)
                     VALUES ('boundary-probe','org','service','probe','{}')
                     ON CONFLICT DO NOTHING"                     "ddp_control 写得进审计"

# ---- 谁在用超级用户连库。**这条比上面所有断言都靠前** ——
#      超级用户绕过全部 GRANT/REVOKE，上面每一条都对它无效。
#      F-20 修了三个长跑客户端，漏掉第四个（MCP）就是这么发现的：
#      那些针对 ddp_corpus / ddp_control 的断言全绿，而 MCP 想写什么写什么
echo ">>> 没有多余的超级用户"
SUPERS=$(run_as ddp "SELECT rolname FROM pg_roles WHERE rolsuper AND rolcanlogin
                     AND rolname NOT IN ('postgres', 'ddp') ORDER BY 1")
if [ -z "$(printf '%s' "$SUPERS" | tr -d '[:space:]')" ]; then
  pass "除属主外没有可登录的超级用户"
else
  fail "除属主外没有可登录的超级用户" "多出来：$(printf '%s' "$SUPERS" | tr '\n' ' ')"
fi

# 反哨兵：属主本身必须**是**超级用户，否则上面那条会因为查不到而恒真
if printf '%s' "$(run_as ddp "SELECT rolsuper FROM pg_roles WHERE rolname = 'ddp'")" | grep -q t; then
  pass "属主 ddp 确实是超级用户（上一条不是恒真）"
else
  fail "属主 ddp 确实是超级用户" "查不到 —— 上一条断言可能恒真"
fi

# 两个服务角色都**不许**是超级用户
for role in ddp_control ddp_corpus; do
  if printf '%s' "$(run_as ddp "SELECT rolsuper FROM pg_roles WHERE rolname = '$role'")" | grep -q t; then
    fail "$role 不是超级用户" "它是超级用户 —— 上面所有权限断言对它都无效"
  else
    pass "$role 不是超级用户"
  fi
done

if [ "$FAILED" -gt 0 ]; then
  echo "边界检查失败 $FAILED 项 —— 见 docs/refactor/DATA-OWNERSHIP.md" >&2
  exit 1
fi
echo "边界检查全部通过"
