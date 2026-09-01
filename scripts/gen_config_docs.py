#!/usr/bin/env python
"""从 pydantic Settings 生成各服务的配置参考文档。

**为什么是生成而不是手写**：`config.py` 里的注释质量本来就很高（取值依据、踩过的坑、
调高调低分别有什么后果都写了），缺的只是"这些注释没出现在任何部署者会看的地方"。
手抄一份进 README，两周后就会和代码漂移；生成的可以随时重跑。

对自部署软件，配置文档化程度直接决定别人能不能把它部署起来。

合仓前这个脚本有**两份逐字复制品**（service 一份、web 一份），差异只有
三个常量。现在是一份 + 一张目标表：加一个服务就往 `TARGETS` 里加一条。

注释从源码里按 AST + 行号取（pydantic 不保留注释）：
  - 字段上方连续的 `#` 行是说明
  - `# ---- xxx ----` 是分节标记，用来给表分组
  - 字段行尾的 `#` 是补充说明

用法：
    python scripts/gen_config_docs.py                    # 全部服务，写入各自的 CONFIG.md
    python scripts/gen_config_docs.py --check            # 只校验是否最新（CI 用）
    python scripts/gen_config_docs.py --only corpus-api  # 只处理一个
"""
import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Target:
    key: str
    source: Path
    output: Path
    title: str
    intro: str
    class_name: str = "Settings"
    #: python（pydantic Settings）或 go（config.go 里的 env(...) 调用）
    lang: str = "python"


TARGETS = (
    Target(
        key="model-gateway",
        source=ROOT / "services" / "model-gateway" / "ddp_gateway" / "config.py",
        output=ROOT / "services" / "model-gateway" / "CONFIG.md",
        title="DeepDocParse 模型网关配置参考",
        intro="""\
模型网关的全部配置项。取自 `services/model-gateway/ddp_gateway/config.py`，
**本文件由脚本生成，不要手改** —— 改注释请改源码，然后重跑
`python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > 服务自己的 `.env` > 下表默认值。
""",
    ),
    Target(
        key="control-api",
        source=ROOT / "services" / "control-api" / "internal" / "config" / "config.go",
        output=ROOT / "services" / "control-api" / "CONFIG.md",
        title="DeepDocParse 控制面配置参考",
        intro="""\
Go 控制面的全部配置项。取自 `services/control-api/internal/config/config.go`，
**本文件由脚本生成，不要手改** —— 改注释请改源码，然后重跑
`python scripts/gen_config_docs.py`。

配置来源：环境变量（Go 侧不读 `.env` 文件 —— 那是 pydantic-settings 的行为，
Go 这边由容器/systemd 注入环境变量）。

**占位密钥会拒绝启动**：`JWT_SECRET` 是 change-me 等于任何人都能给任意
user_id 伪造一个有效会话，且运行时不报任何错。一次性容器 / CI 可用
`ALLOW_INSECURE_DEFAULTS=true` 显式跳过 —— 逃生口必须显式且留痕。
""",
        lang="go",
    ),
    Target(
        key="corpus-api",
        source=ROOT / "services" / "corpus-api" / "ddp_corpus" / "config.py",
        output=ROOT / "services" / "corpus-api" / "CONFIG.md",
        title="DeepDocParse 语料 API 配置参考",
        intro="""\
语料 API 的全部配置项。取自 `services/corpus-api/ddp_corpus/config.py`，
**本文件由脚本生成，不要手改** —— 改注释请改源码，然后重跑
`python scripts/gen_config_docs.py`。

环境变量名 = 字段名大写（pydantic-settings 默认规则，未设前缀）。
配置来源优先级：环境变量 > 服务自己的 `.env` > 下表默认值。

账号、API key、配额、限速那一层的配置**不在这里** —— 它们属于
`services/control-api`（Go），见 `services/control-api/CONFIG.md`。

前端另有构建期变量（`apps/web/.env*`，不在下表）：`VITE_API_TARGET`
（dev server 代理到的后端地址）、`VITE_API_BASE`（打包后请求的前缀），
以及 `VITE_DEFAULT_ENGINE`（上传对话框预选的解析引擎，留空取 `ENGINES` 第一条）。
**`VITE_DEFAULT_ENGINE` 要与 `DEFAULT_PARSE_ENGINE`、`infra/registry/models.yaml`
三者对齐** —— 任一处对不上，上传会在网关侧收 404 unknown_engine。
""",
    ),
)

SECTION_RE = re.compile(r"^\s*#\s*-+\s*(.+?)\s*-+\s*$")


def _clean(comment: str) -> str:
    # 先 strip 掉缩进再剥 '#'：源码里的注释是带缩进的，直接 lstrip("#") 什么都剥不掉
    return comment.strip().lstrip("#").strip()


def collect(source_path: Path, class_name: str) -> list[tuple[str, list[dict]]]:
    """返回 [(节名, [字段, ...])]，保持源码顺序。"""
    lines = source_path.read_text(encoding="utf-8").splitlines()
    tree = ast.parse("\n".join(lines))
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if node is None:
        raise SystemExit(f"{source_path} 里没有 class {class_name}")

    sections: list[tuple[str, list[dict]]] = [("通用", [])]
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        name = stmt.target.id
        if name == "model_config":          # pydantic 自己的配置，不是部署项
            continue

        # 往上收集紧邻的注释块，遇到分节标记就切一节
        doc: list[str] = []
        cursor = stmt.lineno - 2            # lineno 是 1-based，-1 到本行，再 -1 到上一行
        while cursor >= 0 and lines[cursor].strip().startswith("#"):
            marker = SECTION_RE.match(lines[cursor])
            if marker:
                sections.append((marker.group(1), []))
                break
            doc.insert(0, _clean(lines[cursor]))
            cursor -= 1

        # 行尾注释
        _, _, trailing = lines[stmt.lineno - 1].partition("#")
        if trailing.strip():
            doc.append(trailing.strip())

        sections[-1][1].append({
            "name": name,
            "env": name.upper(),
            "type": ast.unparse(stmt.annotation),
            "default": ast.unparse(stmt.value) if stmt.value is not None else "（必填）",
            "doc": " ".join(doc).strip(),
        })
    return [(title, fields) for title, fields in sections if fields]


