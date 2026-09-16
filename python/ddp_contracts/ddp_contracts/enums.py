"""由 packages/contracts/scripts/generate.py 从 enums.yaml 生成 —— 不要手改。
改枚举请改 packages/contracts/enums.yaml，然后重跑 npm run contracts:gen。
"""
from __future__ import annotations

from typing import Final, Literal, TypedDict


class EnumMeta(TypedDict, total=False):
    """一个枚举取值的全部对外信息。"""
    value: str
    label: str
    severity: Literal["neutral", "progress", "ok", "warn", "error"]
    active: bool


# 问答 / 检索 / 抽取平面的降级原因。落在 `messages.degraded`、
# DDP-Extract 的 `degraded`、以及检索响应里。
#
# **一次只报一个**（最先命中的那个）。需要同时报多个的场合请用
# `compile_degraded` 那种列表形状，不要往这里塞逗号分隔串。
Degraded = Literal["no_hits", "parse_mismatch", "resource_index_unavailable", "embedding_unavailable", "vision_unavailable", "crop_unsupported", "crop_failed", "client_aborted", "upstream_error", "upstream_interrupted", "index_changed_during_answer", "decision_unavailable", "no_evidence_in_turn", "inherited_evidence_incomplete", "gate_rejected_all", "citation_persist_failed", "verification_unavailable", "schema_violation", "rerank_unavailable", "no_instruct_model", "empty_query", "answer_unavailable"]

DEGRADED_VALUES: Final[tuple[str, ...]] = (
    "no_hits",
    "parse_mismatch",
    "resource_index_unavailable",
    "embedding_unavailable",
    "vision_unavailable",
    "crop_unsupported",
    "crop_failed",
    "client_aborted",
    "upstream_error",
    "upstream_interrupted",
    "index_changed_during_answer",
    "decision_unavailable",
    "no_evidence_in_turn",
    "inherited_evidence_incomplete",
    "gate_rejected_all",
    "citation_persist_failed",
    "verification_unavailable",
    "schema_violation",
    "rerank_unavailable",
    "no_instruct_model",
    "empty_query",
    "answer_unavailable",
)

DEGRADED_META: Final[dict[str, EnumMeta]] = {
    # 检索一条都没命中
    "no_hits": {"value": "no_hits", "label": "未在本文档中检索到相关内容", "severity": "neutral"},
    # 裁图上的文字与解析出的块文本对不上（相似度低于
    # QA_PARSE_MISMATCH_THRESHOLD / EXTRACT_MISMATCH_THRESHOLD，实测标定 0.55）。
    # 它是**假出处**的主要探测手段，不是小问题。
    "parse_mismatch": {"value": "parse_mismatch", "label": "出处存疑（图上内容与解析文本对不上）", "severity": "warn"},
    # 授权资源的固定解析版本尚无可用索引
    "resource_index_unavailable": {"value": "resource_index_unavailable", "label": "该资源版本索引尚不可用，请查看解析任务", "severity": "warn"},
    # 向量化服务不可达，只走了关键词路。**这条是本项目吃过最大亏的地方**：
    # M4a 时向量检索静默退回 BM25，没人发现。必须可见。
    "embedding_unavailable": {"value": "embedding_unavailable", "label": "仅关键词检索（向量化服务不可用）", "severity": "warn"},
    # 视觉模型不可用，本轮没做视觉核对
    "vision_unavailable": {"value": "vision_unavailable", "label": "未做视觉验证（视觉模型不可用）", "severity": "warn"},
    # 该文件类型不支持按 bbox 裁图（例如非 PDF 原件）
    "crop_unsupported": {"value": "crop_unsupported", "label": "未做视觉验证（该文件不支持区域截图）", "severity": "neutral"},
    # 裁图渲染失败。**注意**：依赖缺失不走这条，见 ddp_core/crops.py 的 _DEP_NOTE
    "crop_failed": {"value": "crop_failed", "label": "未做视觉验证（区域截图失败）", "severity": "warn"},
    # 客户端在流式回答途中断开
    "client_aborted": {"value": "client_aborted", "label": "回答被中断", "severity": "neutral"},
    # 上游模型服务返回错误
    "upstream_error": {"value": "upstream_error", "label": "问答服务异常", "severity": "error"},
    # 上游在流式输出中途断流（拿到的是半截答案）
    "upstream_interrupted": {"value": "upstream_interrupted", "label": "回答生成中途断流", "severity": "error"},
    # 回答生成期间索引 generation 变了，本轮出处已标失效
    "index_changed_during_answer": {"value": "index_changed_during_answer", "label": "回答生成期间索引版本已变化，出处已标为失效", "severity": "warn"},
    # 「这轮要不要检索」的判定模型不可用，已保守地执行检索
    "decision_unavailable": {"value": "decision_unavailable", "label": "是否检索判定不可用，已保守执行检索", "severity": "neutral"},
    # 本轮既没检索到证据也没有可继承证据，拒绝脱离文档作答
    "no_evidence_in_turn": {"value": "no_evidence_in_turn", "label": "本轮没有可继承证据，已拒绝脱离文档作答", "severity": "warn"},
    # 上一轮的证据部分失效，不能直接沿用
    "inherited_evidence_incomplete": {"value": "inherited_evidence_incomplete", "label": "上一轮证据已部分失效，需重新检索后再回答", "severity": "warn"},
    # 候选全部没通过逐篇质量门控（有候选但都不够格，与 no_hits 不同）
    "gate_rejected_all": {"value": "gate_rejected_all", "label": "检索候选均未通过逐篇质量门控", "severity": "warn"},
    # 出处写库失败，相关结论已标为无证据支持
    "citation_persist_failed": {"value": "citation_persist_failed", "label": "出处保存失败，相关结论已标为无证据支持", "severity": "error"},
    # 原文自动核对没得出结论
    "verification_unavailable": {"value": "verification_unavailable", "label": "原文自动核对未得出结论，请人工复核", "severity": "warn"},
    # 模型输出反复不合 schema（已按 EXTRACT_MAX_RETRIES 重试仍失败）。
    # **绝不能被静默当成 not_found** —— 那会把系统故障伪装成"文档里没有"。
    "schema_violation": {"value": "schema_violation", "label": "模型输出不符合 schema（已重试仍失败）", "severity": "error"},
    # 配了精排但上游没注册 rerank 模型，本轮没重排
    "rerank_unavailable": {"value": "rerank_unavailable", "label": "未做精排（重排序服务不可用）", "severity": "neutral"},
    # 注册表里只有 OCR 专用模型（`capabilities` 含 `no_instruct`），
    # 抽值无处可调。同样绝不能伪装成 not_found。
    "no_instruct_model": {"value": "no_instruct_model", "label": "未抽取（后端没有可用的指令模型）", "severity": "error"},
    # MCP `search` 收到空查询串，直接返回空结果
    "empty_query": {"value": "empty_query", "label": "查询词为空", "severity": "neutral"},
    # MCP `ask` 调上游生成时非 200，本轮没有答案（证据仍然返回）
    "answer_unavailable": {"value": "answer_unavailable", "label": "生成服务不可用（证据已返回，结论未生成）", "severity": "error"},
}


def degraded_label(value: str | None) -> str | None:
    """degraded 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = DEGRADED_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 版面编译（DDP-Compile v1）的降级。与 `degraded` 分开是因为它是
# **列表**：一次编译可以同时有好几种降级，而且它落在
# `documents.compile_degraded`（JSON 数组）上。
CompileDegraded = Literal["code_detection_unavailable", "crop_unsupported", "crop_failed", "vision_unavailable", "vision_invalid_output", "provider_unresolved", "reindex_validation_required", "compile_failed"]

COMPILE_DEGRADED_VALUES: Final[tuple[str, ...]] = (
    "code_detection_unavailable",
    "crop_unsupported",
    "crop_failed",
    "vision_unavailable",
    "vision_invalid_output",
    "provider_unresolved",
    "reindex_validation_required",
    "compile_failed",
)

COMPILE_DEGRADED_META: Final[dict[str, EnumMeta]] = {
    # 当前版面引擎报不出代码块
    "code_detection_unavailable": {"value": "code_detection_unavailable", "label": "当前版面引擎不能识别代码块", "severity": "neutral"},
    # 部分视觉原子没有可定位的裁图
    "crop_unsupported": {"value": "crop_unsupported", "label": "部分视觉原子没有可定位裁图", "severity": "neutral"},
    # 部分视觉原子裁图失败
    "crop_failed": {"value": "crop_failed", "label": "部分视觉原子裁图失败", "severity": "warn"},
    # 视觉理解模型不可用
    "vision_unavailable": {"value": "vision_unavailable", "label": "视觉理解模型不可用", "severity": "warn"},
    # 视觉模型返回的结构不合规
    "vision_invalid_output": {"value": "vision_invalid_output", "label": "视觉理解模型返回的结构不合规", "severity": "warn"},
    # 上游实际模型没解析出来，本次编译版本不可比较
    "provider_unresolved": {"value": "provider_unresolved", "label": "上游实际模型未解析，当前编译版本不可比较", "severity": "warn"},
    # 存在历史出处，需先校验并人工确认后才能重建
    "reindex_validation_required": {"value": "reindex_validation_required", "label": "存在历史出处，需先校验并确认后重建", "severity": "warn"},
    # 版面编译整体失败
    "compile_failed": {"value": "compile_failed", "label": "版面编译失败", "severity": "error"},
}


def compile_degraded_label(value: str | None) -> str | None:
    """compile_degraded 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = COMPILE_DEGRADED_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 解析任务状态。契约（`/v1/parse/{id}`）只承诺四态；
# `archiving` 是**产品层**多出来的一态：网关已完成但归档还没落地，
# 对用户是"还在动"。
ParseStatus = Literal["pending", "running", "archiving", "succeeded", "failed"]

