"""DDP-Extract v1 —— 抽取契约的解析与校验层。

格式写在 docs/extract-format.md，改字段先改那份文档。

这一层只做**纯函数**：schema 解析、值强制转换、结果自检。
真正的编排（检索 -> 抽值 -> 裁剪 -> 核对）在 extraction.py，
分开是为了让 schema 的边界能被单测直接钉住，不用起任何上游。

与版面 normalizer 的一个关键差别：`validate_schema` **在请求路径上强制**。
版面自检不强制是因为"一个字段缺失不该让整份解析结果作废"；
但 schema 是调用方给的输入，坏输入要当场 400 拒掉 ——
跑完一轮抽取（N 次检索 + N 次模型调用）再说不合规，是在烧别人的钱。
"""
import json
import re
from dataclasses import dataclass, field as dc_field

EXTRACT_VERSION = "ddp-extract/1"

# 叶子字段支持的类型。**刻意很短**：每加一种类型就要加一条强制转换规则，
# 而转换失败会表现成"抽不到"，看起来像模型不行 —— 宁可少支持几种
LEAF_TYPES = ("string", "number", "integer", "boolean")

# format 只作抽取提示与格式校验，不做时区换算/本地化
KNOWN_FORMATS = ("date", "date-time", "email", "uri")

# 契约里的三态与降级取值，validate_result 照着它检查
FIELD_STATUSES = ("found", "not_found", "error")
RESULT_STATUSES = ("ok", "partial", "failed")
DEGRADED_VALUES = (
    "no_hits", "embedding_unavailable", "vision_unavailable", "crop_unsupported",
    "crop_failed", "parse_mismatch", "upstream_error", "schema_violation",
    # 第九种：配了精排但上游没注册 rerank 模型。**必须在词汇表里** ——
    # 产品层会真的产出它，不收录的话 validate_result 会把一份合法结果判成不合规
    # （service 侧的 run_extraction 因此会直接把任务标 failed）
    "rerank_unavailable",
)

# 不支持的 JSON Schema 构造。**不是没来得及做**：每一条都会让"一个字段一次定位"
# 这个前提失效，出处会指到多个互斥的块（理由逐条写在 docs/extract-format.md）
_UNSUPPORTED_KEYS = ("oneOf", "anyOf", "allOf", "not", "$ref", "patternProperties")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class FieldSpec:
    name: str
    type: str
    description: str
    format: str | None = None
    enum: list | None = None
    required: bool = False

    @property
    def query(self) -> str:
        """这个字段的检索 query。

        字段名 + description 一起进：description 承载语义（"买方单位全称"），
        字段名承载调用方的用词习惯（`buyer_name`），两者对不同文档各有胜负。
        """
        return f"{self.name} {self.description}".strip()


@dataclass
class SchemaSpec:
    kind: str                                   # object | array
    fields: list[FieldSpec] = dc_field(default_factory=list)

    @property
    def required_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]


class SchemaError(ValueError):
    """schema 不合法。调用方的输入问题 -> 400，不是 5xx。"""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def validate_schema(schema: object) -> list[str]:
    """返回问题清单，空 = 通过。**不抛异常**，让调用方决定怎么报。"""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return ["schema 必须是 JSON 对象"]

    for key in _UNSUPPORTED_KEYS:
        if key in schema:
            problems.append(
                f"不支持 {key}：分支/引用语义没法映射到「一个字段一次定位」，"
                f"出处会指到多个互斥的块（见 docs/extract-format.md）")

    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            return problems + ["顶层 type=array 时必须有 items 对象"]
        return problems + _validate_object(items, path="items")
    if kind == "object" or (kind is None and "properties" in schema):
        return problems + _validate_object(schema, path="")
    return problems + [f"顶层 type 必须是 object 或 array（收到 {kind!r}）"]


