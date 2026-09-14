"""版本兼容的否定面：旧版本与未知必填字段必须整体拒绝，不许猜读。

`packages/contracts/fixtures/` 里的反例夹具由 `check_federation_contracts.py`
保证"会被 JSON Schema 拒、且因正确的理由被拒"。本文件补的是另一半：
**运行时内核面对同一份数据也拒**，而且拒绝码是那个能让人查到原因的机器码。

三条被钉死的语义：

1. 未知/旧版本（Bundle/Evidence/TaskSpec/Probe/Admission）→ 拒绝，不做
   版本嗅探；
2. 未知的**必填语义字段**（例如 bundle 的 `required_features`、证据里的
   外来必填字段）→ 拒绝。收下再"忽略不认识的部分"正是 T85 要防的
   "静默降级"：少了字段的 plan 看起来照样能跑；
3. 拒收发生在副作用之前 —— 这里用纯内核验证，能证明的是"它算不出来"，
   落库之前的顺序由 corpus-api 的 HTTP 用例覆盖。

**为什么不用 JSON Schema 代替运行时测试**：schema 是契约，内核实现是另一份
代码。历史上这个仓库出现过"fixture 绿而运行时是死的"（FINDINGS F-*），
两边都要有自己的否定用例。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from ddp_bundle_fixture import sample_parts
from ddp_core.application import plans
from ddp_core.application.admission import validate_receipt
from ddp_core.application.ports import ApplicationError
from ddp_core.application.probe import validate_probe
from ddp_core.bundle import BundleError, build_bundle, json_bytes, validate_parts

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "contracts" / "fixtures"


def load(kind: str, name: str) -> dict:
    return json.loads((FIXTURES / kind / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------- Bundle

@pytest.mark.parametrize("name", [
    "bundle-old-schema.json",
    "bundle-new-schema.json",
    "bundle-required-feature.json",
])
def test_bundle_unknown_version_or_required_feature_is_refused_whole(name):
    """旧/新版本与"我需要的功能没人实现"都是整包拒收，不是降级读一半。

    `validate_parts` 在检查文件清单之前先看 schema 与 required_features ——
    所以这里传空文件表也能证明拒绝发生在任何内容被采信之前。
    """
    with pytest.raises(BundleError) as error:
        validate_parts(load("invalid", name), {})
    assert error.value.code == "bundle_schema_unsupported"


def test_bundle_required_features_must_be_empty_exactly():
    manifest = load("valid", "bundle-manifest.json")
    manifest["required_features"] = ["run-shell"]
    with pytest.raises(BundleError) as error:
        validate_parts(manifest, {})
    assert error.value.code == "bundle_schema_unsupported"


def test_old_evidence_schema_inside_a_real_bundle_is_refused():
    """Bundle 里的证据版本也要拒：不能"包版本对、里面的证据按新版本读"。"""
    source, files = sample_parts()
    records = json.loads(files["evidence.json"])
    records[0]["evidence"]["schema"] = "ddp-evidence/0#FederatedEvidence"
    files["evidence.json"] = json_bytes(records)
    with pytest.raises(BundleError) as error:
        build_bundle(source, files)
    assert error.value.code == "bundle_schema_unsupported"


def test_unknown_evidence_field_is_not_ignored():
    """证据里出现不认识的字段 = 拒收：静默丢字段会让"必填"两个字失去意义。"""
    source, files = sample_parts()
    records = json.loads(files["evidence.json"])
    records[0]["evidence"]["required_client_capabilities"] = ["future-thing"]
    files["evidence.json"] = json_bytes(records)
    with pytest.raises(BundleError) as error:
        build_bundle(source, files)
    assert error.value.code == "bundle_schema_unsupported"


# ----------------------------------------------------------- TaskSpec 协议

def test_old_task_protocol_is_refused_before_any_execution():
    """TaskSpec 的协议版本不匹配 -> kernel 拒绝，调用方拿不到可执行计划。"""
    with pytest.raises(ApplicationError) as error:
        plans.validate_spec(load("invalid", "task-spec-old-protocol.json"))
    assert error.value.code == "invalid_plan"
    assert "unsupported task schema" in str(error.value)


def test_unknown_task_spec_field_is_refused():
    spec = load("valid", "task-spec-exhaustive.json")
    spec["required_client_capabilities"] = ["future-thing"]
    with pytest.raises(ApplicationError) as error:
        plans.validate_spec(spec)
    assert error.value.code == "invalid_plan"


# ----------------------------------------------------------- Probe 回执

def test_old_probe_version_is_protocol_incompatible():
    probe = load("valid", "probe-evidence-succeeded.json")
    probe["schema"] = "ddp-probe/0"
    with pytest.raises(ApplicationError) as error:
        validate_probe(probe)
    assert error.value.code == "protocol_incompatible"


def test_unknown_probe_field_is_protocol_incompatible_not_ignored():
    probe = load("valid", "probe-evidence-succeeded.json")
    probe["required_client_capabilities"] = ["future-thing"]
    with pytest.raises(ApplicationError) as error:
        validate_probe(probe)
    assert error.value.code == "protocol_incompatible"


def test_probe_internal_limits_can_never_be_washed_into_success():
    """T85 的运行时版本：内部有 shard_failed 却在外层写 succeeded = 拒收。

    "先收下、执行时忽略不认识/不支持的东西"在这条上会直接表现成覆盖账本
    报 complete —— 所以拒绝必须发生在编译回执这一步。
    """
    probe = load("invalid", "probe-internal-limit-claims-success.json")
    with pytest.raises(ApplicationError) as error:
        validate_probe(probe)
    assert error.value.code == "partial_retrieval"
    # 变异确认的常驻版：把 internal_limits 清掉，同一份数据必须通过 ——
    # 证明拒绝真的来自那一个字段，而不是夹具整体写坏。
    probe["retrieval"]["internal_limits"] = []
    validate_probe(probe)


# -------------------------------------------------------- Admission 回执

def test_old_admission_version_is_protocol_incompatible():
    receipt = load("valid", "admission-accepted.json")
    receipt["schema"] = "ddp-plan-admission/0#AdmissionReceipt"
    with pytest.raises(ApplicationError) as error:
        validate_receipt(receipt)
    assert error.value.code == "protocol_incompatible"


def test_unknown_admission_field_is_refused_not_ignored():
    receipt = load("valid", "admission-accepted.json")
    receipt["required_client_capabilities"] = ["future-thing"]
    with pytest.raises(ApplicationError) as error:
        validate_receipt(receipt)
    assert error.value.code == "protocol_incompatible"


def test_admission_state_is_not_blurred_when_input_was_not_verified():
    receipt = load("invalid", "admission-accepted-without-verified-input.json")
    with pytest.raises(ApplicationError) as error:
        validate_receipt(receipt)
    assert error.value.code == "input_not_verified"