PARSE_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "running",
    "archiving",
    "succeeded",
    "failed",
)

PARSE_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 已受理，排队中
    "pending": {"value": "pending", "label": "排队中", "severity": "neutral", "active": True},
    # 引擎正在解析
    "running": {"value": "running", "label": "解析中", "severity": "progress", "active": True},
    # 引擎已完成，产品层正在归档结果
    "archiving": {"value": "archiving", "label": "归档中", "severity": "progress", "active": True},
    # 解析完成且结果已可取
    "succeeded": {"value": "succeeded", "label": "已完成", "severity": "ok"},
    # 解析失败，error 里有原因
    "failed": {"value": "failed", "label": "失败", "severity": "error"},
}

# 契约 openapi_v1 只承诺这几个值
PARSE_STATUS_OPENAPI_V1: Final[tuple[str, ...]] = ("pending", "running", "succeeded", "failed",)


def parse_status_label(value: str | None) -> str | None:
    """parse_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = PARSE_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 向量索引状态。索引失败必须能在 UI 上看到，不许静默。
IndexStatus = Literal["none", "pending", "indexing", "ready", "failed"]

INDEX_STATUS_VALUES: Final[tuple[str, ...]] = (
    "none",
    "pending",
    "indexing",
    "ready",
    "failed",
)

INDEX_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 还没建过索引
    "none": {"value": "none", "label": "未索引", "severity": "neutral"},
    # 已排队等待索引
    "pending": {"value": "pending", "label": "待索引", "severity": "neutral", "active": True},
    # 正在建索引
    "indexing": {"value": "indexing", "label": "索引中", "severity": "progress", "active": True},
    # 索引可用，可以问答
    "ready": {"value": "ready", "label": "可问答", "severity": "ok"},
    # 索引失败，index_error 里有原因
    "failed": {"value": "failed", "label": "索引失败", "severity": "error"},
}


def index_status_label(value: str | None) -> str | None:
    """index_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = INDEX_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 版面编译状态。**索引 ready 不代表视觉理解完整** —— 编译状态与降级
# 必须单列并在前端展示。
CompileStatus = Literal["none", "pending", "compiling", "ready", "partial", "failed"]

COMPILE_STATUS_VALUES: Final[tuple[str, ...]] = (
    "none",
    "pending",
    "compiling",
    "ready",
    "partial",
    "failed",
)

COMPILE_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 还没编译
    "none": {"value": "none", "label": "未编译", "severity": "neutral"},
    # 已排队等待编译
    "pending": {"value": "pending", "label": "待编译", "severity": "neutral", "active": True},
    # 正在编译
    "compiling": {"value": "compiling", "label": "编译中", "severity": "progress", "active": True},
    # 编译完整、无降级
    "ready": {"value": "ready", "label": "编译完整", "severity": "ok"},
    # 编译完成但有降级，见 compile_degraded
    "partial": {"value": "partial", "label": "编译有降级", "severity": "warn"},
    # 编译失败
    "failed": {"value": "failed", "label": "编译失败", "severity": "error"},
}


def compile_status_label(value: str | None) -> str | None:
    """compile_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = COMPILE_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 抽取批次状态。`partial` **不是"有点问题"的委婉说法**：它明确表示
# 必填字段没抽全，或批次里个别文档失败。一批 200 份里有 3 份失败
# 报成"成功"，会让人直接拿去用。
RunStatus = Literal["pending", "running", "succeeded", "partial", "failed"]

RUN_STATUS_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "running",
    "succeeded",
    "partial",
    "failed",
)

RUN_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 已受理，排队中
    "pending": {"value": "pending", "label": "排队中", "severity": "neutral", "active": True},
    # 正在抽取
    "running": {"value": "running", "label": "抽取中", "severity": "progress", "active": True},
    # 全部文档全部字段都完成
    "succeeded": {"value": "succeeded", "label": "已完成", "severity": "ok"},
    # 部分文档或部分字段失败
    "partial": {"value": "partial", "label": "部分完成", "severity": "warn"},
    # 整批失败
    "failed": {"value": "failed", "label": "失败", "severity": "error"},
}


def run_status_label(value: str | None) -> str | None:
    """run_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = RUN_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# DDP-Extract 的字段三态。**必须分开对待**：`not_found` 是"我们看过了，
# 文档里确实没有"，是一种正确答案；`error` 才是系统问题。
# 界面上 not_found 绝不能显示成空白或 "—"（那让人以为是没渲染出来），
# error 也绝不能显示成"未提及"（那是把系统故障伪装成事实）。
FieldStatus = Literal["found", "not_found", "error"]

FIELD_STATUS_VALUES: Final[tuple[str, ...]] = (
    "found",
    "not_found",
    "error",
)

FIELD_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 抽到了值，且有出处
    "found": {"value": "found", "label": "已抽取", "severity": "ok"},
    # 文档里确实没有这个字段
    "not_found": {"value": "not_found", "label": "文档中未提及", "severity": "neutral"},
    # 抽取过程本身出错
    "error": {"value": "error", "label": "抽取失败", "severity": "error"},
}


def field_status_label(value: str | None) -> str | None:
    """field_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = FIELD_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 代码块识别的来源。启发式与原生要分开，因为它决定了代码检索的可信度。
CodeDetection = Literal["native", "heuristic", "unavailable"]

CODE_DETECTION_VALUES: Final[tuple[str, ...]] = (
    "native",
    "heuristic",
    "unavailable",
)

CODE_DETECTION_META: Final[dict[str, EnumMeta]] = {
    # 版面引擎直接报出了 code 块
    "native": {"value": "native", "label": "代码识别：原生", "severity": "ok"},
    # 靠启发式规则判出来的
    "heuristic": {"value": "heuristic", "label": "代码识别：启发式", "severity": "neutral"},
    # 当前引擎识别不了代码块
    "unavailable": {"value": "unavailable", "label": "代码识别：不可用", "severity": "warn"},
}


def code_detection_label(value: str | None) -> str | None:
    """code_detection 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = CODE_DETECTION_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 证据是原文还是生成物。**第三条不变式**：生成物与原文必须可区分，
# 且生成物的引用最终仍要指回原始原子 bbox（`derived_from`）。
# 判据是 `evidence.derived_from` 是否为空 —— 不要在别处另立标志位。
SourceType = Literal["source", "generated"]

SOURCE_TYPE_VALUES: Final[tuple[str, ...]] = (
    "source",
    "generated",
)

SOURCE_TYPE_META: Final[dict[str, EnumMeta]] = {
    # 直接来自版面的原子（derived_from 为空）
    "source": {"value": "source", "label": "原文", "severity": "neutral"},
    # 模型生成的理解（derived_from 指向原子）
    "generated": {"value": "generated", "label": "生成理解", "severity": "warn"},
}


def source_type_label(value: str | None) -> str | None:
    """source_type 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = SOURCE_TYPE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# DDP-Layout v1.1 的块类型词汇表 —— **契约的一部分**。
# 每个引擎的 normalizer 都必须产出这八个值之一；认不出来的归 `other`
# （不是丢弃 —— 丢弃会让新引擎的块凭空消失）。
# 规范实现在 `ddp_core.blocks.normalize_type`，守卫在
# `scripts/check_blocktype_parity.py`。
BlockType = Literal["text", "title", "code", "table", "figure", "equation", "list", "other"]

BLOCK_TYPE_VALUES: Final[tuple[str, ...]] = (
    "text",
    "title",
    "code",
    "table",
    "figure",
    "equation",
    "list",
    "other",
)

BLOCK_TYPE_META: Final[dict[str, EnumMeta]] = {
    # 正文段落。也是"压根没有 type"时的默认
    "text": {"value": "text", "label": "正文", "severity": "neutral"},
    # 各级标题
    "title": {"value": "title", "label": "标题", "severity": "neutral"},
    # 代码块
    "code": {"value": "code", "label": "代码", "severity": "neutral"},
    # 表格（table_html 可能有值）
    "table": {"value": "table", "label": "表格", "severity": "neutral"},
    # 图。**无 caption 也要产出原子**，否则视觉链路没输入
    "figure": {"value": "figure", "label": "图", "severity": "neutral"},
    # 行间公式
    "equation": {"value": "equation", "label": "公式", "severity": "neutral"},
    # 列表
    "list": {"value": "list", "label": "列表", "severity": "neutral"},
    # 有 type 但不在映射表里 —— 与「压根没有 type」要分开，后者归 text
    "other": {"value": "other", "label": "其它", "severity": "neutral"},
}


def block_type_label(value: str | None) -> str | None:
    """block_type 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = BLOCK_TYPE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 计量流水的种类。`extract` 按**字段数**计 requests：一次抽取 = N 次检索
# + N 次模型调用，按"一次请求"计费会让 60 字段的 schema 和 1 字段的一样便宜。
UsageKind = Literal["parse", "chat", "embeddings", "mcp", "qa", "embed", "compile_vision", "extract", "knowledge"]

USAGE_KIND_VALUES: Final[tuple[str, ...]] = (
    "parse",
    "chat",
    "embeddings",
    "mcp",
    "qa",
    "embed",
    "compile_vision",
    "extract",
    "knowledge",
)

USAGE_KIND_META: Final[dict[str, EnumMeta]] = {
    # 文档解析，按页计
    "parse": {"value": "parse", "label": "解析", "severity": "neutral"},
    # 对外 chat 代理，按次计
    "chat": {"value": "chat", "label": "对话", "severity": "neutral"},
    # 对外向量化代理
    "embeddings": {"value": "embeddings", "label": "向量化", "severity": "neutral"},
    # MCP 工具调用
    "mcp": {"value": "mcp", "label": "MCP 调用", "severity": "neutral"},
    # 站内问答
    "qa": {"value": "qa", "label": "问答", "severity": "neutral"},
    # 索引时的向量化
    "embed": {"value": "embed", "label": "索引向量化", "severity": "neutral"},
    # 编译期的视觉理解调用
    "compile_vision": {"value": "compile_vision", "label": "视觉理解", "severity": "neutral"},
    # 结构化抽取，按字段数计
    "extract": {"value": "extract", "label": "结构化抽取", "severity": "neutral"},
    # 图谱 / wiki 生成，按次计
    "knowledge": {"value": "knowledge", "label": "知识生成", "severity": "neutral"},
}