# --------------------------------------------------------------- Go 源码

#: `env("KEY", "default")` / envInt / envBool / envList
GO_CALL_RE = re.compile(
    r'(?P<field>\w+):\s*(?:int32\()?env(?P<kind>Int|Bool|List)?\('
    r'"(?P<key>[A-Z0-9_]+)"\s*,\s*(?P<default>[^\n]*?)\)')
GO_SECTION_RE = re.compile(r"^\s*//\s*-+\s*(.+?)\s*-+\s*$")

GO_TYPES = {None: "string", "Int": "int", "Bool": "bool", "List": "list[str]"}


def collect_go(source_path: Path) -> list[tuple[str, list[dict]]]:
    """从 config.go 的 `env(...)` 调用里抽配置项。

    说明取自**结构体字段的注释**（Load() 里那一片赋值是没有注释的），
    所以先扫一遍结构体建索引，再按字段名对上。这样注释只写一处，
    与 pydantic 那边"注释就在字段上方"的习惯一致。
    """
    lines = source_path.read_text(encoding="utf-8").splitlines()

    docs: dict[str, str] = {}
    sections: list[tuple[str, list[str]]] = [("通用", [])]
    pending: list[str] = []
    in_struct = False
    for line in lines:
        if line.startswith("type Config struct {"):
            in_struct = True
            continue
        if in_struct and line.startswith("}"):
            break
        if not in_struct:
            continue
        marker = GO_SECTION_RE.match(line)
        if marker:
            sections.append((marker.group(1), []))
            pending = []
            continue
        stripped = line.strip()
        if stripped.startswith("//"):
            pending.append(stripped.lstrip("/").strip())
            continue
        if not stripped:
            pending = []
            continue
        field = stripped.split()[0]
        trailing = stripped.split("//", 1)[1].strip() if "//" in stripped else ""
        docs[field] = " ".join([*pending, trailing]).strip()
        sections[-1][1].append(field)
        pending = []

    body = "\n".join(lines)
    found: dict[str, dict] = {}
    for m in GO_CALL_RE.finditer(body):
        field = m.group("field")
        default = m.group("default").strip().rstrip(")").strip()
        found[field] = {
            "name": field,
            "env": m.group("key"),
            "type": GO_TYPES[m.group("kind")],
            "default": default,
            "doc": docs.get(field, ""),
        }

    out: list[tuple[str, list[dict]]] = []
    for title, fields in sections:
        rows = [found[f] for f in fields if f in found]
        if rows:
            out.append((title, rows))
    return out


def render(target: Target, sections: list[tuple[str, list[dict]]]) -> str:
    total = sum(len(fields) for _, fields in sections)
    out = [f"# {target.title}", "", target.intro, f"共 **{total}** 项。", ""]
    for title, fields in sections:
        out += [f"## {title}", "", "| 环境变量 | 类型 | 默认值 | 说明 |", "|---|---|---|---|"]
        for f in fields:
            doc = f["doc"].replace("|", "\\|") or "—"
            default = f["default"].replace("|", "\\|")
            out.append(f"| `{f['env']}` | `{f['type']}` | `{default}` | {doc} |")
        out.append("")
    out.append("<!-- 由 scripts/gen_config_docs.py 生成，请勿手改 -->")
    return "\n".join(out) + "\n"


def process(target: Target, check: bool) -> int:
    if target.lang == "go":
        sections = collect_go(target.source)
    else:
        sections = collect(target.source, target.class_name)
    # "每一项都有说明"是验收标准本身，所以由脚本机械把关：
    # 没说明的配置项等于没文档，部署者照样得回去读源码
    undocumented = [f["env"] for _, fields in sections for f in fields if not f["doc"]]
    if undocumented:
        print(f"::error::{target.source.relative_to(ROOT)} 里这些配置项没有注释，"
              f"无法生成说明：{', '.join(undocumented)}")
        return 1

    content = render(target, sections)
    if check:
        current = target.output.read_text(encoding="utf-8") if target.output.exists() else ""
        if current != content:
            print(f"::error::{target.output.relative_to(ROOT)} 与 "
                  f"{target.source.relative_to(ROOT)} 不同步，"
                  f"请重跑 python scripts/gen_config_docs.py")
            return 1
        print(f"{target.output.relative_to(ROOT)} 是最新的")
        return 0

    target.output.parent.mkdir(parents=True, exist_ok=True)
    target.output.write_text(content, encoding="utf-8")
    print(f"已写入 {target.output.relative_to(ROOT)}（{sum(len(f) for _, f in sections)} 项）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    parser.add_argument("--only", help="只处理某一个服务（见 TARGETS 的 key）")
    args = parser.parse_args()

    targets = [t for t in TARGETS if args.only in (None, t.key)]
    if not targets:
        print(f"::error::没有叫 {args.only} 的目标，可选：{[t.key for t in TARGETS]}")
        return 1
    # **不要短路**：第一个不同步就退出的话，第二个的问题要等下一轮才暴露
    return max(process(t, args.check) for t in targets)


if __name__ == "__main__":
    sys.exit(main())
