"""Web 联邦任务界面的 e2e 夹具必须过冻结契约。

`apps/web/e2e/fixtures/federation/*.json` 是 Playwright 用例替后端返回的响应。
**替身比真实端点宽松，界面测试就是在测一个不存在的后端**（F-34）：少一个必填字段、
多一个契约外字段、写一个契约里没有的状态值，界面照样渲染得漂漂亮亮，上线才炸。

所以每个夹具都按它扮演的响应过 schema，并且把 `x-ddp-enum` 展开成真实取值再验 ——
jsonschema 不认识 `x-ddp-enum`，不展开的话"写错一个枚举值"这一整类问题验不出来。
"""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ddp_contracts import enums
from ddp_core.application import coverage as coverage_kernel
from ddp_corpus import federation
from test_federation_admissions import FEDERATION_TASKS_SPEC, SCHEMAS

FIXTURES = Path(__file__).resolve().parents[3] / "apps/web/e2e/fixtures/federation"

#: 夹具文件 → 它扮演的响应。新增夹具必须登记，否则下面的完整性用例会红。
ROLES = {
    "task-list.json": "TaskListPage",
    "task-list-last-page.json": "TaskListPage",
    "task-running.json": "TaskStatus",
    "task-succeeded.json": "TaskStatus",
    "task-insufficient.json": "TaskStatus",
    "events-first.json": "EventPage",
    "events-later.json": "EventPage",
    "plan.json": "TaskPlan",
    "coverage.json": "CoverageLedger",
}


def _expand_enums(node):
    """把 `x-ddp-enum` 换成 enums.yaml 里的真实取值（可空的带上 null）。"""
    if isinstance(node, dict):
        out = {key: _expand_enums(value) for key, value in node.items()}
        name = node.get("x-ddp-enum")
        if isinstance(name, str):
            values = list(getattr(enums, f"{name.upper()}_VALUES"))
            if node.get("x-ddp-enum-nullable"):
                values.append(None)
            out["enum"] = values
        return out
    if isinstance(node, list):
        return [_expand_enums(item) for item in node]
    return node


COMPONENTS = _expand_enums(copy.deepcopy(FEDERATION_TASKS_SPEC["components"]))


def _ddp(schema_file: str, definition: str) -> Draft202012Validator:
    schema = _expand_enums(SCHEMAS["schemas"][schema_file])
    return Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": schema["$defs"]})


def _openapi(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": COMPONENTS})


VALIDATORS = {
    "TaskListPage": _openapi("TaskListPage"),
    "TaskStatus": _openapi("TaskStatus"),
    "EventPage": _openapi("EventPage"),
    "TaskPlan": _ddp("ddp-plan-admission/v1.json", "TaskPlan"),
    "CoverageLedger": _ddp("ddp-scope-coverage/v1.json", "CoverageLedger"),
}
EVIDENCE = _ddp("ddp-evidence/v1.json", "FederatedEvidence")
BINDING = _ddp("ddp-evidence/v1.json", "ClaimEvidenceBinding")
CONFLICT = _ddp("ddp-scope-coverage/v1.json", "EvidenceConflict")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_every_fixture_file_has_a_contract_role():
    assert {path.name for path in FIXTURES.glob("*.json")} == set(ROLES), \
        "新夹具要在 ROLES 里登记它扮演的响应，否则它不受契约约束"


@pytest.mark.parametrize(("name", "role"), sorted(ROLES.items()))
def test_fixture_matches_the_frozen_contract(name, role):
    VALIDATORS[role].validate(load(name))


@pytest.mark.parametrize("name", sorted(name for name, role in ROLES.items() if role == "TaskStatus"))
def test_task_result_documents_use_contract_shapes(name):
    """`TaskStatus.result` 在 OpenAPI 里是自由对象 —— 里面的证据、绑定、矛盾与原因单独按契约验。"""
    result = load(name)["result"]
    if result is None:
        return
    for item in result["evidence"]:
        EVIDENCE.validate(item)
    for binding in result["claim_evidence_bindings"]:
        BINDING.validate(binding)
    for conflict in result["conflicts"]:
        CONFLICT.validate(conflict)
    if result["answer_reason"] is not None:
        federation.unavailable_answer(result["answer_reason"])   # 未声明的原因在这里就抛
    ids = {item["evidence_id"] for item in result["evidence"]}
    for group in [binding["evidence_refs"] for binding in result["claim_evidence_bindings"]] + \
            [conflict["evidence_refs"] for conflict in result["conflicts"]]:
        assert set(group) <= ids, "绑定与矛盾只能引用本结果里的证据"
    assert result["evidence_sufficiency"] in enums.EVIDENCE_SUFFICIENCY_VALUES
    assert result["retrieval_completeness"] in enums.RETRIEVAL_COMPLETENESS_VALUES


def test_coverage_fixture_passes_the_kernel_invariants_too():
    """schema 之外还有内核的判据（fast 不许 complete、矛盾记录与充分性一致……）。"""
    coverage_kernel.validate_ledger(load("coverage.json"))