def usage_kind_label(value: str | None) -> str | None:
    """usage_kind 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = USAGE_KIND_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 调用者身份类型。corpus-api **不自己验用户凭据**，它只信任 control-api
# 在内部调用里下发的 `X-DDP-Actor-Kind` + `X-DDP-Actor`。
ActorKind = Literal["user", "api_key", "service"]

ACTOR_KIND_VALUES: Final[tuple[str, ...]] = (
    "user",
    "api_key",
    "service",
)

ACTOR_KIND_META: Final[dict[str, EnumMeta]] = {
    # 浏览器会话（JWT / OIDC）
    "user": {"value": "user", "label": "用户", "severity": "neutral"},
    # sk- 开头的对外 key
    "api_key": {"value": "api_key", "label": "API Key", "severity": "neutral"},
    # 服务间调用（服务凭据）
    "service": {"value": "service", "label": "服务", "severity": "neutral"},
}


def actor_kind_label(value: str | None) -> str | None:
    """actor_kind 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = ACTOR_KIND_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 组织内角色（RBAC）。**首发是单组织独占部署**，一次部署 = 一份语料，
# 组织内成员共享语料；角色控制的是"能做什么"，不是"能看见什么"。
Role = Literal["viewer", "contributor", "reviewer", "admin"]

ROLE_VALUES: Final[tuple[str, ...]] = (
    "viewer",
    "contributor",
    "reviewer",
    "admin",
)

ROLE_META: Final[dict[str, EnumMeta]] = {
    # 只读：检索、问答、看证据
    "viewer": {"value": "viewer", "label": "只读成员", "severity": "neutral"},
    # viewer + 上传、重解析、发起抽取
    "contributor": {"value": "contributor", "label": "贡献者", "severity": "neutral"},
    # contributor + 复核队列、确认/驳回知识条目
    "reviewer": {"value": "reviewer", "label": "复核员", "severity": "neutral"},
    # 全部 + 成员管理、API key、配额、删除
    "admin": {"value": "admin", "label": "管理员", "severity": "neutral"},
}


def role_label(value: str | None) -> str | None:
    """role 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = ROLE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 持久任务的状态机（§10）。**领取必须带 generation fencing**：
# lease 只解决"谁可以接管"，最终写入还要比 generation —— 否则被判死的
# 旧 worker 迟到写入会覆盖新结果。
TaskStatus = Literal["queued", "claimed", "running", "succeeded", "failed", "cancelled"]

TASK_STATUS_VALUES: Final[tuple[str, ...]] = (
    "queued",
    "claimed",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)

TASK_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 已落库等待领取
    "queued": {"value": "queued", "label": "排队中", "severity": "neutral", "active": True},
    # 已被某个 worker 领取（带 lease_until）
    "claimed": {"value": "claimed", "label": "已领取", "severity": "progress", "active": True},
    # 正在执行，靠 heartbeat 续租
    "running": {"value": "running", "label": "执行中", "severity": "progress", "active": True},
    # 完成
    "succeeded": {"value": "succeeded", "label": "已完成", "severity": "ok"},
    # 失败，失败原因必须持久化并在 UI 可见
    "failed": {"value": "failed", "label": "失败", "severity": "error"},
    # 被显式取消。**终态，迟到的成功/失败写入一律被 generation + 状态守卫拒绝**。
    # 与 failed 分开是因为"用户不想要了"和"系统做砸了"对用户是两件事：
    # 前者不该进失败告警，后者必须留失败原因。
    "cancelled": {"value": "cancelled", "label": "已取消", "severity": "warn"},
}


def task_status_label(value: str | None) -> str | None:
    """task_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = TASK_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 持久任务的种类。每种**分别设并发与队列**，不共用一个无量纲总并发。
TaskKind = Literal["parse_poll", "compile", "index", "extract", "knowledge", "gc", "federation_execute", "federation_plan"]

TASK_KIND_VALUES: Final[tuple[str, ...]] = (
    "parse_poll",
    "compile",
    "index",
    "extract",
    "knowledge",
    "gc",
    "federation_execute",
    "federation_plan",
)

TASK_KIND_META: Final[dict[str, EnumMeta]] = {
    # 轮询解析引擎并归档结果
    "parse_poll": {"value": "parse_poll", "label": "解析归档", "severity": "neutral"},
    # 版面编译（含视觉理解）
    "compile": {"value": "compile", "label": "版面编译", "severity": "neutral"},
    # 分块 + 向量化 + 写索引
    "index": {"value": "index", "label": "建立索引", "severity": "neutral"},
    # 结构化抽取批次
    "extract": {"value": "extract", "label": "结构化抽取", "severity": "neutral"},
    # 图谱 / wiki 生成
    "knowledge": {"value": "knowledge", "label": "知识生成", "severity": "neutral"},
    # 对象回收（带宽限期）
    "gc": {"value": "gc", "label": "对象回收", "severity": "neutral"},
    # 联邦节点侧的单步执行（`federation.execute`）。受理与执行行先提交、
    # 再排这个任务 —— 进程重启后由别的 worker 按租约接管，已受理的执行
    # 不会永远停在 queued/running（不变式 7）。
    "federation_execute": {"value": "federation_execute", "label": "联邦执行", "severity": "neutral"},
    # 联邦协调者推进一个已批准计划（`federation_tasks._execute_plan`）。
    # 与节点侧分开成两种任务，协调者等待本地执行时不会占满执行池
    # （否则单池会被"等子任务的父任务"堵死）。
    "federation_plan": {"value": "federation_plan", "label": "联邦计划执行", "severity": "neutral"},
}


def task_kind_label(value: str | None) -> str | None:
    """task_kind 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = TASK_KIND_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 直传上传会话的状态（§9.1）。**`verifying` 不能跳过**：服务端没校验完
# 对象大小与摘要之前，文档不得进入解析 —— 否则等于信任客户端声明的哈希。
UploadStatus = Literal["created", "uploading", "verifying", "ready", "failed", "expired"]

UPLOAD_STATUS_VALUES: Final[tuple[str, ...]] = (
    "created",
    "uploading",
    "verifying",
    "ready",
    "failed",
    "expired",
)

UPLOAD_STATUS_META: Final[dict[str, EnumMeta]] = {
    # 会话已创建，预签名已下发
    "created": {"value": "created", "label": "待上传", "severity": "neutral", "active": True},
    # 客户端正在分片上传
    "uploading": {"value": "uploading", "label": "上传中", "severity": "progress", "active": True},
    # 已 finalize，服务端正在校验摘要
    "verifying": {"value": "verifying", "label": "校验中", "severity": "progress", "active": True},
    # 校验通过，已发出 DocumentSubmitted
    "ready": {"value": "ready", "label": "已就绪", "severity": "ok"},
    # 校验失败或客户端放弃
    "failed": {"value": "failed", "label": "失败", "severity": "error"},
    # 预签名过期未完成
    "expired": {"value": "expired", "label": "已过期", "severity": "warn"},
}


def upload_status_label(value: str | None) -> str | None:
    """upload_status 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = UPLOAD_STATUS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# `ScopeManifest` 的成员枚举状态（计划 §5.4）。**这是「查了哪里」这句话
# 的分母**：分母没封上就没有百分比可言。
#
# `partial` 与 `expired` 必须与 `sealed` 严格分开：把无法展开的子域
# 当成空目录，等于用"那里没有资料"冒充"我没能去看"。
EnumerationState = Literal["building", "sealed", "partial", "expired"]

ENUMERATION_STATE_VALUES: Final[tuple[str, ...]] = (
    "building",
    "sealed",
    "partial",
    "expired",
)

ENUMERATION_STATE_META: Final[dict[str, EnumMeta]] = {
    # 正在逐个目录取分页快照，还没封存
    "building": {"value": "building", "label": "正在确定检索范围", "severity": "progress", "active": True},
    # 全部获准目录都取到稳定快照且已去重封存，可重放。
    # **只有这个值允许后续声明 retrieval=complete。**
    "sealed": {"value": "sealed", "label": "检索范围已确定", "severity": "ok"},
    # 有子目录超时、拒绝或不支持枚举。未展开子域记在
    # `unexpanded_subtrees[]`，**不得当成空集**，也不得给出真实总数。
    "partial": {"value": "partial", "label": "检索范围不完整（部分下级目录无法展开）", "severity": "warn"},
    # 快照有效期已过或枚举游标失效。不能把不同分页时代的列表拼成
    # "完整快照"（§5.5）—— 要重新枚举生成新 scope。
    "expired": {"value": "expired", "label": "检索范围已过期，需重新确定", "severity": "warn"},
}


