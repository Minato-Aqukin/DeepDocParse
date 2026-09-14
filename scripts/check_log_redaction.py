#!/usr/bin/env python
"""日志脱敏静态守卫 —— 生产代码里不许把凭据或正文打进日志。

    python scripts/check_log_redaction.py            # 门禁扫描
    python scripts/check_log_redaction.py --self-test # 先证明扫描器本身有效

## 它守的是什么

`docs/refactor/STATUS.md` 的「验收门还差什么」把「日志脱敏的机械检查」列为
安全轮次欠账。这是那条欠账的机械部分：**扫源码**，不是扫运行时日志 ——
后者只能证明"已经漏出去的那些没被打印"，前者才拦得住新写进去的。

判据分两层，只有一层是行为，另一层是结构：

1. **值**：日志调用的实参里出现凭据/正文标识符（token、secret、api_key、
   Authorization、jwt、password、dsn、payload、body、excerpt、question、
   file_bytes…）→ 红。这是唯一能机械判定的那半。
2. **键**：结构化日志的键名是 `"token"` / `"api_key"` / `"body"` 这类
   名字 → 红。**下标键也算**：`logger.info(headers["Authorization"])`、
   `{"authorization": h}`、Go 的 kv 对与 `zap.String("token", …)`。

形状覆盖（每一类都有 self-test 样例钉着）：

- Python：`log.info(...)` / `print(...)`；`import logging as L`、
  `L = logging.getLogger(...)`、`H = L` 这些别名的调用；关键字参数、下标键、
  字典键、直接以敏感 key 形状出现的字面量、f-string；`logging.getLogger().info`
  这种包裹形态。
- Go：**不按接收者白名单**扫所有 `.Info/.Warn/.Error/.Debug/.Fatal/.Panic/
  .Log/.Errorf…(` 调用（`l.Info`、`logger.With(...).Info` 都命中），
  kv 对里的键与值、`zap.<Kind>("key", …)` 的嵌套键；`log.Printf`/`fmt.Println`
  族仍然只查值。

**它不声称能做的事**（写在前面，免得被当成假守卫）：

- 格式化字符串里的提示文字（`"占位 token 检查被跳过"`）不算命中 —— 这是
  刻意的，不能靠关键词把正常文案连坐；
- 硬编码进字面量的密钥、上游异常文本里夹带的连接串扫不出来；
- **跨文件/动态的间接层扫不出来**：`helpers.logger` 这类再导出、
  `functools.wraps` 包装、`getattr(logger, level)()`、依赖注入拿到的 logger
  变量，都识别不了；Go 的接口方法、泛型包装同理；
- 因此它拦的是"直接写下的泄漏模式"，不是全部泄漏。残余风险靠代码审查与
  运行时凭据轮换兜底。

## 假守卫防护

- **扫到零个文件 = 失败**：文件集与最小数量都钉死。globbing 写错时，
  这个脚本必须红，而不是安静地绿。
- **--self-test**：用内嵌样例证明"该红的红、该绿的绿"（变异确认的常驻版）。
- **allowlist 过期即红**：例外必须带理由，且被例外的那一行必须真的还是
  命中项；否则说明代码已经改了而例外没删，这条守卫会当场报出来。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "scripts" / "log_redaction_allowlist.txt"

#: 扫哪些目录。生产源码 = services/ 与 python/ 下的包；测试目录单独排除，
#: 因为测试里打印假密钥是夹具，不是泄漏。
PYTHON_ROOTS = (ROOT / "services", ROOT / "python")
GO_ROOTS = (ROOT / "services" / "control-api",)
EXCLUDE_DIRS = {"tests", "__pycache__", "node_modules", ".venv", "venv", "build", "dist"}
#: globbing 写错时的兜底。数字按当下源码树取下界（写死是特意的：
#: 重构把文件搬走时应该有人显式改这里，而不是让守卫默默量到更少）。
MIN_PYTHON_FILES = 100
MIN_GO_FILES = 30

#: 日志调用的宿主名。显式列出，避免把 `catalog.error()` 这类领域方法当成日志。
LOGGER_NAMES = {"log", "logger", "_log", "_logger", "LOG", "LOGGER", "slog", "logging"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal",
               "log"}
#: print 也是日志出口（本仓有若干 print 到 stderr 的 worker）。
PRINT_NAMES = {"print"}

#: 凭据/正文标识符的片段。大小写不敏感，按 `_` 与 camelCase 切段后逐段精确匹配
#: —— 精确匹配是为了不把 `tokenize_backend` 这种正常名字连坐。
SENSITIVE_SEGMENTS = {
    "token", "secret", "password", "passwd", "credential", "credentials",
    "apikey", "authorization", "authheader", "bearer", "jwt", "dsn", "connstr",
    "connectionstring", "databaseurl", "dsnurl", "accesskey", "secretkey",
    "requestbody", "responsebody", "rawbody", "payload", "excerpt", "question",
    "rawquestion", "filebytes", "rawbytes", "access_token", "refresh_token",
}
#: 组合片段（切段之后仍可能合并在一起的）。
SENSITIVE_COMPOUND = re.compile(
    r"(?i)^(?:api_?key|access_?key|secret_?key|auth_?header|connection_?string|"
    r"database_?url|request_?body|response_?body|file_?bytes|raw_?question)$")

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _segments(name: str) -> list[str]:
    """把标识符切成小写片段：apiKey / API_KEY / api-key 都切成 {api,key}。"""
    return [part for part in re.split(r"[_\-.]+", _CAMEL.sub("_", name)) if part]


def _sensitive_name(name: str) -> bool:
    if not name:
        return False
    lowered = name.lower()
    if SENSITIVE_COMPOUND.match(lowered):
        return True
    parts = _segments(lowered)
    if any(part in SENSITIVE_SEGMENTS for part in parts):
        return True
    # `apitoken`、`dsnurl` 这类连写（没有分隔符）也命中。
    joined = "".join(parts)
    return any(joined == word or lowered == word
               for word in ("token", "secret", "password", "credential", "apikey",
                            "authorization", "jwt", "dsn", "payload", "excerpt", "question"))


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


# ---------------------------------------------------------------- Python 扫描

def _leaf_names(node: ast.AST) -> list[str]:
    """取一个表达式里所有"末端标识符"：名字、属性链最后一段与字符串键。

    `body.event_id` 只算 `event_id` —— `body` 是取属性的中间量，不是被打印的
    值；`payload[0]` 算 `payload`；**`headers["Authorization"]` 额外算上
    字符串键 `Authorization`**（下标键是结构化日志最常见的凭据入口，只看
    `headers` 会漏）；字典字面量的键同理；f-string 里的表达式递归。
    """
    names: list[str] = []
    if isinstance(node, ast.Name):
        names.append(node.id)
    elif isinstance(node, ast.Attribute):
        names.append(node.attr)
    elif isinstance(node, ast.JoinedStr):
        # f-string 只看格式化表达式：字面量部分与直接字符串实参同一口径，
        # 不再在 `payload={payload}` 上对同一个值重复报两次。
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                names.extend(_leaf_names(value.value))
    elif isinstance(node, ast.BinOp):
        names.extend(_leaf_names(node.left))
        names.extend(_leaf_names(node.right))
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for item in node.elts:
            names.extend(_leaf_names(item))
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.extend(_string_keys(key.value))
            if value is not None:
                names.extend(_leaf_names(value))
    elif isinstance(node, ast.Call):
        # `str(token)` / `repr(payload)`：看参数，不看被调函数名。
        for arg in node.args:
            names.extend(_leaf_names(arg))
        for keyword in node.keywords:
            if keyword.value is not None:
                names.extend(_leaf_names(keyword.value))
    elif isinstance(node, ast.Subscript):
        names.extend(_leaf_names(node.value))
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.extend(_string_keys(key.value))
    elif isinstance(node, ast.Starred):
        names.extend(_leaf_names(node.value))
    return names


def _string_keys(text: str) -> list[str]:
    """把字符串/literal key 变成可判定的候选名；提示文字交给 `_sensitive_name` 过滤。

    `"token"` / `"Authorization:"` 是 key 的形状；`"token 检查被跳过"` 这种
    带空格的自然语言提示不是 —— 后者必须保持绿（self-test 里钉着）。
    """
    candidate = text.strip().rstrip("=:").strip()
    return [candidate] if candidate else []


def _is_getlogger(func: ast.expr, getlogger_names: set[str]) -> bool:
    if isinstance(func, ast.Name):
        return func.id in getlogger_names
    if isinstance(func, ast.Attribute):
        return func.attr == "getLogger"
    return False


def _logger_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """本文件里 logging 的别名：`import logging as L`、getLogger 别名、赋值别名。

    这是机械守卫能到的一层，**不是全部**：跨文件的间接层（`helpers.logger`）、
    `functools.wraps` 的包装、动态 getattr 仍然扫不到 —— 残余风险写在模块
    docstring 里，别把它读成"全覆盖"。
    """
    module_names = {"logging"}
    getlogger_names = {"getLogger"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    module_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "logging":
            for alias in node.names:
                if alias.name == "getLogger":
                    getlogger_names.add(alias.asname or alias.name)

    logger_names: set[str] = set(LOGGER_NAMES) | module_names
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        # `L = logging.getLogger(...)` / `L = gl(...)`；`gl` 是 getLogger 别名。
        if _is_getlogger(node.value.func, getlogger_names):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    logger_names.add(target.id)
    # 二次传播：`L2 = L1`（logger 实例的简单别名）。
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id in logger_names:
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in logger_names:
                        logger_names.add(target.id)
                        changed = True
    return logger_names, getlogger_names


def _python_call_target(node: ast.Call, logger_names: set[str],
                        getlogger_names: set[str]) -> str | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id in PRINT_NAMES:
        return "print"
    if isinstance(func, ast.Name) and func.id in logger_names:
        return func.id
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        base = func.value
        if isinstance(base, ast.Name) and base.id in logger_names:
            return f"{base.id}.{func.attr}"
        if isinstance(base, ast.Attribute) and base.attr in logger_names:
            return f"{base.attr}.{func.attr}"
        # 包裹形态：`logging.getLogger(__name__).info(...)` /
        # `get_logger(__name__).info(...)`。
        if isinstance(base, ast.Call) and _is_getlogger(base.func, getlogger_names):
            return f"getLogger().{func.attr}"
    return None


def scan_python(path: Path, text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [(exc.lineno or 1, "python source does not parse")]

    logger_names, getlogger_names = _logger_aliases(tree)
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _python_call_target(node, logger_names, getlogger_names)
        if target is None:
            continue
        for argument in [*node.args, *node.keywords]:
            if isinstance(argument, ast.keyword):
                if _sensitive_name(argument.arg or ""):
                    findings.append((node.lineno, f"{target}() emits {argument.arg!r}"))
                if argument.value is not None:
                    for name in _leaf_names(argument.value):
                        if _sensitive_name(name):
                            findings.append((node.lineno, f"{target}() emits {name!r}"))
                continue
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                # literal key 形态（`logger.info("token", value)` 的第一半）；
                # 自然语言提示（带空格、非 key 形状）由 `_string_keys` 放行。
                for name in _string_keys(argument.value):
                    if _sensitive_name(name):
                        findings.append((node.lineno, f"{target}() emits literal {name!r}"))
                continue
            for name in _leaf_names(argument):
                if _sensitive_name(name):
                    findings.append((node.lineno, f"{target}() emits {name!r}"))
    return findings


# -------------------------------------------------------------------- Go 扫描

#: **不按接收者名字过滤**：`l.Info` / `logger.With(...).Info` / `zap` 这些
#: 别名与包装形态的名字无穷无尽，按 host 白名单只会给绕过留门。方法名 +
#: 参数形状（kv 对/格式串）已经足够把误报压到 allowlist 能兜住的程度。
GO_CALL = re.compile(
    r"\.\s*(?P<method>Debug|Info|Warn|Warning|Error|Log|Fatal|Panic)"
    r"(?P<suffix>Context|f)?\s*\("
)
#: `log` / `fmt` 的 Print 族是独立的（消息 + 值，没有键）。
GO_FMTLOG = re.compile(
    r"\b(?:log|fmt)\s*\.\s*(?P<fmtlog>Print|Printf|Println|Fprint\w*)\s*\("
)
_GO_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_GO_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
#: zap 把键包在 `zap.String("key", value)` 这类嵌套调用里；键是字面量。
GO_ZAP_KEY = re.compile(r"\bzap\s*\.\s*\w+\s*\(\s*\"((?:[^\"\\]|\\.)*)\"")


def _balanced_arguments(text: str, open_paren: int) -> str:
    """取一对括号之间的原始文本（忽略字符串里的括号）。"""
    depth, quote, escaped = 1, None, False
    out: list[str] = []
    index = open_paren + 1
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'", "`"):
            quote = char
            out.append(char)
        elif char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(out)
            out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _split_top_level(arguments: str) -> list[str]:
    parts, depth, quote, escaped, current = [], 0, None, False, []
    for char in arguments:
        if quote:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'", "`"):
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _go_operand_names(argument: str) -> list[str]:
    stripped = argument.strip()
    if _GO_STRING.fullmatch(stripped) or (stripped.startswith("`") and stripped.endswith("`")):
        return []
    # 先掏空字面量，否则 `"token="+token` 会在引号里再找到一个 token。
    without_literals = re.sub(r'"(?:[^"\\]|\\.)*"', " ", stripped)
    without_literals = re.sub(r"`[^`]*`", " ", without_literals)
    names: list[str] = []
    for match in _GO_IDENT.finditer(without_literals):
        token = match.group(0)
        if token in ("true", "false", "nil", "string", "int"):
            continue
        # 取链的末端：a.b.c -> c；单独的名字取自己。
        names.append(token.rsplit(".", 1)[-1])
    return names


def _go_message_findings(message: str, method: str, line: int) -> list[tuple[int, str]]:
    """消息本身：纯字面量提示不算命中；`"token="+token` 这种拼接必须算。"""
    if _GO_STRING.fullmatch(message.strip()):
        return []
    return [(line, f"{method}() logs {name!r}")
            for name in _go_operand_names(message) if _sensitive_name(name)]


def scan_go(path: Path, text: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for match in GO_CALL.finditer(text):
        method = match.group("method")
        suffix = match.group("suffix") or ""
        arguments = _split_top_level(_balanced_arguments(text, match.end() - 1))
        line = text.count("\n", 0, match.start()) + 1
        if suffix == "f":
            # Infof/Errorf 是 printf 形态：消息 + 值，没有键。
            for argument in arguments:
                for name in _go_operand_names(argument):
                    if _sensitive_name(name):
                        findings.append((line, f"{method}f() logs {name!r}"))
        else:
            findings.extend(_go_message_findings(arguments[0] if arguments else "",
                                                 method, line))
            # 结构化日志的 kv 是成对出现的：奇数下标是键，偶数下标是值。
            body = arguments[1:] if arguments else []
            for key, value in zip(body[0::2], body[1::2]):
                literal = _GO_STRING.fullmatch(key.strip())
                if literal and _sensitive_name(literal.group(1)):
                    findings.append((line, f"{method}() logs key {literal.group(1)!r}"))
                for name in _go_operand_names(value):
                    if _sensitive_name(name):
                        findings.append((line, f"{method}() logs {name!r}"))
        # zap 风格的嵌套调用：键在 `zap.String("key", …)` 里。
        raw = _balanced_arguments(text, match.end() - 1)
        for zap_match in GO_ZAP_KEY.finditer(raw):
            key = zap_match.group(1)
            if _sensitive_name(key):
                findings.append((line, f"{method}() logs zap key {key!r}"))
    for match in GO_FMTLOG.finditer(text):
        method = match.group("fmtlog")
        arguments = _split_top_level(_balanced_arguments(text, match.end() - 1))
        line = text.count("\n", 0, match.start()) + 1
        # log.Printf / fmt.Println：字面量提示文字不算，只查值。
        for argument in arguments:
            for name in _go_operand_names(argument):
                if _sensitive_name(name):
                    findings.append((line, f"{method}() logs {name!r}"))
    return findings


# ------------------------------------------------------------------ 文件集合

def production_files() -> tuple[list[Path], list[Path]]:
    python_files: list[Path] = []
    for root in PYTHON_ROOTS:
        for path in sorted(root.rglob("*.py")):
            relative_parts = path.relative_to(ROOT).parts
            if any(part in EXCLUDE_DIRS for part in relative_parts):
                continue
            if path.name.startswith("test_") or path.name == "conftest.py":
                continue
            python_files.append(path)
    go_files: list[Path] = []
    for root in GO_ROOTS:
        for path in sorted(root.rglob("*.go")):
            relative_parts = path.relative_to(ROOT).parts
            if any(part in EXCLUDE_DIRS for part in relative_parts):
                continue
            if path.name.endswith("_test.go"):
                continue
            go_files.append(path)
    return python_files, go_files


# ------------------------------------------------------------------- allowlist

def load_allowlist(path: Path) -> tuple[dict[tuple[str, int], str], list[str]]:
    """返回 ((相对路径, 行号) -> 理由, 格式错误列表)。

    每行：`相对路径:行号  # 理由`。空行与 `#` 注释跳过。
    """
    allowed: dict[tuple[str, int], str] = {}
    problems: list[str] = []
    if not path.exists():
        return allowed, problems
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, _, reason = line.partition("#")
        location = body.strip()
        if not reason.strip():
            problems.append(f"allowlist 第 {number} 行没有理由（例外必须写清为什么安全）")
            continue
        rel, sep, lineno = location.rpartition(":")
        if not sep or not lineno.isdigit():
            problems.append(f"allowlist 第 {number} 行格式不对：{location!r}")
            continue
        allowed[(rel, int(lineno))] = reason.strip()
    return allowed, problems


# --------------------------------------------------------------------- 主流程

def run_scan(*, verbose: bool = False) -> int:
    python_files, go_files = production_files()
    problems: list[str] = []
    if len(python_files) < MIN_PYTHON_FILES:
        problems.append(f"只扫到 {len(python_files)} 个 Python 文件（下界 {MIN_PYTHON_FILES}）"
                        "—— globbing 写错时这条必须红")
    if len(go_files) < MIN_GO_FILES:
        problems.append(f"只扫到 {len(go_files)} 个 Go 文件（下界 {MIN_GO_FILES}）"
                        "—— globbing 写错时这条必须红")

    allowed, allowlist_problems = load_allowlist(ALLOWLIST)
    problems.extend(allowlist_problems)
    hits: list[Finding] = []
    for path in python_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, reason in scan_python(path, text):
            hits.append(Finding(path.relative_to(ROOT).as_posix(), line, reason))
    for path in go_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line, reason in scan_go(path, text):
            hits.append(Finding(path.relative_to(ROOT).as_posix(), line, reason))

    remaining: list[Finding] = []
    hit_keys: set[tuple[str, int]] = set()
    for finding in hits:
        key = (finding.path, finding.line)
        hit_keys.add(key)
        if key in allowed:
            if verbose:
                print(f"allowlisted {finding.path}:{finding.line} —— {allowed[key]}")
            continue
        remaining.append(finding)

    # 例外必须仍然命中：代码改了、例外没删 = 一条永久豁免，必须报出来。
    for (rel, line), reason in sorted(allowed.items()):
        if (rel, line) not in hit_keys:
            problems.append(f"allowlist 过期：{rel}:{line} 现在不再命中（{reason}），"
                            "请删掉这条例外")

    for problem in problems:
        print(f"::error::日志脱敏: {problem}", file=sys.stderr)
    for finding in sorted(remaining, key=lambda item: (item.path, item.line)):
        print(f"::error::日志脱敏: {finding.render()}", file=sys.stderr)

    scanned = f"扫描 {len(python_files)} 个 Python + {len(go_files)} 个 Go 生产文件"
    if problems or remaining:
        print(f"日志脱敏 FAIL：{scanned}，{len(remaining)} 处命中，{len(problems)} 个结构问题",
              file=sys.stderr)
        return 1
    print(f"日志脱敏 OK：{scanned}，无凭据/正文外泄模式，"
          f"{len(allowed)} 条已审阅例外")
    return 0


# ------------------------------------------------------------------- self-test

PYTHON_BAD = '''\
import logging
logger = logging.getLogger(__name__)
L = logging.getLogger("alias")
H = logger

def f(headers, token, payload, question):
    logger.info("token=%s", token)
    logger.warning(f"payload={payload}")
    logger.info(headers["Authorization"])
    logger.info("h=%s", headers["Authorization"])
    logger.info("authorization")
    L.info(question)
    H.exception("q", question)
'''
PYTHON_GOOD = '''\
import logging
logger = logging.getLogger(__name__)
logger.info("processed %d documents", count)
logger.warning("token 检查被跳过")  # 提示文字，不是值
tokenize_backend = "jieba"
logger.info("backend=%s", tokenize_backend)
logger.info("headers received")
'''
GO_BAD = '''\
package main

import "log/slog"

func f(apiKey string, token string, err error, header string) {
\tslog.Error("auth failed", "api_key", apiKey, "err", err)
\tslog.Info("token="+token, "upstream", "x")
\tl := slog.Default()
\tl.Info("auth failed", "api_key", apiKey)
\tlogger.With("svc", "x").Info("auth", "token", token)
\tzap.L().Info("auth", zap.String("authorization", header))
}
'''
GO_GOOD = '''\
package main

import "log/slog"

func f(err error, count int, header string) {
\tslog.Error("auth failed", "err", err)
\tslog.Info("processed documents", "count", count)
\tslog.Info("headers received", "auth_state", header)
\tfmt.Println("done", count)
}
'''


def self_test() -> int:
    failures: list[str] = []

    def check(name: str, got: list[tuple[int, str]], expected: int) -> None:
        if len(got) != expected:
            failures.append(f"{name}: 期望 {expected} 处命中，实际 {len(got)}：{got}")

    check("python 坏样例", scan_python(Path("bad.py"), PYTHON_BAD), 7)
    check("python 好样例", scan_python(Path("good.py"), PYTHON_GOOD), 0)
    check("go 坏样例", scan_go(Path("bad.go"), GO_BAD), 8)
    check("go 好样例", scan_go(Path("good.go"), GO_GOOD), 0)

    # 0 文件检查也是一种自我测试：把下界调到不可能满足的值必须失败。
    import contextlib
    import io

    global MIN_PYTHON_FILES, MIN_GO_FILES
    for name, variable in (("Python", "MIN_PYTHON_FILES"), ("Go", "MIN_GO_FILES")):
        original = globals()[variable]
        try:
            globals()[variable] = 10**9
            # 这一趟刻意会红；把它的输出收起来，别让 self-test 的成功打印像失败。
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                empty_scan_code = run_scan(verbose=False)
            if empty_scan_code == 0:
                failures.append(f"{name} 最小文件数下界被架空："
                                f"把 {variable} 调到 10^9 后扫描仍然绿")
        finally:
            globals()[variable] = original

    if failures:
        for failure in failures:
            print(f"::error::日志脱敏 self-test: {failure}", file=sys.stderr)
        return 1
    print("日志脱敏 self-test OK：坏样例全红、好样例全绿、最小文件数不是摆设")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true",
                        help="证明扫描器本身有效，然后退出")
    parser.add_argument("--with-self-test", action="store_true",
                        help="先自检再扫描（门禁用，一条命令里两件事都做）")
    parser.add_argument("--verbose", action="store_true", help="打印被例外放过的命中")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.with_self_test and self_test() != 0:
        return 1
    return run_scan(verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
