"""抽取平面守卫：unhashable required、inf/nan、词汇表同源。"""
import pytest

from ddp_contracts.enums import DEGRADED_VALUES as CONTRACT_DEGRADED
from ddp_contracts.enums import FIELD_STATUS_VALUES as CONTRACT_FIELD_STATUS
from ddp_core import extract_format as fmt
from ddp_core.extract_format import (
    DEGRADED_VALUES,
    FIELD_STATUSES,
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


def test_vocabularies_come_from_contracts_single_source():
    assert FIELD_STATUSES is CONTRACT_FIELD_STATUS
    assert DEGRADED_VALUES is CONTRACT_DEGRADED
    assert set(CONTRACT_FIELD_STATUS) == {"found", "not_found", "error"}
    assert "rerank_unavailable" in DEGRADED_VALUES
    assert "no_instruct_model" in DEGRADED_VALUES


def test_vocab_parity_with_enums_yaml():
    import re
    from pathlib import Path

    yaml_text = (Path(__file__).resolve().parents[3]
                 / "packages" / "contracts" / "enums.yaml").read_text(encoding="utf-8")
    degraded_block = yaml_text.split("compile_degraded:")[0]
    yaml_values = set(re.findall(r"- value: ([a-z0-9_]+)", degraded_block))
    assert set(DEGRADED_VALUES) <= yaml_values, (
        "extract_format 的降级词超出了 enums.yaml 的 degraded 词汇："
        f"{sorted(set(DEGRADED_VALUES) - yaml_values)}")
    assert "rerank_unavailable" in yaml_values
    assert "no_instruct_model" in yaml_values


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