def enumeration_state_label(value: str | None) -> str | None:
    """enumeration_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = ENUMERATION_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 覆盖账本里**单个目标**的状态（计划 §7.4）。目标键是
# `(origin_node_id, collection_id, operation)` —— 一台服务器有多个集合时，
# 探测了其中一个**不能**把整台标成完成（计划 T85）。
#
# `unsupported` 可以结束对该目标的发现处理，但**它不表示在那里完成了
# 全文检索**；报告时它进"排除数"，不进"成功检索数"。
CoverageTargetState = Literal["planned", "in_flight", "succeeded", "partial", "denied", "failed", "unsupported", "unreachable", "not_attempted", "revoked"]

COVERAGE_TARGET_STATE_VALUES: Final[tuple[str, ...]] = (
    "planned",
    "in_flight",
    "succeeded",
    "partial",
    "denied",
    "failed",
    "unsupported",
    "unreachable",
    "not_attempted",
    "revoked",
)

COVERAGE_TARGET_STATE_META: Final[dict[str, EnumMeta]] = {
    # 已进入本次范围，尚未发出请求
    "planned": {"value": "planned", "label": "待检索", "severity": "neutral", "active": True},
    # 请求已发出，还没有回执
    "in_flight": {"value": "in_flight", "label": "检索中", "severity": "progress", "active": True},
    # 拿到有效且完成的检索回执
    "succeeded": {"value": "succeeded", "label": "已检索", "severity": "ok"},
    # 目标自己报了内部限制（分片失败、索引落后、只查了子集）。
    # **算缺口，不算完成** —— 节点外层写 completed 而内部有 partial
    # 是计划 §6.4 明确禁止的。
    "partial": {"value": "partial", "label": "部分检索（对方报告内部不完整）", "severity": "warn"},
    # 鉴权通过但该目标拒绝本次操作
    "denied": {"value": "denied", "label": "对方拒绝", "severity": "warn"},
    # 请求出错（非超时）
    "failed": {"value": "failed", "label": "检索失败", "severity": "error"},
    # 已核实该目标不支持所需 operation。**只有可核验依据才能记这个值** ——
    # 能力元数据过期或缺失一律算 unknown/未完成，不得直接排除（§7.3）。
    "unsupported": {"value": "unsupported", "label": "对方不支持该操作", "severity": "neutral"},
    # 超时或连不上
    "unreachable": {"value": "unreachable", "label": "无法连接", "severity": "error"},
    # 预算耗尽 / 任务取消 / 范围过期导致压根没发出。**不是"没有资料"**
    "not_attempted": {"value": "not_attempted", "label": "未检索（预算或取消）", "severity": "warn"},
    # 成员在范围封存后被撤销。**留在分母里**（§5.5）——
    # 从分母删掉来把完成率做漂亮是明确禁止的。
    "revoked": {"value": "revoked", "label": "成员已撤销（保留在范围内）", "severity": "warn"},
}


def coverage_target_state_label(value: str | None) -> str | None:
    """coverage_target_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = COVERAGE_TARGET_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 整个任务的检索完成度（计划 §7.4）。
#
# `complete` 的判据是**合取**，缺一条都不许写：
#   ① `enumeration_state == sealed` 且无未展开子域；
#   ② 所有适用且已授权的目标都返回有效、完成的回执；
#   ③ 没有 in_flight / not_attempted / unreachable / denied / revoked，
#      也没有任何目标自报 partial；
#   ④ 每个被排除的目标都有可核验依据。
#
# 快速模式**永远不允许**写 complete（§7.2）：它只完成了自己选中的候选，
# 所以它报的是 `partial` 加上"未检索范围"。
RetrievalCompleteness = Literal["not_started", "partial", "complete"]

RETRIEVAL_COMPLETENESS_VALUES: Final[tuple[str, ...]] = (
    "not_started",
    "partial",
    "complete",
)

RETRIEVAL_COMPLETENESS_META: Final[dict[str, EnumMeta]] = {
    # 范围还没封存或还没开始检索
    "not_started": {"value": "not_started", "label": "尚未检索", "severity": "neutral", "active": True},
    # 有目标未完成，或本轮是 fast 模式。**fast 模式的成功结局也是这个值**
    # —— 它必须同时给出未检索范围，不能因为选中的候选全成功就报完成。
    "partial": {"value": "partial", "label": "部分范围已检索", "severity": "warn"},
    # 上述四条合取全部成立。**这仍然不代表证据充分或结论正确。**
    "complete": {"value": "complete", "label": "声明范围内已全部检索", "severity": "ok"},
}


def retrieval_completeness_label(value: str | None) -> str | None:
    """retrieval_completeness 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = RETRIEVAL_COMPLETENESS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 证据充分性（计划 §7.4 第三轴）。与检索完成度**严格分开**：
# 「该查的都查了」和「查到的够回答」是两件事，而
# 「够回答」和「答对了」又是第三件事（那一件靠人工评审，不进这个枚举）。
#
# `conflicting` 不是 `insufficient` 的变体：矛盾证据意味着拿到了实质内容
# 但来源互相打架，界面上要让用户看见冲突，而不是折叠成"资料不足"。
EvidenceSufficiency = Literal["sufficient_by_policy", "insufficient", "conflicting", "unknown"]

EVIDENCE_SUFFICIENCY_VALUES: Final[tuple[str, ...]] = (
    "sufficient_by_policy",
    "insufficient",
    "conflicting",
    "unknown",
)

EVIDENCE_SUFFICIENCY_META: Final[dict[str, EnumMeta]] = {
    # 按当次策略判定证据足够。名字里的 `by_policy` 是刻意的 ——
    # 它是**按规则判的**，不是"客观上充分"，更不是 LLM 自报信心
    # （§7.2 明确禁止把自报信心当唯一早停条件）。
    "sufficient_by_policy": {"value": "sufficient_by_policy", "label": "证据满足本次策略要求", "severity": "ok"},
    # 没有足够证据支撑结论，必须如实说不足
    "insufficient": {"value": "insufficient", "label": "证据不足", "severity": "warn"},
    # 多来源证据互相矛盾（含同一资料的不同版本）。要展示冲突，不要挑一个。优先级低于 insufficient / unknown：证据本身不足时报不足，矛盾记录照样保留
    "conflicting": {"value": "conflicting", "label": "证据存在矛盾", "severity": "warn"},
    # 还没评估（检索未完成 / 评估器不可用）
    "unknown": {"value": "unknown", "label": "证据充分性未知", "severity": "neutral"},
}


def evidence_sufficiency_label(value: str | None) -> str | None:
    """evidence_sufficiency 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = EVIDENCE_SUFFICIENCY_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 一条证据矛盾记录**凭什么**成立（计划 §7.6：同时处理矛盾证据，不挑一个）。
# 两种依据都只能把 `sufficient_by_policy` 压成 `conflicting`，**不能**抬高它，
# 也**不能**把 `insufficient` / `unknown` 改写成 `conflicting`（那会藏掉"证据不足"
# 并放行不该发生的生成）—— 这两种情况下矛盾记录照样保留、照样可见。
# 模型说"有矛盾"最多让界面多一个警告，而模型说"没矛盾"不改变任何东西。
# 每条记录都要人看（`semantic_review=needs_review`），这里记的是"值得复核的
# 矛盾"，不是裁决。
EvidenceConflictBasis = Literal["version_divergence", "generation_reported"]

EVIDENCE_CONFLICT_BASIS_VALUES: Final[tuple[str, ...]] = (
    "version_divergence",
    "generation_reported",
)

EVIDENCE_CONFLICT_BASIS_META: Final[dict[str, EnumMeta]] = {
    # 规则判定：同一来源（同节点、同资源）的不同固定版本在**同一定位**
    # （物理页 + 块序）上取回了不同正文。只看结构，不读语义。
    "version_divergence": {"value": "version_divergence", "label": "同一资料的版本不一致", "severity": "warn"},
    # 带出处生成时模型标出的矛盾引用对。只在引用全部落在本次证据编号域、
    # 且至少指向两条不同证据时才采信；引用不成立则整份答案作废。
    "generation_reported": {"value": "generation_reported", "label": "生成时标出的矛盾", "severity": "warn"},
}