def _validate_object(node: dict, *, path: str) -> list[str]:
    problems: list[str] = []
    prefix = f"{path}." if path else ""
    properties = node.get("properties")
    if not isinstance(properties, dict) or not properties:
        return [f"{prefix}properties 缺失或为空：没有字段就没有可抽取的东西"]

    required = node.get("required") or []
    if not isinstance(required, list):
        problems.append(f"{prefix}required 必须是数组")
        required = []
    for name in required:
        if name not in properties:
            problems.append(f"{prefix}required 里的 {name!r} 不在 properties 中")

    for name, prop in properties.items():
        where = f"{prefix}properties.{name}"
        if not isinstance(prop, dict):
            problems.append(f"{where} 必须是对象")
            continue
        for key in _UNSUPPORTED_KEYS:
            if key in prop:
                problems.append(f"{where} 不支持 {key}")
        ptype = prop.get("type", "string")
        if ptype in ("object", "array"):
            problems.append(
                f"{where} 不支持嵌套 {ptype}：每加一层嵌套，检索次数与出处归属的歧义"
                f"都乘一次。请拍平成 {name}.xxx 这样的扁平键")
            continue
        if ptype not in LEAF_TYPES:
            problems.append(f"{where}.type 必须是 {'/'.join(LEAF_TYPES)} 之一（收到 {ptype!r}）")
        # description 是硬要求，见下
        description = str(prop.get("description") or "").strip()
        if not description:
            problems.append(
                f"{where} 缺 description。它就是这个字段的检索 query —— 没有它只能拿"
                f"字段名去检索，{name!r} 这种名字必然打偏，而失败会表现成「抽不到」，"
                f"看起来像模型不行")
        fmt = prop.get("format")
        if fmt is not None and fmt not in KNOWN_FORMATS:
            problems.append(f"{where}.format 未知：{fmt!r}（支持 {'/'.join(KNOWN_FORMATS)}）")
        enum = prop.get("enum")
        if enum is not None and (not isinstance(enum, list) or not enum):
            problems.append(f"{where}.enum 必须是非空数组")
    return problems


def parse_schema(schema: dict) -> SchemaSpec:
    """schema -> SchemaSpec。**调用前必须先过 validate_schema**（这里不再重复校验）。"""
    kind = "array" if schema.get("type") == "array" else "object"
    node = schema.get("items") if kind == "array" else schema
    node = node if isinstance(node, dict) else {}
    required = set(node.get("required") or [])
    fields = [
        FieldSpec(
            name=name,
            type=prop.get("type", "string"),
            description=str(prop.get("description") or "").strip(),
            format=prop.get("format"),
            enum=prop.get("enum"),
            required=name in required,
        )
        for name, prop in (node.get("properties") or {}).items()
        if isinstance(prop, dict)
    ]
    return SchemaSpec(kind=kind, fields=fields)


# ---------- 值的强制转换 ----------

_TRUE = {"true", "yes", "是", "有", "1", "y"}
_FALSE = {"false", "no", "否", "无", "没有", "0", "n"}
# 数字里常见的装饰：货币符号、千分位、单位。抽出来的 "¥1,234.00 元" 要能变成 1234.0
_NUMBER_STRIP = re.compile(r"[^\d.\-+eE]")


class CoerceError(ValueError):
    pass


def coerce_value(raw: object, spec: FieldSpec):
    """把模型给的值转成 schema 声明的类型。转不动抛 CoerceError -> 字段判 error。

    **不许"尽力而为地猜"**：猜出来的值没法追溯，而抽取结果是要被当数据用的。
    转不动就如实报 error，让用户看得见"这个字段我们没能可靠地抽出来"。
    """
    if raw is None:
        return None
    if spec.type == "string":
        value = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        value = value.strip()
        if not value:
            return None
        _check_format(value, spec)
        _check_enum(value, spec)
        return value
    if spec.type == "boolean":
        if isinstance(raw, bool):
            return raw
        key = str(raw).strip().lower()
        if key in _TRUE:
            return True
        if key in _FALSE:
            return False
        raise CoerceError(f"无法把 {raw!r} 解释为布尔值")
    # number / integer
    if isinstance(raw, bool):       # bool 是 int 的子类，不拦会把 True 变成 1
        raise CoerceError(f"字段声明为 {spec.type}，模型给的是布尔值 {raw!r}")
    if isinstance(raw, (int, float)):
        number = raw
    else:
        cleaned = _NUMBER_STRIP.sub("", str(raw))
        if not cleaned or cleaned in ("-", "+", "."):
            raise CoerceError(f"无法从 {raw!r} 里解析出数字")
        try:
            number = float(cleaned)
        except ValueError as exc:
            raise CoerceError(f"无法从 {raw!r} 里解析出数字") from exc
    if spec.type == "integer":
        if float(number) != int(number):
            raise CoerceError(f"字段声明为 integer，但值 {raw!r} 有小数部分")
        number = int(number)
    _check_enum(number, spec)
    return number


def _check_enum(value, spec: FieldSpec) -> None:
    """枚举外的值一律拒绝。

    **不许把没有的选项硬塞一个进去** —— 枚举字段的下游多半是分类统计，
    塞一个近似值会让统计悄悄错掉，比空着更糟。
    """
    if spec.enum and value not in spec.enum:
        raise CoerceError(f"值 {value!r} 不在 enum {spec.enum} 内")


