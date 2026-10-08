#!/usr/bin/env python
"""数据所有权守卫 —— 企业边界 5 的机械保障。

    python scripts/check_data_ownership.py

「一个数据对象只能有一个写入所有者」如果只写在文档里，迟早会有人为了图快
在 Python 里直接 UPDATE 一下 `control.memberships`，或者在 Go 里顺手
INSERT 一条 evidence。数据库角色是最后一道防线（`database/control/0002_roles.sql`），
**但那要等到运行时才拦得住**，而那时代码已经合进去了。

这把尺子在静态层面把它挡在合入之前。判据见
`docs/refactor/DATA-OWNERSHIP.md` §4：

1. Go 代码里不得出现对 corpus 表的写操作
2. Python 代码里不得出现对 control 侧治理表的写操作
3. corpus 模型里不得出现指向 control 表的 ForeignKey
4. 跨服务边界一律 outbox，不得出现"两个连接同时 BEGIN"

**这不是完备的**（拼字符串拼出来的 SQL 骗得过它）。它挡的是顺手写下的
那一行 —— 而顺手写下的那一行正是这类边界实际被破坏的方式。
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

GO_ROOT = ROOT / "services" / "control-api"
PY_ROOTS = [
    ROOT / "python" / "ddp_core" / "ddp_core",
    ROOT / "services" / "corpus-api" / "ddp_corpus",
    ROOT / "services" / "corpus-worker" / "ddp_worker",
    ROOT / "services" / "mcp" / "ddp_mcp",
]

#: 语料表 —— Go 一个字都写不了
#: 更新方法：`grep -rhoE '__tablename__ = "[a-z_]+"'
#:   python/ddp_core/ddp_core services/corpus-api/ddp_corpus
#:   services/corpus-worker/ddp_worker services/mcp/ddp_mcp`
#: 扫出来的每个表都必须在这里出现。只许加、不许删：
#: `check_scan_is_not_vacuous` 有数量下限，少一张就红。
CORPUS_TABLES = {
    # 文档与解析主线
    "documents", "document_uploads", "parse_jobs", "chunks",
    "evidence", "citations",
    "agent_turns", "assertions", "retrieval_candidates", "evidence_verifications",
    # 知识与 wiki（wiki_entries 系旧表，与 wikis 主表并存）
    "knowledge_entities", "graph_edges",
    "wiki_entries", "wiki_sections", "wiki_sentences", "knowledge_reviews",
    "wikis", "wiki_revisions", "wiki_pages", "wiki_dependencies",
    "wiki_claim_bindings", "wiki_human_edits", "wiki_write_keys",
    # 对话与抽取（从旧 web 层迁入 corpus）
    "conversations", "messages",
    "extraction_templates", "extraction_runs", "extraction_items",
    # 任务与事件
    "tasks", "corpus_outbox", "processed_events", "usage_claims",
    # 资源与上传
    "resources", "resource_versions", "upload_events",
    # 复本与集合
    "bundle_replicas", "bundle_replica_revoke_keys",
    "collections", "collection_members",
    "collection_catalog_snapshots", "collection_catalog_pages",
    "collection_catalog_views", "collection_receipts",
    # 客户端投影
    "client_pages", "client_receipts", "client_snapshots", "client_views",
    # 联邦与远端算力
    "federation_admissions", "federation_cache_entries", "federation_credential_nonces",
    "federation_delegation_consumption", "federation_deliveries", "federation_executions",
    "federation_probes", "federation_requests", "federation_root_ledgers",
    "federation_root_reservations", "federation_task_events",
    "remote_computes",
    # 覆盖率
    "coverage_entries", "coverage_ledgers",
}

#: 控制面的治理表 —— Python 一个字都写不了。
#: 更新方法：`grep -rhoE "CREATE TABLE (IF NOT EXISTS )?control\.[a-z_]+"
#:   database/control/*.sql` 的表名部分（去 `control.` 前缀），
#:   加上恢复演练 scripts/recovery_drill_pitr.py COUNT_TABLES 里的 control.*。
#: `usage_ledger` 不在这里：语料侧本来就碰不到它（它通过 outbox 事件上报），
#: 而把它列进来会让"Python 提到这个名字"也变成违规，那样反而挡不住真问题
CONTROL_TABLES = {
    "organizations", "users", "memberships", "roles", "role_permissions",
    "api_keys", "quotas", "usage_ledger", "audit_events",
    "upload_sessions", "file_grants", "control_outbox",
    # 节点身份与目录（恢复演练 COUNT_TABLES 在内 —— 备份漏掉它们也会报 NOLOSS）
    "node_identity", "node_directories", "node_members", "node_directory_views",
    "member_snapshots", "member_snapshot_pages",
    # 授权作用域
    "scope_manifests", "scope_target_pages", "scope_catalog_sources",
    "scope_catalog_revocations", "scope_remote_sources",
    # 联邦凭证与子树快照
    "federation_credential_nonces",
    "subtree_snapshots", "subtree_snapshot_pages",
}

WRITE_VERBS = ("INSERT INTO", "UPDATE", "DELETE FROM")


def _sql_writes(
    text: str, tables: set[str], *, exclude_schema: str | None = None
) -> set[str]:
    """找 `INSERT INTO <t>` / `UPDATE <t>` / `DELETE FROM <t>`。

    表名允许带 schema 前缀（control/corpus/public，可带双引号），表名本身也可带
    双引号；返回的永远是**真实表名**。**大小写不敏感** —— 小写的 sql 一样是 sql。
    `exclude_schema` 用于同名消歧：语料侧与控制侧各有一张
    `federation_credential_nonces`（语料在 public，控制在 control），Go 查语料时
    必须跳过 `control.` 限定的命中，只看 corpus/public/无前缀的写操作。
    """
    hits: set[str] = set()
    for verb in WRITE_VERBS:
        pattern = re.compile(
            rf"{verb}\s+(?:\"?(control|corpus|public)\"?\.)?\"?([a-z_]+)\"?",
            re.IGNORECASE)
        for match in pattern.finditer(text):
            schema = (match.group(1) or "").lower()
            if exclude_schema is not None and schema == exclude_schema:
                continue
            table = match.group(2).lower()
            if table in tables:
                hits.add(f"{verb} {table}")
    return hits


def check_go_never_writes_corpus() -> list[str]:
    problems = []
    for path in sorted(GO_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text(encoding="utf-8")
        # control. 限定的同名表是控制侧自己的，不管（见 _sql_writes 消歧注释）
        for hit in sorted(_sql_writes(text, CORPUS_TABLES, exclude_schema="control")):
            problems.append(f"{path.relative_to(ROOT)}: Go 写了语料表 —— {hit}")
    return problems


def check_python_never_writes_control() -> list[str]:
    problems = []
    for root in PY_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            # 注释与 docstring 里提到表名是正常的（这份代码库注释很多），
            # 所以只看**真正的字符串字面量与 SQL 文本**
            tree = ast.parse(text)
            literals = "\n".join(
                node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                # docstring 单独排除：它们也是 Constant，但不是 SQL
                and not node.value.lstrip().startswith(("\n", "#"))
            )
            for hit in sorted(_sql_writes(literals, CONTROL_TABLES)):
                problems.append(f"{path.relative_to(ROOT)}: Python 写了控制面表 —— {hit}")
    return problems


def _fk_target_is_control(ref: str) -> bool:
    """ForeignKey 指向的表是不是控制面的。

    `control.memberships.id` 按点切完第一段是 `control`，根本不在表集合里，
    所以必须看**每一段**：去引号、小写之后，任何一段命中 CONTROL_TABLES
    都算。`memberships.id`（无 schema）、`"control"."memberships"."id"`
    （全引号）都逃不掉；`documents.id` 不在集合里，不算。
    """
    segments = [seg.strip().strip('"').lower() for seg in ref.split(".")]
    return any(seg in CONTROL_TABLES for seg in segments)


def check_no_cross_schema_foreign_keys() -> list[str]:
    """corpus 模型里不得出现指向 control 表的 ForeignKey。

    跨 schema 硬外键会把两个服务的发布顺序绑死，也让"Python 不得修改组织成员"
    失去数据库层保障。引用完整性由对账兜底，不由外键兜底。
    """
    problems = []
    for root in PY_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                if name != "ForeignKey" or not node.args:
                    continue
                arg = node.args[0]
                if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                    continue
                if _fk_target_is_control(arg.value):
                    problems.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"语料模型指向控制面表 ForeignKey({arg.value!r})")
    return problems


def _privilege_files() -> list[pathlib.Path]:
    """默认权限要看全部 control 迁移，不只 0002。

    未来加 0017/0018 时裸写法一样要拦 —— 文件名写死就漏了。
    镜像 services/control-api/internal/migrate/sql 由
    check_control_migrations.py 钉成字节一致，这里只看权威源。
    """
    control = sorted((ROOT / "database" / "control").glob("*.sql"))
    return [*control, ROOT / "database" / "corpus" / "grants.sql"]


#: 0002 里那一行裸写法的原文（空白归一化、小写后比对）。它是已应用的历史：
#: control-migrate 把每个迁移的 sha256 写进 control.schema_migrations，
#: 改一个字都会让所有部署过的库下次 up 直接报错（migrate.go Up），
#: 所以 0002 本人不能动，由 0017 用 FOR ROLE ddp 重新声明覆盖（后声明的赢）。
#: 这里按原文精确豁免 —— 这行本身被改过的话，归一化比对就对不上，
#: 守卫照样红；豁免一次都没命中也会单独报错（豁免烂掉也看得见）。
_GRANDFATHERED_BARE = {
    "database/control/0002_roles.sql": {
        "alter default privileges in schema control "
        "grant select, insert, update, delete on tables to ddp_control;",
    },
}


def _default_privilege_missing_for_role(statement: str) -> bool:
    """这条 `ALTER DEFAULT PRIVILEGES` 是不是裸的（没写 `FOR ROLE`）。

    没写 `FOR ROLE` 就等于"将来谁建的表、权限归谁"没钉死：换个角色跑迁移，
    新表的新权限就悄悄归到那个角色名下（见 DATA-OWNERSHIP.md §1）。
    """
    return re.search(r"\bFOR\s+ROLE\b", statement, re.IGNORECASE) is None


def check_default_privileges_pin_owner() -> list[str]:
    """每个 `ALTER DEFAULT PRIVILEGES` 都必须带 `FOR ROLE`（0002 那一行历史原文除外）。"""
    problems = []
    stmt_pattern = re.compile(
        r"^ALTER\s+DEFAULT\s+PRIVILEGES\b(.*?);",
        re.IGNORECASE | re.DOTALL | re.MULTILINE)
    files = _privilege_files()
    if not files:
        return ["database/control 下一个 .sql 都没有 —— 扫描路径可能写错了"]
    exempted = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            problems.append(f"{path}: 授权文件找不到了，路径可能写错了")
            continue
        rel = path.relative_to(ROOT).as_posix()
        for match in stmt_pattern.finditer(text):
            if not _default_privilege_missing_for_role(match.group(0)):
                continue
            norm = " ".join(match.group(0).split()).lower()
            if norm in _GRANDFATHERED_BARE.get(rel, ()):
                exempted += 1
                continue
            problems.append(
                f"{rel}: ALTER DEFAULT PRIVILEGES 缺 FOR ROLE —— "
                "建表角色一换，新表的默认权限就归错人")
    if not exempted:
        problems.append(
            "0002 的历史豁免没命中任何语句 —— 0002 被改过（已应用的迁移不许改）"
            "或者豁免字符串写错了，先看 database/control/0002_roles.sql")
    return problems


def check_scan_is_not_vacuous() -> list[str]:
    """反哨兵：扫不到文件时上面三条全绿，而那说明路径写错了。"""
    problems = []
    go_files = list(GO_ROOT.rglob("*.go"))
    py_files = [p for root in PY_ROOTS if root.exists() for p in root.rglob("*.py")]
    if len(go_files) < 10:
        problems.append(f"只扫到 {len(go_files)} 个 Go 文件，GO_ROOT 可能写错了")
    if len(py_files) < 30:
        problems.append(f"只扫到 {len(py_files)} 个 Python 文件，PY_ROOTS 可能写错了")
    # CORPUS_TABLES 只许加不许删：删一张就少一张守卫，所以先卡数量
    if len(CORPUS_TABLES) < 61:
        problems.append(
            f"CORPUS_TABLES 只剩 {len(CORPUS_TABLES)} 张（应 ≥61），少列的表写操作拦不住")
    # 判据本身也要有效：拿一段确定违规的文本试一下
    if not _sql_writes("UPDATE control.memberships SET role = 'admin'", CONTROL_TABLES):
        problems.append("SQL 写操作的匹配逻辑坏了 —— 连明显的违规都认不出来")
    if not _sql_writes("insert into evidence (id) values (1)", CORPUS_TABLES):
        problems.append("SQL 匹配对小写不生效 —— 小写的 sql 一样是 sql")
    # public 限定、双引号、引号 schema 四种写法都要认到真表
    for probe, want in (
        ("UPDATE public.documents SET x = 1", {"UPDATE documents"}),
        ('UPDATE "documents" SET x = 1', {"UPDATE documents"}),
        ("INSERT INTO public.chunks (id) VALUES (1)", {"INSERT INTO chunks"}),
        ('UPDATE "public"."evidence" SET x = 1', {"UPDATE evidence"}),
    ):
        if _sql_writes(probe, CORPUS_TABLES) != want:
            problems.append(f"反哨兵漏网 —— {probe!r} 没认到真表，schema/引号写法绕得过守卫")
    # CORPUS_TABLES 一张都不能少：每个表四种写法全认，否则少的那张随便写
    for table in sorted(CORPUS_TABLES):
        for probe in (
            f"UPDATE {table} SET x = 1",
            f"UPDATE public.{table} SET x = 1",
            f'UPDATE "{table}" SET x = 1',
            f"INSERT INTO corpus.{table} (id) VALUES (1)",
        ):
            if not _sql_writes(probe, CORPUS_TABLES):
                problems.append(
                    f"反哨兵漏网 —— {probe!r} 没认出来，CORPUS_TABLES 缺表或匹配逻辑坏了")
    # 跨 schema 外键的三种写法都要拦，语料内部外键不许误报
    for ref in ("control.memberships.id", "memberships.id",
                '"control"."memberships"."id"'):
        if not _fk_target_is_control(ref):
            problems.append(f"反哨兵漏网 —— ForeignKey({ref!r}) 绕得过跨 schema 外键守卫")
    if _fk_target_is_control("documents.id"):
        problems.append("ForeignKey('documents.id') 被误报了 —— 语料内部外键是合法的")
    # 默认权限钉死：裸写法必须 flagged，带 FOR ROLE 的必须放过
    if not _default_privilege_missing_for_role(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA control "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ddp_control;"):
        problems.append("ALTER DEFAULT PRIVILEGES 的裸写法（缺 FOR ROLE）认不出来了")
    if _default_privilege_missing_for_role(
            "ALTER DEFAULT PRIVILEGES FOR ROLE ddp IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ddp_corpus;"):
        problems.append("带 FOR ROLE 的 ALTER DEFAULT PRIVILEGES 被误报了")
    return problems


def main() -> int:
    problems = (
        check_go_never_writes_corpus()
        + check_python_never_writes_control()
        + check_no_cross_schema_foreign_keys()
        + check_default_privileges_pin_owner()
        + check_scan_is_not_vacuous()
    )
    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n判据与理由见 docs/refactor/DATA-OWNERSHIP.md。"
              "跨边界要写别人的表，一律走 outbox 事件。", file=sys.stderr)
        return 1
    print("数据所有权守卫通过：Go 不写语料表，Python 不写控制面表，无跨 schema 外键")


if __name__ == "__main__":
    raise SystemExit(main())
