"""抽取边界：坏 schema、非有限值和可追溯的字面数值。"""
import pytest

from ddp_core import extract_format as fmt
from ddp_core.extract_format import (
    CoerceError,
    FieldSpec,
    coerce_value,
    validate_result,
    validate_schema,
)


def _props():
    return {"properties": {"a": {"type": "string", "description": "字段 A"}}}


def test_validate_schema_treats_unhashable_required_as_problem():
    schema = {**_props(), "required": [["a"]]}
    problems = validate_schema(schema)
    assert problems, "unhashable required 条目必须变成 problem，而不是 TypeError"
    assert any("required" in p for p in problems)


def test_validate_schema_treats_dict_required_as_problem():
    schema = {**_props(), "required": [{"name": "a"}]}
    problems = validate_schema(schema)
    assert problems
    assert any("required" in p for p in problems)


def test_validate_schema_reports_missing_string_required():
    schema = {**_props(), "required": ["ghost"]}
    assert any("ghost" in p for p in validate_schema(schema))


@pytest.mark.parametrize("raw", [float("inf"), float("-inf"), float("nan")])
def test_coerce_rejects_non_finite_numbers(raw):
    with pytest.raises(CoerceError):
        coerce_value(raw, FieldSpec(name="n", type="number", description="数"))
    with pytest.raises(CoerceError):
        coerce_value(raw, FieldSpec(name="i", type="integer", description="整数"))


def test_coerce_rejects_non_finite_string_numbers():
    with pytest.raises(CoerceError):
        coerce_value("inf", FieldSpec(name="n", type="number", description="数"))


def test_coerce_keeps_finite_numbers():
    num = FieldSpec(name="n", type="number", description="数")
    integer = FieldSpec(name="i", type="integer", description="整数")
    assert coerce_value(3.0, num) == 3.0
    assert coerce_value(3.0, integer) == 3
    assert coerce_value("42", integer) == 42
    with pytest.raises(CoerceError):
        coerce_value(3.5, integer)


@pytest.mark.parametrize("raw, expected", [
    # 带单位装饰的单数字回答：与字面见证共用记号写法后可取值，不再误杀。
    ("3 times", 3),
    ("10,000 times", 10000),
    ("8,192", 8192),
    ("5 µA", 5),
    ("¥1,234.00 元", 1234),
    ("$−40", -40),
    ("3 pounds-force", 3),
    ("1 eV", 1),
    ("1.2e3", 1200),
    ("3.0", 3),
    ("共194种语言", 194),
    # Unicode 负号必须保留符号：旧写法去符号后 −40 变成 +40。
    ("−40", -40),
    # 大整数不经 float 中转：9007199254740993 转 float 会变成 …992。
    ("9007199254740993", 9007199254740993),
])
def test_coerce_accepts_single_decorated_number(raw, expected):
    assert coerce_value(raw, FieldSpec(name="i", type="integer", description="整数")) == expected


@pytest.mark.parametrize("raw", [
    # 非法千分位不能去逗号硬认："1,2" 不是 12（见证侧同样拒绝）。
    "1,2",
    "1,,2",
    # 不得从未闭合的数字语法里截取子串，也不得丢弃币种外的负号。
    "--40",
    "1e-",
    "1e",
    "e-40",
    "−$40",
    # 多个数字时"取第一个"是猜：区间与并列必须整体拒绝。
    "−40∼105",
    "12 and 34",
    "version 3.5.2",
    "xP3",
    "GPT-3",
    "model_v3.2",
    # 超出可用范围的指数：不能抛裸 ValueError 逃出 CoerceError 契约。
    "1e9999",
    # 无数字与非有限写法。
    "",
    "Adam",
    "inf",
    "nan",
])
def test_coerce_rejects_ambiguous_or_invalid_numbers(raw):
    integer = FieldSpec(name="i", type="integer", description="整数")
    num = FieldSpec(name="n", type="number", description="数")
    with pytest.raises(CoerceError):
        coerce_value(raw, integer)
    with pytest.raises(CoerceError):
        coerce_value(raw, num)


@pytest.mark.parametrize("runtime_limit", [0, 640])
def test_coerced_integer_round_trips_with_json_digit_limit(runtime_limit):
    import json
    import sys

    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(runtime_limit)
        limit = min(runtime_limit or sys.int_info.default_max_str_digits,
                    sys.int_info.default_max_str_digits)
        spec = FieldSpec(name="n", type="integer", description="整数")
        value = coerce_value(f"1e{limit - 1}", spec)
        assert json.loads(json.dumps(value)) == 10 ** (limit - 1)
        with pytest.raises(CoerceError):
            coerce_value(f"1e{limit}", spec)
    finally:
        sys.set_int_max_str_digits(previous)