def evidence_conflict_basis_label(value: str | None) -> str | None:
    """evidence_conflict_basis 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = EVIDENCE_CONFLICT_BASIS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 联邦任务结果里 `answer_reason` 的取值：**为什么这次没有答案**（有答案时为 null）。
# 它和 `evidence_sufficiency` 分开：充分性说"证据够不够"，这里说"答案这一步
# 具体卡在哪" —— 证据充分但模型没装、远端答案引用越界、预算超了，都要让用户
# 看得出区别，而不是统一显示成"没有答案"。
#
# **代码是封闭集合，细节是后缀**：其中五个代码允许带 `:细节`
# （`receipt_binding_mismatch:plan_digest`、`delegated_execution_failed:peer_execution_timeout`、
# `delegated_admission_not_accepted:waiting_input`、`delegated_answer_rejected:…`、
# `peer_unavailable:http_503`），细节是对端的状态/错误码或出错字段，经过字符集与长度
# 清洗；其余代码不带后缀。对端给的字符串**永远只进细节**，不会变成新的代码。
# 查文案前先去掉冒号后缀；协调者写出之前会检查代码已声明（`federation.unavailable_answer`）。
FederatedAnswerReason = Literal["insufficient_evidence", "local_model_missing", "evidence_excerpt_unavailable", "excerpt_over_contract_bound", "upstream_error", "no_model_output", "budget_exceeded", "unsupported_generation", "delegated_answer_missing", "delegated_answer_rejected", "delegated_bindings_missing", "delegated_binding_out_of_scope", "delegated_conflict_out_of_scope", "evidence_delegation_over_limit", "invalid_admission_receipt", "receipt_binding_mismatch", "delegated_admission_not_accepted", "delegated_execution_failed", "peer_unavailable"]

FEDERATED_ANSWER_REASON_VALUES: Final[tuple[str, ...]] = (
    "insufficient_evidence",
    "local_model_missing",
    "evidence_excerpt_unavailable",
    "excerpt_over_contract_bound",
    "upstream_error",
    "no_model_output",
    "budget_exceeded",
    "unsupported_generation",
    "delegated_answer_missing",
    "delegated_answer_rejected",
    "delegated_bindings_missing",
    "delegated_binding_out_of_scope",
    "delegated_conflict_out_of_scope",
    "evidence_delegation_over_limit",
    "invalid_admission_receipt",
    "receipt_binding_mismatch",
    "delegated_admission_not_accepted",
    "delegated_execution_failed",
    "peer_unavailable",
)

FEDERATED_ANSWER_REASON_META: Final[dict[str, EnumMeta]] = {
    # 证据不足（或没有可引用证据），不给模型凭常识补答的机会
    "insufficient_evidence": {"value": "insufficient_evidence", "label": "证据不足，未生成答案", "severity": "warn"},
    # 本节点与计划内远端都没有可用的生成能力（或生成预算为 0）
    "local_model_missing": {"value": "local_model_missing", "label": "没有可用的生成模型，只返回证据", "severity": "warn"},
    # 某条证据取不到正文（空白或缺失），不能拿无根片段生成
    "evidence_excerpt_unavailable": {"value": "evidence_excerpt_unavailable", "label": "有证据取不到原文片段，未生成答案", "severity": "warn"},
    # 证据正文超过契约上限（2000 字符），显式拒绝而不是静默截断
    "excerpt_over_contract_bound": {"value": "excerpt_over_contract_bound", "label": "证据片段超出长度上限，未生成答案", "severity": "warn"},
    # 调生成模型的请求失败或返回非 200
    "upstream_error": {"value": "upstream_error", "label": "生成服务出错，只返回证据", "severity": "error"},
    # 模型没有返回可用文本
    "no_model_output": {"value": "no_model_output", "label": "模型没有输出，只返回证据", "severity": "error"},
    # 生成结果超出计划的生成 token 预算
    "budget_exceeded": {"value": "budget_exceeded", "label": "超出生成预算，答案作废", "severity": "error"},
    # 生成文本的引用结构不成立（无引用、越界引用、矛盾标注不成立）
    "unsupported_generation": {"value": "unsupported_generation", "label": "生成的答案引用不成立，已作废", "severity": "error"},
    # 远端执行完成但没有返回答案文档
    "delegated_answer_missing": {"value": "delegated_answer_missing", "label": "远端没有返回答案", "severity": "error"},
    # 远端答案校验未通过；远端自报的原因认不出来时放进细节
    "delegated_answer_rejected": {"value": "delegated_answer_rejected", "label": "远端答案未通过校验", "severity": "error"},
    # 远端答案没有任何主张绑定
    "delegated_bindings_missing": {"value": "delegated_bindings_missing", "label": "远端答案没有引用，已作废", "severity": "error"},
    # 远端答案的引用不在本次发送的证据里
    "delegated_binding_out_of_scope": {"value": "delegated_binding_out_of_scope", "label": "远端答案引用了未发送的证据，已作废", "severity": "error"},
    # 远端标出的矛盾引用不在本次发送的证据里
    "delegated_conflict_out_of_scope": {"value": "delegated_conflict_out_of_scope", "label": "远端标注的矛盾引用不成立，答案已作废", "severity": "error"},
    # 要委托的证据条数超过受理上限，不截断证据去凑数
    "evidence_delegation_over_limit": {"value": "evidence_delegation_over_limit", "label": "证据条数超过委托上限，未生成答案", "severity": "warn"},
    # 远端受理回执缺执行任务号
    "invalid_admission_receipt": {"value": "invalid_admission_receipt", "label": "远端受理回执无效", "severity": "error"},
    # 远端回执与本次 root/step/幂等键/计划修订/执行者对不上；细节是出错字段（回执不是对象时为 schema）
    "receipt_binding_mismatch": {"value": "receipt_binding_mismatch", "label": "远端回执与本次任务对不上，未采用", "severity": "error"},
    # 远端没有受理答案步骤；细节是回执状态（如 waiting_input / rejected）
    "delegated_admission_not_accepted": {"value": "delegated_admission_not_accepted", "label": "远端未受理生成请求", "severity": "error"},
    # 远端答案执行没有成功；细节是对端错误码或状态（含本节点轮询超时 peer_execution_timeout）
    "delegated_execution_failed": {"value": "delegated_execution_failed", "label": "远端生成步骤未完成", "severity": "error"},
    # 远端生成节点未登记、连不上、回 HTTP 错误或返回非法响应；细节是对端错误码、http_状态或 transport
    "peer_unavailable": {"value": "peer_unavailable", "label": "远端生成节点不可用", "severity": "error"},
}


def federated_answer_reason_label(value: str | None) -> str | None:
    """federated_answer_reason 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = FEDERATED_ANSWER_REASON_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 协调者入口（`POST /api/v1/task-intents`）**受理哪些 TaskSpec.operation**。
# 这是一个闭集：认不出来的 operation 当场拒绝，不许落库。
#
# 为什么必须闭集：规划只按 operation 决定要不要加生成步骤。以前不看 operation，
# 本地模型就绪时**任何** operation 都会被追加一个 `answer` 步 —— 提交
# `corpus.retrieve`（只取证据）会白跑一次生成，提交一个没实现的 operation
# （例如 `wiki.pages`）会拿回一个 RAG 答案。那是静默错义，不是报错。
#
# 本地运行时的 TaskSpec 还有别的 operation（本机自己的计划许可），不受这里约束。
FederationTaskOperation = Literal["corpus.retrieve", "rag.answer.cited"]

FEDERATION_TASK_OPERATION_VALUES: Final[tuple[str, ...]] = (
    "corpus.retrieve",
    "rag.answer.cited",
)

FEDERATION_TASK_OPERATION_META: Final[dict[str, EnumMeta]] = {
    # 只按范围取证据，不生成结论
    "corpus.retrieve": {"value": "corpus.retrieve", "label": "只取证据", "severity": "neutral"},
    # 取证据并生成带出处的回答
    "rag.answer.cited": {"value": "rag.answer.cited", "label": "带出处的回答", "severity": "neutral"},
}


def federation_task_operation_label(value: str | None) -> str | None:
    """federation_task_operation 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = FEDERATION_TASK_OPERATION_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 联邦任务事件流（`GET /api/v1/tasks/{root_task_id}/events`）里 `Event.type` 的取值。
# 事件是**可恢复的进度记录**，不是状态真相：状态以 `TaskStatus` 各轴为准，事件用来
# 让界面说清"发生了什么、什么时候"，断线后按 `after=next_seq` 续读不丢。
TaskEventType = Literal["intent_created", "plan_ready", "plan_approved", "execution_started", "task_resumed", "task_completed", "task_failed", "task_cancelled", "delivery_pending", "delivery_confirmed", "delivery_expired"]

TASK_EVENT_TYPE_VALUES: Final[tuple[str, ...]] = (
    "intent_created",
    "plan_ready",
    "plan_approved",
    "execution_started",
    "task_resumed",
    "task_completed",
    "task_failed",
    "task_cancelled",
    "delivery_pending",
    "delivery_confirmed",
    "delivery_expired",
)

TASK_EVENT_TYPE_META: Final[dict[str, EnumMeta]] = {
    # 任务需求与探索许可已落库
    "intent_created": {"value": "intent_created", "label": "已创建任务", "severity": "neutral"},
    # 规划完成（Probe 与计划修订已生成），等待批准
    "plan_ready": {"value": "plan_ready", "label": "计划已生成，等待批准", "severity": "neutral"},
    # 用户批准了这一修订与执行许可
    "plan_approved": {"value": "plan_approved", "label": "已批准计划", "severity": "ok"},
    # 执行已受理并排入持久队列
    "execution_started": {"value": "execution_started", "label": "开始执行", "severity": "progress"},
    # 重新判权后补做未完成目标（执行代次 +1）
    "task_resumed": {"value": "task_resumed", "label": "补做未完成目标", "severity": "progress"},
    # 执行结束且至少有目标产出证据（查全与否看覆盖账本）
    "task_completed": {"value": "task_completed", "label": "执行结束", "severity": "ok"},
    # 执行失败（没有任何目标产出证据，或协调者被清扫）
    "task_failed": {"value": "task_failed", "label": "执行失败", "severity": "error"},
    # 用户显式取消；终态，迟到结果不许覆盖
    "task_cancelled": {"value": "task_cancelled", "label": "已取消", "severity": "warn"},
    # 结果已固化为交付文档，等待下载后校验确认
    "delivery_pending": {"value": "delivery_pending", "label": "结果待确认", "severity": "neutral"},
    # 客户端校验摘要后确认了交付
    "delivery_confirmed": {"value": "delivery_confirmed", "label": "结果已确认", "severity": "ok"},
    # 交付在有效期内没有被确认
    "delivery_expired": {"value": "delivery_expired", "label": "结果交付已过期", "severity": "warn"},
}


def task_event_type_label(value: str | None) -> str | None:
    """task_event_type 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = TASK_EVENT_TYPE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# TaskPlan 的规划轴（计划 §8.1）。`invalidated` 是关键一态：
# 计划过期、输入版本变了、授权被撤销之后，**旧计划不许被执行**，
# 要重新规划并重新批准（§6.6 接单时重新检查）。
PlanningState = Literal["draft", "exploring", "ready", "awaiting_approval", "approved", "invalidated"]

PLANNING_STATE_VALUES: Final[tuple[str, ...]] = (
    "draft",
    "exploring",
    "ready",
    "awaiting_approval",
    "approved",
    "invalidated",
)

PLANNING_STATE_META: Final[dict[str, EnumMeta]] = {
    # TaskSpec 已建，还没探测
    "draft": {"value": "draft", "label": "草稿", "severity": "neutral", "active": True},
    # 已获探索许可，正在 Probe
    "exploring": {"value": "exploring", "label": "正在探测", "severity": "progress", "active": True},
    # 计划已生成，等待用户批准外发边界
    "ready": {"value": "ready", "label": "计划待批准", "severity": "neutral", "active": True},
    # 计划变化超出原许可，暂停等重新批准
    "awaiting_approval": {"value": "awaiting_approval", "label": "等待重新批准", "severity": "warn", "active": True},
    # 计划与外发边界都已批准，可以接单
    "approved": {"value": "approved", "label": "已批准", "severity": "ok"},
    # 计划过期、输入版本变更或授权撤销。**不得凭旧 Probe 放行**
    "invalidated": {"value": "invalidated", "label": "计划已失效，需重新规划", "severity": "warn"},
}