def _check_format(value: str, spec: FieldSpec) -> None:
    if spec.format == "date" and not _DATE_RE.match(value):
        raise CoerceError(f"format=date 要求 YYYY-MM-DD，收到 {value!r}")
    if spec.format == "date-time" and not _DATETIME_RE.match(value):
        raise CoerceError(f"format=date-time 要求 ISO 8601，收到 {value!r}")
    if spec.format == "email" and not _EMAIL_RE.match(value):
        raise CoerceError(f"format=email 不匹配：{value!r}")


# ---------- 模型输出里把 JSON 抠出来 ----------

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def parse_json_object(raw: str) -> dict | None:
    """从模型输出里取出第一个 JSON 对象；取不到返回 None（调用方重试/判 schema_violation）。

    模型很爱在 JSON 前后加解释文字或 ``` 围栏 —— 这不算它出错，
    但**也不能靠"大概能 json.loads"蒙混过去**：抠不出来就是抠不出来，
    重试用尽后必须打 schema_violation，不许静默把这个字段当成"文档里没有"。
    """
    if not raw:
        return None
    text = raw.strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    # 退一步：找第一个配平的 {...}
    start = text.find("{")
    while start != -1:
        depth, in_str, escaped = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start:i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------- 结果构造与自检 ----------

def field_result(*, status: str, value=None, citations: list | None = None,
                 verified: bool = False, degraded: str | None = None,
                 confidence: dict | None = None) -> dict:
    return {
        "status": status,
        "value": value,
        "citations": citations or [],
        "verified": verified,
        "degraded": degraded,
        "confidence": confidence or {"level": "unknown", "top_similarity": None},
    }


def overall_status(fields: dict, spec: SchemaSpec) -> str:
    """ok / partial / failed。

    partial 的判据是**必填字段没抽到**，不是"有字段没抽到" ——
    schema 里大多数字段本来就允许缺（合同不一定有违约金条款）。
    把可选字段的 not_found 算成 partial 会让这个标记永远是 partial，从此没人看它。
    """
    if not fields:
        return "failed"
    if any(f.get("status") == "error" for f in fields.values()):
        return "partial"
    missing = [n for n in spec.required_names
               if fields.get(n, {}).get("status") != "found"]
    return "partial" if missing else "ok"


def validate_result(result: object) -> list[str]:
    """结果自检，返回问题清单（空 = 通过）。测试里是硬断言。"""
    problems: list[str] = []
    if not isinstance(result, dict):
        return ["结果不是对象"]
    if result.get("extract_version") != EXTRACT_VERSION:
        problems.append(f"extract_version 应为 {EXTRACT_VERSION}，"
                        f"实际 {result.get('extract_version')!r}")
    if result.get("status") not in RESULT_STATUSES:
        problems.append(f"status 不合规：{result.get('status')!r}")
    degraded = result.get("degraded")
    if degraded is not None and degraded not in DEGRADED_VALUES:
        problems.append(f"degraded 不在词汇表内：{degraded!r}")

    buckets = []
    if isinstance(result.get("fields"), dict):
        buckets.append(("fields", result["fields"]))
    for i, record in enumerate(result.get("records") or []):
        if isinstance(record, dict) and isinstance(record.get("fields"), dict):
            buckets.append((f"records[{i}].fields", record["fields"]))
    if not buckets:
        problems.append("既没有 fields 也没有 records —— 结果为空")

    for where, fields in buckets:
        for name, item in fields.items():
            at = f"{where}.{name}"
            if not isinstance(item, dict):
                problems.append(f"{at} 必须是对象")
                continue
            if item.get("status") not in FIELD_STATUSES:
                problems.append(f"{at}.status 不合规：{item.get('status')!r}")
            if item.get("status") != "found" and item.get("value") is not None:
                problems.append(f"{at}: 非 found 状态却带着值 {item.get('value')!r} ——"
                                f" 这会让空值看起来像结论")
            if item.get("status") == "found" and not item.get("citations"):
                problems.append(f"{at}: found 却没有出处。"
                                f" 抽到值必须指得回原文，否则这个项目就没有存在意义")
            fdeg = item.get("degraded")
            if fdeg is not None and fdeg not in DEGRADED_VALUES:
                problems.append(f"{at}.degraded 不在词汇表内：{fdeg!r}")
    return problems