def test_coerce_decorated_number_values():
    num = FieldSpec(name="n", type="number", description="数")
    assert coerce_value("99.3%", num) == 99.3
    assert coerce_value("3 times", num) == 3.0


@pytest.mark.parametrize("description, expected", [
    # description 里点名的位置短语，一字不改地搬出来作第三检索 leg。
    ("Factor stated in the abstract for the comparison.", "abstract"),
    ("Factor stated in abstract for the comparison.", "abstract"),
    ("DistilBERT parameter count in the unit millions shown in Table3.", "Table 3"),
    ("Value shown in Table 6 of the paper.", "Table 6"),
    ("See Figure 4 for the distribution.", "Figure 4"),
    ("Result stated in Section 4.1.", "Section 4.1"),
    ("Result stated in §3.1.", "§3.1"),
    ("论文 Vicuna benchmark 结果表中，Guanaco 65B 的平均得分。只依据该结果表作答。",
     "Vicuna benchmark"),
    ("论文 Double Quantization 小节中，量化常数的内存。只依据该小节正文作答。",
     "Double Quantization"),
    ("论文 Paged Optimizers 小节中，分页优化器的内存机制。", "Paged Optimizers"),
    # 无位置短语：返回 None，调用方保持原有两条 leg，不伪造。
    ("Number of languages represented in the unsupervised training data.", None),
    ("Maximum supported input length stated by the paper, as a number.", None),
    # "that same <位置>" 回指同样点名了位置名词：只搬位置名词本身作 leg。
    ("Factor of GPU memory reduction stated for that same abstract comparison.", "abstract"),
    ("Value from that same table comparison.", "table"),
    # 无位置名词的 "same comparison" 不点名位置：不搬。
    ("Factor stated for that same comparison.", None),
    # 中文回指无实质内容：不搬。
    ("只依据该表格作答，不要换算单位。", None),
])
def test_scope_phrase_query_extracts_verbatim_location(description, expected):
    assert fmt.scope_phrase_query(
        FieldSpec(name="f", type="string", description=description)) == expected




def test_validate_result_accepts_contract_degraded_values():
    for value in ("resource_index_unavailable", "rerank_unavailable", "schema_violation"):
        result = {
            "extract_version": fmt.EXTRACT_VERSION,
            "status": "partial",
            "degraded": value,
            "fields": {
                "a": {"status": "found", "value": "x", "citations": ["c1"],
                      "verified": False, "degraded": None,
                      "confidence": {"level": "high", "top_similarity": 0.9}},
            },
        }
        assert validate_result(result) == [], value




@pytest.mark.parametrize("value, text, expected", [
    (8192, "up to 8,192 input tokens", True),
    (8192, "up to 8192 input tokens", True),
    (1200, "about 1.2e3 pairs", True),
    (0.5, "ratio 0.50 after tuning", True),
    # R7 实测的失败缝：CPU 对 count 输出 12，引用的 seq142 原文里根本没有 12。
    (12, "Data Source Language Size Unsupervised Data MTP EN, ZH 291.1M", False),
    (194, "it brings in 1.2 billion text pairs", False),
    (194, "it brings in 1.2 billion text pairs of 194 languages", True),
    # 逗号只认千分位：“1,2”不是数字写法，不能去逗号硬认成 12。
    (12, "values 1,2 and 34", False),
    (12, "values 1,,2 and 34", False),
    (12, "Values: 12, then 34.", True),
    (1000, "1,000e0 units", True),
    (9007199254740993, "count 9007199254740993", True),
    (9007199254740992, "count 9007199254740993", False),
    (1.0000000001, "ratio 1", False),
    (12, "negative value −12", False),
    (9, "value 1e9999", False),
    # 标识符里的数字不是数量：公网实测把数据源 xP3 的 3 当成语言数。
    (3, "xP3, mC4, CC-News Multi-Lingual 488.4M", False),
    (3, "MTP EN, ZH 291.1M S2ORC EN 48.3M xP3 Multi-Lingual", False),
    (-3, "Compared with GPT-3", False),
    (3.5, "version 3.5.2", False),
    (3, "xP3 covers 3 languages", True),
    (194, "共194种语言", True),
    (0.3, "supply 0.3V", True),
])
def test_value_supported_by_source_is_normalized_mechanical_check(value, text, expected):
    assert fmt.value_supported_by_source(value, text) is expected


def test_field_support_value_role_is_independent_of_reading_order():
    answer = {"found": True, "value": "v",
              "support": {"value_source": 3, "context_sources": ["1", 2]}}
    assert fmt.resolve_field_support(answer, 4) == [2, 0, 1]