def planning_state_label(value: str | None) -> str | None:
    """planning_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = PLANNING_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 远端执行者的受理轴（计划 §8.1 / §6.6）。
#
# **`unknown` 不等于「没执行」** —— 这是计划 T82 专门要求的区分：
# 回执丢了要先按幂等键对账，不能立刻把有副作用的步骤换个节点重做。
AdmissionState = Literal["not_submitted", "waiting_input", "checking", "accepted", "rejected", "unknown"]

ADMISSION_STATE_VALUES: Final[tuple[str, ...]] = (
    "not_submitted",
    "waiting_input",
    "checking",
    "accepted",
    "rejected",
    "unknown",
)

ADMISSION_STATE_META: Final[dict[str, EnumMeta]] = {
    # 还没提交给执行者
    "not_submitted": {"value": "not_submitted", "label": "未提交", "severity": "neutral"},
    # 受理会话已建、等输入上传完（§6.6）。**这一态不占 GPU** ——
    # 输入没齐就排队等于占着卡等上传。
    "waiting_input": {"value": "waiting_input", "label": "等待输入上传", "severity": "progress", "active": True},
    # 服务端正在校验输入摘要与格式
    "checking": {"value": "checking", "label": "校验输入中", "severity": "progress", "active": True},
    # 已持久受理并返回 AdmissionReceipt（≠ 算力预留）
    "accepted": {"value": "accepted", "label": "已受理", "severity": "ok", "active": True},
    # 明确拒绝（授权、计划过期、输入不合格、配额）
    "rejected": {"value": "rejected", "label": "被拒绝", "severity": "error"},
    # 请求发出了但回执丢失。**必须按幂等键查询对账**，查到已有任务就用它；
    # 不得增加逻辑执行代次，也不得重复计一次成功交付。
    "unknown": {"value": "unknown", "label": "受理状态未知（正在对账）", "severity": "warn", "active": True},
}


def admission_state_label(value: str | None) -> str | None:
    """admission_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = ADMISSION_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 输出验收轴（计划 §8.1 / §4.3 两层引用校验）。
#
# **结构校验通过 ≠ 内容正确**：`passed` 只表示引用确实存在、版本对得上、
# 定位可授权解析；"原文是否真的支持这个结论"是 `needs_review`
# 要人看的那件事（计划 §14.3 主张支持度，明确不能用引用存在率替代）。
ValidationState = Literal["pending", "passed", "failed", "needs_review"]

VALIDATION_STATE_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "passed",
    "failed",
    "needs_review",
)

VALIDATION_STATE_META: Final[dict[str, EnumMeta]] = {
    # 还没校验
    "pending": {"value": "pending", "label": "待校验", "severity": "neutral", "active": True},
    # 结构校验通过：引用存在、版本正确、定位可解析
    "passed": {"value": "passed", "label": "校验通过", "severity": "ok"},
    # 结构校验不通过（虚构引用 / 错版本 / 无权定位）
    "failed": {"value": "failed", "label": "校验未通过", "severity": "error"},
    # 需要人工复核语义支持度或冲突
    "needs_review": {"value": "needs_review", "label": "需人工复核", "severity": "warn"},
}


def validation_state_label(value: str | None) -> str | None:
    """validation_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = VALIDATION_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 交付轴（计划 §8.3）。**计算成功不代表本地拿到结果。**
#
# `expired` 必须能显示出来：TTL 到期导致未领取结果失效时，界面上
# **不许**仍然显示"已保存本地"（计划 §8.3 原文要求）。
DeliveryState = Literal["not_requested", "pending", "transferring", "confirmed", "expired"]

DELIVERY_STATE_VALUES: Final[tuple[str, ...]] = (
    "not_requested",
    "pending",
    "transferring",
    "confirmed",
    "expired",
)

DELIVERY_STATE_META: Final[dict[str, EnumMeta]] = {
    # 不需要回传（结果留在中心）
    "not_requested": {"value": "not_requested", "label": "无需交付", "severity": "neutral"},
    # 结果已就绪，等待本地领取
    "pending": {"value": "pending", "label": "待领取", "severity": "neutral", "active": True},
    # 正在下载
    "transferring": {"value": "transferring", "label": "传输中", "severity": "progress", "active": True},
    # 本地校验 manifest 与文件后已幂等确认
    "confirmed": {"value": "confirmed", "label": "已交付", "severity": "ok"},
    # 暂存 TTL 到期，结果已失效。**不得显示成已保存本地**
    "expired": {"value": "expired", "label": "交付已过期（结果未领取）", "severity": "error"},
}


def delivery_state_label(value: str | None) -> str | None:
    """delivery_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = DELIVERY_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 数据保留类别（计划 §8.1 / §8.3）。**临时处理不自动进入永久语料库** ——
# 远端算一次不等于对方获得了这份资料的长期副本。
#
# `task_pinned` 是给 GC 看的：引用仍被活跃任务或他人合法产物使用时，
# GC 不能删唯一副本（计划 §8.3 末段，项目已有的 `gc.py` 宽限期同理）。
RetentionClass = Literal["temporary", "task_pinned", "persistent", "deleting", "deleted"]

RETENTION_CLASS_VALUES: Final[tuple[str, ...]] = (
    "temporary",
    "task_pinned",
    "persistent",
    "deleting",
    "deleted",
)

RETENTION_CLASS_META: Final[dict[str, EnumMeta]] = {
    # 临时输入/中间产物，按 TTL 清理
    "temporary": {"value": "temporary", "label": "临时数据", "severity": "neutral"},
    # 被活跃任务引用，GC 不得回收
    "task_pinned": {"value": "task_pinned", "label": "任务占用中", "severity": "neutral"},
    # 已按授权进入永久语料
    "persistent": {"value": "persistent", "label": "永久保存", "severity": "ok"},
    # 正在清理（宽限期内可能仍可见）
    "deleting": {"value": "deleting", "label": "正在清理", "severity": "progress", "active": True},
    # 已清理
    "deleted": {"value": "deleted", "label": "已删除", "severity": "neutral"},
}


def retention_class_label(value: str | None) -> str | None:
    """retention_class 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = RETENTION_CLASS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 发布轴（计划 §8.1 / §4.4）。**私有来源的派生页面不能靠切 public 绕过
# 原许可** —— 发布前要检查派生内容的公开权（计划 §4.4、T06）。
PublishingState = Literal["private", "draft", "published", "withdrawn"]

PUBLISHING_STATE_VALUES: Final[tuple[str, ...]] = (
    "private",
    "draft",
    "published",
    "withdrawn",
)

PUBLISHING_STATE_META: Final[dict[str, EnumMeta]] = {
    # 仅所有者与获授权者可见
    "private": {"value": "private", "label": "私有", "severity": "neutral"},
    # 草稿，未发布
    "draft": {"value": "draft", "label": "草稿", "severity": "neutral"},
    # 已按授权范围发布
    "published": {"value": "published", "label": "已发布", "severity": "ok"},
    # 已撤回。**不承诺收回已下载副本**
    "withdrawn": {"value": "withdrawn", "label": "已撤回", "severity": "warn"},
}


def publishing_state_label(value: str | None) -> str | None:
    """publishing_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = PUBLISHING_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 节点能力的就绪度（计划 §5.2）。**三件事必须分开记**：
# 静态能力配置、周期健康探测、本次任务预检。
#
# `configured` 不代表能用：计划原文举的例子是 `gpu=true` 不代表
# 所需模型已经就绪 —— 这正是本项目踩过的坑的联邦版本
# （注册表里有 OCR 专用模型，抽取平面拿它去抽值，抽不出来被记成
# `not_found`，系统能力缺失伪装成"文档里没有"，见已有的 `no_instruct`）。
CapabilityReadiness = Literal["configured", "ready", "draining", "unhealthy", "unknown"]

CAPABILITY_READINESS_VALUES: Final[tuple[str, ...]] = (
    "configured",
    "ready",
    "draining",
    "unhealthy",
    "unknown",
)

CAPABILITY_READINESS_META: Final[dict[str, EnumMeta]] = {
    # 配置里声明了这个能力，但没有健康证据。**不得当成可用**
    "configured": {"value": "configured", "label": "已配置（未验证可用）", "severity": "neutral"},
    # 健康探测通过且当前可接单
    "ready": {"value": "ready", "label": "可用", "severity": "ok"},
    # 正在排空，不接新单但在跑的会做完
    "draining": {"value": "draining", "label": "正在排空", "severity": "warn"},
    # 健康探测失败
    "unhealthy": {"value": "unhealthy", "label": "不可用", "severity": "error"},
    # 没有有效的健康证据（从没探过 / 记录过期）。
    # **过期记录不是当前能力证明**（§5.5），要按未知处理，不许按
    # 最后一次成功当成现在可用。
    "unknown": {"value": "unknown", "label": "能力状态未知", "severity": "warn"},
}


def capability_readiness_label(value: str | None) -> str | None:
    """capability_readiness 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = CAPABILITY_READINESS_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# Probe 的输入校验深度（计划 §6.4 / T78）。**这两个值的区别是钱**：
# 只看了文件描述就放进 admission，等于信任客户端声明的哈希 ——
# 本项目在直传上传那里已经踩过同一个坑（upload_status 的 `verifying`
# 不能跳过），联邦侧是同一条规则。
InputValidation = Literal["metadata_only", "content_verified"]

INPUT_VALIDATION_VALUES: Final[tuple[str, ...]] = (
    "metadata_only",
    "content_verified",
)

INPUT_VALIDATION_META: Final[dict[str, EnumMeta]] = {
    # 只校验了声明的格式/大小/类型，**没收到内容**。
    # 上传阶段只能是这个值，且此时不得占 GPU。
    "metadata_only": {"value": "metadata_only", "label": "仅校验元数据", "severity": "warn"},
    # 已收到内容并自己算过摘要校验通过。**预检仍不能排除运行时 OOM 或坏页**
    "content_verified": {"value": "content_verified", "label": "已校验内容", "severity": "ok"},
}


def input_validation_label(value: str | None) -> str | None:
    """input_validation 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = INPUT_VALIDATION_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 检索模式（计划 §6.3 / §7）。**mode 决定怎么查，scope 决定查哪些** ——
