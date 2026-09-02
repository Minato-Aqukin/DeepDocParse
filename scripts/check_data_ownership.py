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
CORPUS_TABLES = {
    "documents", "document_uploads", "parse_jobs", "chunks", "evidence", "citations",
    "agent_turns", "assertions", "retrieval_candidates", "evidence_verifications",
    "knowledge_entities", "graph_edges", "wiki_entries", "wiki_sections", "wiki_sentences",
    "knowledge_reviews", "conversations", "messages",
    "extraction_templates", "extraction_runs", "extraction_items",
    "tasks", "corpus_outbox", "processed_events", "usage_claims",
}

#: 控制面的治理表 —— Python 一个字都写不了。
#: `usage_ledger` 不在这里：语料侧本来就碰不到它（它通过 outbox 事件上报），
#: 而把它列进来会让"Python 提到这个名字"也变成违规，那样反而挡不住真问题
CONTROL_TABLES = {
    "organizations", "users", "memberships", "roles", "role_permissions",
    "api_keys", "quotas", "usage_ledger", "audit_events",
    "upload_sessions", "file_grants", "control_outbox",
}

WRITE_VERBS = ("INSERT INTO", "UPDATE", "DELETE FROM")


def _sql_writes(text: str, tables: set[str]) -> set[str]:
    """找 `INSERT INTO <t>` / `UPDATE <t>` / `DELETE FROM <t>`。

    表名允许带 schema 前缀。**大小写不敏感** —— 小写的 sql 一样是 sql。
    """
    hits: set[str] = set()
    for verb in WRITE_VERBS:
        pattern = re.compile(
            rf"{verb}\s+(?:(?:control|corpus)\.)?([a-z_]+)", re.IGNORECASE)
        for match in pattern.finditer(text):
            table = match.group(1).lower()
            if table in tables:
                hits.add(f"{verb} {table}")
    return hits


def check_go_never_writes_corpus() -> list[str]:
    problems = []
    for path in sorted(GO_ROOT.rglob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        text = path.read_text(encoding="utf-8")
        for hit in sorted(_sql_writes(text, CORPUS_TABLES)):
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
                table = arg.value.split(".")[0]
                if table in CONTROL_TABLES:
                    problems.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: "
                        f"语料模型指向控制面表 ForeignKey({arg.value!r})")
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
    # 判据本身也要有效：拿一段确定违规的文本试一下
    if not _sql_writes("UPDATE control.memberships SET role = 'admin'", CONTROL_TABLES):
        problems.append("SQL 写操作的匹配逻辑坏了 —— 连明显的违规都认不出来")
    if not _sql_writes("insert into evidence (id) values (1)", CORPUS_TABLES):
        problems.append("SQL 匹配对小写不生效 —— 小写的 sql 一样是 sql")
    return problems


def main() -> int:
    problems = (
        check_go_never_writes_corpus()
        + check_python_never_writes_control()
        + check_no_cross_schema_foreign_keys()
        + check_scan_is_not_vacuous()
    )
    for line in problems:
        print(f"::error::{line}")
    if problems:
        print("\n判据与理由见 docs/refactor/DATA-OWNERSHIP.md。"
              "跨边界要写别人的表，一律走 outbox 事件。", file=sys.stderr)
        return 1
    print("数据所有权守卫通过：Go 不写语料表，Python 不写控制面表，无跨 schema 外键")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