@pytest.mark.parametrize("support", [
    None, "1", [], {}, {"value_source": 1},
    {"value_source": 1, "context_sources": [], "extra": 2},
    {"value_source": 1, "context_sources": None},
    {"value_source": 1, "context_sources": [1]},
    {"value_source": 1, "context_sources": ["1"]},
    {"value_source": 1, "context_sources": [2, 2]},
    {"value_source": 1, "context_sources": [2, 3, 4]},
    {"value_source": 0, "context_sources": []},
    {"value_source": 5, "context_sources": []},
    {"value_source": True, "context_sources": []},
    {"value_source": 1.5, "context_sources": []},
    {"value_source": "not-an-id", "context_sources": []},
    {"value_source": 1, "context_sources": [True]},
    {"value_source": 1, "context_sources": [5]},
])
def test_invalid_field_support_is_rejected_whole(support):
    answer = {"found": True, "value": "v", "support": support}
    assert fmt.resolve_field_support(answer, 4) is None


@pytest.mark.parametrize("answer", [
    {"found": False, "value": None,
     "support": {"value_source": 1, "context_sources": []}},
    {"found": False, "value": "v", "support": None},
    {"found": True, "value": None,
     "support": {"value_source": 1, "context_sources": []}},
    {"found": "false", "value": None, "support": None},
    {"found": 1, "value": "v",
     "support": {"value_source": 1, "context_sources": []}},
    {"found": True, "value": "v"},
    {"found": True, "value": "v", "source": 2,
     "support": {"value_source": 1, "context_sources": []}},
])
def test_contradictory_or_ambiguous_field_selection_is_rejected(answer):
    assert fmt.resolve_field_support(answer, 2) is None


def test_explicit_bounded_absence_has_no_selected_source():
    assert fmt.resolve_field_support(
        {"found": False, "value": None, "support": None}, 2) == []


def test_source_index_rejects_coercive_or_unknown_identities():
    assert fmt.resolve_source_index(2, 3) == 1
    assert fmt.resolve_source_index("2", 3) == 1
    for bad in (True, 1.5, 0, 4, None, "abc"):
        assert fmt.resolve_source_index(bad, 3) is None


def test_field_messages_groups_contiguous_region_without_merging_ids():
    """Reading order and sequence gaps must not remap an original citation ID."""
    import json

    spec = FieldSpec(name="memory_factor", type="integer",
                     description="Factor stated in the abstract.")
    hits = [
        {"evidence_id": "later", "document_id": "d", "parse_job_id": "j",
         "page_idx": 4, "seq": 47, "bbox": [1, 2, 3, 4],
         "page_size": [612, 792], "text": "later experiment restatement 3"},
        {"evidence_id": "first-half", "document_id": "d", "parse_job_id": "j",
         "page_idx": 0, "seq": 3, "bbox": [9, 9, 9, 9],
         "page_size": [612, 792], "text": "the requested comparison states GPU"},
        {"evidence_id": "second-half", "document_id": "d", "parse_job_id": "j",
         "page_idx": 0, "seq": 4, "bbox": [9, 9, 9, 9],
         "page_size": [612, 792], "text": "memory requirement by 3 times",
         "context_of_evidence_id": "first-half"},
        {"evidence_id": "after-gap", "document_id": "d", "parse_job_id": "j",
         "page_idx": 0, "seq": 6, "bbox": [9, 9, 9, 9],
         "page_size": [612, 792], "text": "a later section has a different comparison"},
    ]
    messages = fmt.field_messages(spec, hits)
    payload = json.loads(messages[1]["content"])
    groups = payload["source_groups"]
    # 连续同区域两半同组：先按原文顺序出现，source_id 仍指向输入位序。
    assert [[fragment["source_id"] for fragment in group["fragments"]]
            for group in groups] == [[2, 3], [4], [1]]
    assert [fragment.get("sequence")
            for fragment in groups[0]["fragments"]] == [3, 4]
    assert groups[0]["fragments"][1]["adjacent_to_source_id"] == 2


def test_record_source_selects_exactly_the_read_fragment():
    assert fmt.resolve_record_source(
        {"source": 1, "values": {"source": "business", "values": 7}}, 2) == (
        0, {"source": "business", "values": 7})
    assert fmt.resolve_record_source(
        {"source": "2", "values": {"name": "row"}}, 2) == (1, {"name": "row"})


@pytest.mark.parametrize("row", [
    {"values": {"name": "row"}},
    {"source": 1},
    {"source": 1, "values": None},
    {"source": 1, "values": [1]},
    {"source": 1, "values": "row"},
    {"source": None, "values": {"name": "row"}},
    {"source": True, "values": {"name": "row"}},
    {"source": 1.0, "values": {"name": "row"}},
    {"source": 0, "values": {"name": "row"}},
    {"source": 3, "values": {"name": "row"}},
    {"source": "abc", "values": {"name": "row"}},
    {"source": [1], "values": {"name": "row"}},
    "not-a-row",
])
def test_record_source_rejects_missing_or_hallucinated_pointer(row):
    assert fmt.resolve_record_source(row, 2) is None