# 两者不许互相覆盖：`fast` 不能缩小用户固定的资源范围，
# `local_first`（排序偏好）也不能偷偷变成 `local_only`（外发策略）。
SearchMode = Literal["fast", "exhaustive_scope"]

SEARCH_MODE_VALUES: Final[tuple[str, ...]] = (
    "fast",
    "exhaustive_scope",
)

SEARCH_MODE_META: Final[dict[str, EnumMeta]] = {
    # 有界选点：摘要排序 + 少量并行 Probe + 有条件扩展。
    # **结局最多是 retrieval=partial**，必须报告未检索范围。
    "fast": {"value": "fast", "label": "快速检索（部分范围）", "severity": "neutral"},
    # 按封存的 ScopeManifest 逐个目标实际探测。摘要只影响顺序、不删成员。
    # 即使已经拿到好答案也继续做完，除非用户取消（§7.3）。
    "exhaustive_scope": {"value": "exhaustive_scope", "label": "范围穷查", "severity": "neutral"},
}


def search_mode_label(value: str | None) -> str | None:
    """search_mode 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = SEARCH_MODE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# `client-runtime` 的连接状态（计划 §3.4）。**与数据状态分开**
# （数据状态见 `snapshot_state`）—— 合起来的后果是
# "一个无关面板订阅失败把整个界面标成服务器断开"，计划明确禁止。
#
# 每个 `(environment_id, authenticated_profile_id)` 只有**一个**重连
# 负责人（计划 T66）；界面组件只订阅状态，不各自开重连循环。
TransportState = Literal["disconnected", "connecting", "authenticating", "ready", "backoff", "blocked"]

TRANSPORT_STATE_VALUES: Final[tuple[str, ...]] = (
    "disconnected",
    "connecting",
    "authenticating",
    "ready",
    "backoff",
    "blocked",
)

TRANSPORT_STATE_META: Final[dict[str, EnumMeta]] = {
    # 未连接
    "disconnected": {"value": "disconnected", "label": "未连接", "severity": "neutral"},
    # 正在建立连接
    "connecting": {"value": "connecting", "label": "连接中", "severity": "progress", "active": True},
    # 连上了，正在认证
    "authenticating": {"value": "authenticating", "label": "认证中", "severity": "progress", "active": True},
    # 可用
    "ready": {"value": "ready", "label": "已连接", "severity": "ok"},
    # 有限退避等待重试
    "backoff": {"value": "backoff", "label": "等待重连", "severity": "warn", "active": True},
    # 认证失效或被拒，**不再自动重试**（避免无休止刷新，T66）
    "blocked": {"value": "blocked", "label": "连接被拒绝（需重新配对）", "severity": "error"},
}


def transport_state_label(value: str | None) -> str | None:
    """transport_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = TRANSPORT_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 客户端缓存投影的数据状态（计划 §3.4）。与 `transport_state` 分开的理由
# 在那条里。`stale` 要能显示：断网时可以看已取得的本地内容，
# 但**不能显示假在线**（计划 §3.2）。
SnapshotState = Literal["loading", "current", "stale", "failed"]

SNAPSHOT_STATE_VALUES: Final[tuple[str, ...]] = (
    "loading",
    "current",
    "stale",
    "failed",
)

SNAPSHOT_STATE_META: Final[dict[str, EnumMeta]] = {
    # 首次取快照中
    "loading": {"value": "loading", "label": "加载中", "severity": "progress", "active": True},
    # 与服务端游标一致
    "current": {"value": "current", "label": "最新", "severity": "ok"},
    # 连接中断或游标落后，显示的是旧数据。**不得显示成在线最新**
    "stale": {"value": "stale", "label": "数据可能已过期", "severity": "warn"},
    # 取快照失败（游标失效时应重新取快照而不是永久等）
    "failed": {"value": "failed", "label": "数据加载失败", "severity": "error"},
}


def snapshot_state_label(value: str | None) -> str | None:
    """snapshot_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = SNAPSHOT_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 联邦协议的机器可读错误码（计划 §9.6）。
#
# **命名对齐项目既有约定**：计划正文写的是 SCREAMING_CASE，这里统一成
# snake_case —— 项目所有错误码（`invalid_request_error`、`quota_error` …）
# 与所有枚举都是 snake_case，而生成器也只接受 snake_case。
# 同一个概念两种拼法就是漂移的开始，所以在 P0 一次定死。
#
# **对外降敏**：不得借错误码暴露私有资源是否存在（§8.4）——
# 无权主体看到的应该是"找不到"而不是"存在但你没权限"。
#
# **`unreachable` 类错误绝不能被前端翻译成「对方没有资料」**（§9.6 原文）：
# 那是把"我没查到"说成"那里没有"。
FederationError = Literal["discovery_incomplete", "scope_expired", "capability_unknown", "capability_unsupported", "input_not_verified", "egress_denied", "plan_changed", "offer_expired", "admission_unknown", "idempotency_conflict", "partial_retrieval", "insufficient_evidence", "budget_exhausted", "source_revoked", "delivery_expired", "local_model_missing", "protocol_incompatible", "task_cancelled", "credential_invalid", "credential_expired", "credential_replayed", "credential_audience_mismatch", "credential_operation_denied", "credential_scope_denied", "node_unknown", "node_revoked", "node_identity_mismatch", "node_identity_unavailable", "credential_unavailable"]

FEDERATION_ERROR_VALUES: Final[tuple[str, ...]] = (
    "discovery_incomplete",
    "scope_expired",
    "capability_unknown",
    "capability_unsupported",
    "input_not_verified",
    "egress_denied",
    "plan_changed",
    "offer_expired",
    "admission_unknown",
    "idempotency_conflict",
    "partial_retrieval",
    "insufficient_evidence",
    "budget_exhausted",
    "source_revoked",
    "delivery_expired",
    "local_model_missing",
    "protocol_incompatible",
    "task_cancelled",
    "credential_invalid",
    "credential_expired",
    "credential_replayed",
    "credential_audience_mismatch",
    "credential_operation_denied",
    "credential_scope_denied",
    "node_unknown",
    "node_revoked",
    "node_identity_mismatch",
    "node_identity_unavailable",
    "credential_unavailable",
)

FEDERATION_ERROR_META: Final[dict[str, EnumMeta]] = {
    # 成员枚举没能封存，覆盖承诺随之降级
    "discovery_incomplete": {"value": "discovery_incomplete", "label": "节点范围未能完整确定", "severity": "warn"},
    # ScopeManifest 过期，需重新枚举生成新 scope
    "scope_expired": {"value": "scope_expired", "label": "检索范围已过期", "severity": "warn"},
    # 没有有效健康证据。**与 unsupported 严格分开** —— 未知要去预检，不是排除
    "capability_unknown": {"value": "capability_unknown", "label": "对方能力未知（需预检）", "severity": "warn"},
    # 已核实不支持所需 operation
    "capability_unsupported": {"value": "capability_unsupported", "label": "对方不支持该操作", "severity": "neutral"},
    # 输入摘要/格式还没校验通过就想进 admission
    "input_not_verified": {"value": "input_not_verified", "label": "输入尚未校验通过", "severity": "error"},
    # 外发许可不覆盖这次发送（接收方、内容或有效期超界）。
    # `local_only` 命中时也是这个码 —— 它高于所有自动回退（§6.2）。
    "egress_denied": {"value": "egress_denied", "label": "该数据不允许发往此接收方", "severity": "error"},
    # 计划修订变了，原批准不再适用
    "plan_changed": {"value": "plan_changed", "label": "执行计划已变更，需重新批准", "severity": "warn"},
    # Offer 有效期已过（Offer 本来就不预留算力）
    "offer_expired": {"value": "offer_expired", "label": "执行意向已过期", "severity": "warn"},
    # 受理状态不明。**不等于未执行**，要按幂等键对账（T82）
    "admission_unknown": {"value": "admission_unknown", "label": "受理状态未知（正在对账）", "severity": "warn"},
    # 同一幂等键对应不同请求正文。**返回冲突，不许复用不相关结果**（T80）
    "idempotency_conflict": {"value": "idempotency_conflict", "label": "幂等键冲突（请求内容不一致）", "severity": "error"},
    # 检索只完成了一部分，覆盖账本里有缺口
    "partial_retrieval": {"value": "partial_retrieval", "label": "检索未覆盖全部范围", "severity": "warn"},
    # 本次范围与配置下没拿到足够证据
    "insufficient_evidence": {"value": "insufficient_evidence", "label": "证据不足", "severity": "warn"},
    # 根预算用尽（含发现与 Probe 的消耗）
    "budget_exhausted": {"value": "budget_exhausted", "label": "预算已用尽", "severity": "warn"},
    # 来源被撤销或转为私有，停止新授权并重判派生依赖
    "source_revoked": {"value": "source_revoked", "label": "来源已撤销", "severity": "warn"},
    # 结果暂存 TTL 到期未领取
    "delivery_expired": {"value": "delivery_expired", "label": "结果已过期未领取", "severity": "error"},
    # 本地缺所需模型。**必须明确报出来**，不得悄悄请求远端（I03 / T18）——
    # 这正是项目已有的 `no_instruct_model` 在本地模式下的对应物。
    "local_model_missing": {"value": "local_model_missing", "label": "本地缺少所需模型", "severity": "error"},
    # 协议版本或必需字段不兼容，明确拒绝而不是忽略后乱执行
    "protocol_incompatible": {"value": "protocol_incompatible", "label": "协议版本不兼容", "severity": "error"},
    # 对已取消任务调用 resume。**取消是显式终态，不得被"恢复"改写回
    # running** —— 重跑必须是一条新任务（新授权、新覆盖分母），而不是
    # 拿旧计划接着跑。返回 409，任务状态原样不动。
    "task_cancelled": {"value": "task_cancelled", "label": "任务已取消，不能恢复", "severity": "error"},
    # 节点凭证缺失字段、不是规范序列化、base64 不严格、算法不是 Ed25519、
    # 有效期超过上限，或签名验不过（篡改）。一律 401，不区分是哪一步 ——
    # 分开报等于给伪造者一个逐步试错的口。
    "credential_invalid": {"value": "credential_invalid", "label": "节点凭证无效", "severity": "error"},
    # 凭证已过期或签发时间在未来（超出时钟偏差容忍）
    "credential_expired": {"value": "credential_expired", "label": "节点凭证已过期", "severity": "error"},
    # 同一个 jti 第二次出现。凭证是**单次使用**的：执行者在验签通过后
    # 持久记下 jti 直到过期，并发重放由唯一约束仲裁（401）。
    "credential_replayed": {"value": "credential_replayed", "label": "节点凭证被重放", "severity": "error"},
    # 凭证的 audience 不是接收它的这个节点。转手给第三个节点（越权转委托）就是这个码
    "credential_audience_mismatch": {"value": "credential_audience_mismatch", "label": "凭证不是发给本节点的", "severity": "error"},
    # 凭证授权的操作不是本端点的操作（403）
    "credential_operation_denied": {"value": "credential_operation_denied", "label": "凭证不允许该操作", "severity": "error"},
    # 凭证的请求绑定（方法/路径/正文摘要）或范围约束（root_task_id / step_id /
    # scope_ref / task_spec_digest）不覆盖这次请求或它要读的那一行（403）；
    # 以别的协调者名义提交计划也是这个码。
    "credential_scope_denied": {"value": "credential_scope_denied", "label": "凭证范围不覆盖该请求", "severity": "error"},
    # 签发节点不在本节点控制面的成员目录里，或尚未被管理员批准（401）
    "node_unknown": {"value": "node_unknown", "label": "未知或未批准的节点", "severity": "error"},
    # 签发节点已被管理员撤销。撤销对新请求生效的延迟以公钥缓存上限为界（401）
    "node_revoked": {"value": "node_revoked", "label": "节点已被撤销", "severity": "error"},
    # 语料服务配置的节点身份（BUNDLE_NODE_ID）与控制面持久密钥派生的身份不一致，
    # 或控制面报告的本节点身份变了。**Fail Closed（503）**：否则本地目标会被当成远端
    "node_identity_mismatch": {"value": "node_identity_mismatch", "label": "本节点身份不一致（联邦已停用）", "severity": "error"},
    # 还没从控制面取到本节点持久身份（503），联邦端点与出站一律拒绝
    "node_identity_unavailable": {"value": "node_identity_unavailable", "label": "本节点身份尚未确定", "severity": "error"},
    # 本节点控制面在凭证链路上不可用：出站时签不出凭证（不可达、拒签、响应形状不对），
    # 或入站时查不到签发节点的信任记录（503）。**是本节点的问题，不是对端没有资料**
    # —— 协调者记 unreachable 并保留可重试。
    "credential_unavailable": {"value": "credential_unavailable", "label": "本节点凭证服务不可用", "severity": "error"},
}


def federation_error_label(value: str | None) -> str | None:
    """federation_error 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = FEDERATION_ERROR_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 一张节点凭证授权的**唯一**操作（DDP-NODE-CREDENTIAL）。每个节点对节点
# 端点恰好对应一个值；凭证只签一个操作，拿读执行状态的凭证去受理任务是
# `credential_operation_denied`。
NodeCredentialOperation = Literal["probe_create", "probe_read", "admission_create", "admission_lookup", "execution_read", "execution_cancel", "evidence_set_read", "resource_locate", "result_resolve", "catalog_read"]

NODE_CREDENTIAL_OPERATION_VALUES: Final[tuple[str, ...]] = (
    "probe_create",
    "probe_read",
    "admission_create",
    "admission_lookup",
    "execution_read",
    "execution_cancel",
    "evidence_set_read",
    "resource_locate",
    "result_resolve",
    "catalog_read",
)

NODE_CREDENTIAL_OPERATION_META: Final[dict[str, EnumMeta]] = {
    # POST /api/v1/federation/probes
    "probe_create": {"value": "probe_create", "label": "发起探测", "severity": "neutral"},
    # GET /api/v1/federation/probes/{probe_id}
    "probe_read": {"value": "probe_read", "label": "读取探测回执", "severity": "neutral"},
    # POST /api/v1/federation/admissions
    "admission_create": {"value": "admission_create", "label": "提交接单", "severity": "neutral"},
    # POST /api/v1/federation/admissions/lookup
    "admission_lookup": {"value": "admission_lookup", "label": "对账接单", "severity": "neutral"},
    # GET /api/v1/federation/tasks/{executor_task_id}
    "execution_read": {"value": "execution_read", "label": "读取执行状态", "severity": "neutral"},
    # POST /api/v1/federation/tasks/{executor_task_id}/cancel
    "execution_cancel": {"value": "execution_cancel", "label": "取消执行", "severity": "neutral"},
    # GET /api/v1/federation/evidence-sets/{set_ref}
    "evidence_set_read": {"value": "evidence_set_read", "label": "读取证据集", "severity": "neutral"},
    # POST /api/v1/federation/resources/locate
    "resource_locate": {"value": "resource_locate", "label": "定位资源版本", "severity": "neutral"},
    # POST /api/v1/federation/results/resolve
    "result_resolve": {"value": "result_resolve", "label": "解析证据引用", "severity": "neutral"},
    # GET /api/v1/federation/published-collections
    "catalog_read": {"value": "catalog_read", "label": "读取发布目录", "severity": "neutral"},
}


def node_credential_operation_label(value: str | None) -> str | None:
    """node_credential_operation 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = NODE_CREDENTIAL_OPERATION_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 语料服务节点对节点端点的认证方式。`node_credential` 是唯一的生产形态；
# `shared_token_insecure` 只给没有控制面的开发夹具，**必须显式配置且同时
# 打开 ALLOW_INSECURE_DEFAULTS**，并在 /readyz 与能力声明里如实报成降级。
PeerAuthMode = Literal["node_credential", "shared_token_insecure"]

PEER_AUTH_MODE_VALUES: Final[tuple[str, ...]] = (
    "node_credential",
    "shared_token_insecure",
)

PEER_AUTH_MODE_META: Final[dict[str, EnumMeta]] = {
    # 控制面持有节点私钥，按请求签发限定 audience/actor/操作/范围/有效期的单次凭证
    "node_credential": {"value": "node_credential", "label": "节点签名凭证", "severity": "ok"},
    # 旧的共享 peer token + 服务凭据 + actor 头。所有登记同伴共用一个秘密、
    # 调用方身份未签名、同名用户会被直接合并 —— 只许开发用
    "shared_token_insecure": {"value": "shared_token_insecure", "label": "共享口令（不安全，仅开发）", "severity": "warn"},
}


def peer_auth_mode_label(value: str | None) -> str | None:
    """peer_auth_mode 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = PEER_AUTH_MODE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 控制域管理员批准的直接节点成员状态，批准不授予资源权限或证明远端持有密钥。
NodeMembershipState = Literal["pending", "approved", "revoked"]

NODE_MEMBERSHIP_STATE_VALUES: Final[tuple[str, ...]] = (
    "pending",
    "approved",
    "revoked",
)

NODE_MEMBERSHIP_STATE_META: Final[dict[str, EnumMeta]] = {
    # 已登记但管理员尚未批准
    "pending": {"value": "pending", "label": "待批准", "severity": "neutral"},
    # 管理员已批准配置，健康与接单另行判断
    "approved": {"value": "approved", "label": "已批准", "severity": "ok"},
    # 已撤销，保留旧快照成员位置且禁止旧修订恢复
    "revoked": {"value": "revoked", "label": "已撤销", "severity": "warn"},
}


def node_membership_state_label(value: str | None) -> str | None:
    """node_membership_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = NODE_MEMBERSHIP_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"


# 单目录快照中的下级枚举状态，不声明递归全局覆盖。
MemberExpansionState = Literal["not_requested", "unexpanded_subtree", "source_revoked"]

MEMBER_EXPANSION_STATE_VALUES: Final[tuple[str, ...]] = (
    "not_requested",
    "unexpanded_subtree",
    "source_revoked",
)

MEMBER_EXPANSION_STATE_META: Final[dict[str, EnumMeta]] = {
    # 成员支持枚举但尚未请求下级目录
    "not_requested": {"value": "not_requested", "label": "尚未展开", "severity": "neutral"},
    # 下级不可枚举，不等于空目录
    "unexpanded_subtree": {"value": "unexpanded_subtree", "label": "下级未展开", "severity": "warn"},
    # 原快照成员已撤销或当前调用者不可见
    "source_revoked": {"value": "source_revoked", "label": "来源已撤销", "severity": "warn"},
}


def member_expansion_state_label(value: str | None) -> str | None:
    """member_expansion_state 的用户文案。未知取值也要给出可读文字，
    不能把原始枚举丢给用户。"""
    if not value:
        return None
    meta = MEMBER_EXPANSION_STATE_META.get(value)
    return meta["label"] if meta else f"未知取值（{value}）"
