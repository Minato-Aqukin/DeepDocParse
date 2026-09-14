"""P6 评测夹具：冻结的合成联邦网络与标注问题集。

这个模块**只造数据、不跑评测**。它生成一张确定性的合成网络（6 个逻辑节点、
12 个公开集合 + 1 个私有集合、每问独立的小文档），把人工标注的问题冻结成
JSON 并带上内容摘要，好让任何一次运行都能证明自己跑的是同一份数据。

标注口径（与计划 §14.1 一致）：
- 每题声明 `required_evidence`（该题答案必须指回的证据 id）与
  `evidence_collections`（证据所在的 `节点/集合`）；
- `decoy_evidence` 是"内容相似但不是答案"的证据，用来检查系统有没有把
  诱饵当答案（本轮只如实报告它是否被召回，不记违规）；
- `private_decoy_evidence` 住在**任何 scope 都不包含**的私有集合里，
  任何一次探测或返回都是覆盖诚实性违规；
- `conflict` 标记一组互相矛盾的必需证据（两版不同的参数值）。

冻结格式：`dataset_digest` 是**去掉该字段后**的 canonical JSON 摘要
（`ddp_core.application.plans.digest`）；文件本身用缩进 JSON 落盘，格式
变化不改变数据身份。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ddp_core.application import plans

from .executor import rank_evidence

FIXTURE_SCHEMA = "ddp-routing-eval/1#Fixture"
FIXTURE_REVISION = "routing-eval-fixture-v3.2026-09-01"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "federation-v3.json"
FROZEN_AT = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
SCOPE_REF = "scope-routing-eval-all"
LOCAL_NODE = "node-a"
OPERATION = "corpus.retrieve"
#: 与协调者 `_probe_request` 每个目标的候选上限一致；夹具里的集合都远小于它，
#: 所以预言机检索**永远不会因为候选上限截断必需证据** —— 召回差异只能来自
#: 目标选择，不能来自检索截断。
PROBE_CANDIDATE_LIMIT = 8

#: 计划 §14.1 要求覆盖的问题类别。每类至少 3 题（断言在 `_validate` 里）。
CLASSES = (
    "local-solvable",
    "b-only",
    "cross-collection-split",
    "nearest-node-decoy",
    "local-similar-decoy",
    "summary-hidden",
    "no-evidence-in-scope",
    "conflicting-versions",
    "private-decoy",
)

#: 合成网络：6 个节点 × 2 个公开集合。topics/languages 是集合摘要（只影响
#: 候选排序，不删成员 —— 这正是 `routing.candidates` 的契约）。
_PUBLIC_COLLECTIONS: dict[str, dict] = {
    "col-a-core": {"node": "node-a", "topics": ["manuals", "general"], "languages": ["en"]},
    "col-a-ops": {"node": "node-a", "topics": ["operations", "safety"], "languages": ["en"]},
    "col-b-hardware": {"node": "node-b", "topics": ["hardware", "power"], "languages": ["en"]},
    "col-b-service": {"node": "node-b", "topics": ["service", "calibration"], "languages": ["en"]},
    "col-c-thermal": {"node": "node-c", "topics": ["thermal", "cooling"], "languages": ["en"]},
    "col-c-power": {"node": "node-c", "topics": ["power", "electrical"], "languages": ["en"]},
    "col-d-software": {"node": "node-d", "topics": ["software", "firmware"], "languages": ["en"]},
    "col-d-network": {"node": "node-d", "topics": ["network", "connectivity"], "languages": ["en"]},
    "col-e-archive": {"node": "node-e", "topics": ["archive", "legacy"], "languages": ["en"]},
    "col-e-legal": {"node": "node-e", "topics": ["legal", "compliance"], "languages": ["en"]},
    "col-f-lab": {"node": "node-f", "topics": ["lab", "measurements"], "languages": ["en"]},
    # 摘要故意与问题无关：唯一证据藏在摘要看不出来的集合里（计划 T84）。
    "col-f-misc": {"node": "node-f", "topics": ["misc", "unlabeled"], "languages": ["en"]},
}
#: 私有集合：住在本地节点上（如果进了 scope，fast 会第一个探它），但从不进
#: 任何 scope manifest。任何探测/返回它都是违规。
_PRIVATE_COLLECTIONS: dict[str, dict] = {
    "col-a-private": {"node": "node-a", "topics": ["manuals", "general"], "languages": ["en"]},
}


def _evidence(collection: str, text: str) -> dict:
    return {"collection": collection, "text": text}


def _question(question_id: str, cls: str, query: str, *, notes: str,
              required=(), decoys=(), private_decoys=(), conflict=False,
              summary_hidden=False) -> dict:
    return {
        "question_id": question_id, "class": cls, "query": query, "notes": notes,
        "required": list(required), "decoys": list(decoys),
        "private_decoys": list(private_decoys), "conflict": conflict,
        "summary_hidden": summary_hidden,
    }


# ---------------------------------------------------------------------------
# 问题表：27 题 = 9 类 × 3。每题 `required` 是答案必须指回的证据。
# ---------------------------------------------------------------------------
_QUESTIONS: list[dict] = [
    # ---- local-solvable：证据只在本地（node-a），两种模式都应找到 ----
    _question(
        "q-local-01", "local-solvable",
        "What is the calibration interval for the AX-100 pressure sensor?",
        notes="本地手册直接给答案；fast 的 local_first 必须真的探到本地目标。",
        required=[_evidence("col-a-core",
                            "The AX-100 pressure sensor must be calibrated every 90 days.")],
        decoys=[_evidence("col-b-hardware",
                          "The AX-100 sensor connector matches the BX-200 probe.")]),
    _question(
        "q-local-02", "local-solvable",
        "Which fuse protects the bench power rail in the local workshop?",
        notes="本地运行安全集合里的唯一答案。",
        required=[_evidence("col-a-ops",
                            "The bench power rail is protected by a 5A slow-blow fuse.")],
        decoys=[_evidence("col-c-power",
                          "Bench power rails in the main hall use a 10A breaker.")]),
    _question(
        "q-local-03", "local-solvable",
        "How are spare seals stored at the local depot?",
        notes="本地存储规范；远程集合有相似措辞但不是答案。",
        required=[_evidence("col-a-ops",
                            "Spare seals are stored in a dry cabinet below 30 percent humidity.")],
        decoys=[_evidence("col-b-service",
                          "Calibration seals are shipped in a dry cabinet.")]),
    # ---- b-only：证据只在 node-b，fast 必须越出本地并探到 B ----
    _question(
        "q-bonly-01", "b-only",
        "What torque is specified for the BX-200 flange bolts?",
        notes="B 唯一；fast 排位在候选上限内。",
        required=[_evidence("col-b-service",
                            "Tighten BX-200 flange bolts to 42 Nm in a star pattern.")]),
    _question(
        "q-bonly-02", "b-only",
        "Which lubricant is approved for the node B gearbox?",
        notes="B 唯一；本地给的是别的润滑剂。",
        required=[_evidence("col-b-service",
                            "The gearbox uses synthetic ISO VG 220 lubricant.")],
        decoys=[_evidence("col-a-core",
                          "The local pump gearbox uses mineral grease on the test stand.")]),
    _question(
        "q-bonly-03", "b-only",
        "What is the replacement interval for the BX-200 intake filter?",
        notes="B 唯一；证据在硬件集合。",
        required=[_evidence("col-b-hardware",
                            "Replace the BX-200 intake filter every 500 operating hours.")]),
    # ---- cross-collection-split：答案需要两个集合各一条证据 ----
    _question(
        "q-cross-01", "cross-collection-split",
        "What are the alarm limit and wiring colour for the thermal probe?",
        notes="两条证据都在 node-c（fast 范围内），用来对比同节点的组合题。",
        required=[_evidence("col-c-thermal",
                            "Thermal probe alarm limit is 85 degrees."),
                  _evidence("col-c-power",
                            "The thermal probe wiring uses blue and brown conductors.")]),
    _question(
        "q-cross-02", "cross-collection-split",
        "Give the firmware checksum and the archive retention rule for the CN-7 build.",
        notes="一条在 node-d（fast 内），一条在 node-e（fast 候选上限外）→ fast 只能拿到一半。",
        required=[_evidence("col-d-software",
                            "CN-7 firmware checksum is 0x7a31."),
                  _evidence("col-e-archive",
                            "CN-7 build archives are retained for seven years.")]),
    _question(
        "q-cross-03", "cross-collection-split",
        "Give the legal hold rule and the lab calibration note for LOT-9.",
        notes="两条都在 fast 候选上限外（node-e/node-f）→ fast 拿不到。",
        required=[_evidence("col-e-legal",
                            "LOT-9 is under legal hold until the audit closes."),
                  _evidence("col-f-lab",
                            "LOT-9 calibration reference is the 1.0 ohm standard.")]),
    # ---- nearest-node-decoy：最近的节点有相似内容但没有答案 ----
    _question(
        "q-near-01", "nearest-node-decoy",
        "What is the shutdown temperature for the KP-5 reactor?",
        notes="node-c 有相似的热学句子（诱饵），答案在 node-e（fast 外）。",
        required=[_evidence("col-e-archive",
                            "The KP-5 reactor shutdown temperature is 120 degrees.")],
        decoys=[_evidence("col-c-thermal",
                          "KP-5 thermal paste cures at 120 degrees.")]),
    _question(
        "q-near-02", "nearest-node-decoy",
        "How long is the KP-5 reactor warm-up cycle?",
        notes="node-d 的启动时间相似（诱饵），答案在 node-f（fast 外）。",
        required=[_evidence("col-f-misc",
                            "The KP-5 warm-up cycle lasts 45 minutes.")],
        decoys=[_evidence("col-d-software",
                          "The KP-5 controller boots in 45 seconds.")]),
    _question(
        "q-near-03", "nearest-node-decoy",
        "Which material is the KP-5 gasket made from?",
        notes="诱饵在 node-b，答案在 node-d（fast 内）→ 本题 fast 命中，证明诱饵不阻断。",
        required=[_evidence("col-d-network",
                            "The KP-5 gasket material is nitrile rubber.")],
        decoys=[_evidence("col-b-hardware",
                          "The KP-5 gasket ships with a nitrile washer.")]),
    # ---- local-similar-decoy：本地有相似命中，真实证据在远端 ----
    _question(
        "q-ldecoy-01", "local-similar-decoy",
        "What is the seal torque for the NW-3 pump?",
        notes="本地诱饵（node-a）只是「检查在本地」，答案在 node-e。",
        required=[_evidence("col-e-archive",
                            "The NW-3 pump seal torque is 18 Nm.")],
        decoys=[_evidence("col-a-core",
                          "NW-3 pump seal inspection is performed locally.")]),
    _question(
        "q-ldecoy-02", "local-similar-decoy",
        "What voltage does the NW-3 pump use?",
        notes="本地诱饵提到同样的电压值，答案在 node-f。",
        required=[_evidence("col-f-misc",
                            "The NW-3 pump uses 230 V AC.")],
        decoys=[_evidence("col-a-ops",
                          "NW-3 pump lockout uses a local 230 V tag.")]),
    _question(
        "q-ldecoy-03", "local-similar-decoy",
        "What is the NW-3 impeller diameter?",
        notes="本地诱饵相似，答案在 node-c（fast 内）→ 本题 fast 命中。",
        required=[_evidence("col-c-thermal",
                            "The NW-3 impeller diameter is 160 mm.")],
        decoys=[_evidence("col-a-core",
                          "NW-3 impeller inspection is performed locally.")]),
    # ---- summary-hidden：唯一关键证据在摘要看不出来的集合（col-f-misc）----
    _question(
        "q-hidden-01", "summary-hidden",
        "Find the derating curve for the QX-9 capacitor.",
        notes="col-f-misc 摘要写的是 misc/unlabeled，关键词排序也看不见它。",
        required=[_evidence("col-f-misc",
                            "The QX-9 capacitor derating curve drops to 60 percent at 85 C.")],
        summary_hidden=True),
    _question(
        "q-hidden-02", "summary-hidden",
        "What humidity range applies to the QX-9 storage bay?",
        notes="唯一证据在摘要缺失的集合；穷查必须靠身份顺序也会触达。",
        required=[_evidence("col-f-misc",
                            "The QX-9 storage bay is approved for 20 to 60 percent humidity.")],
        summary_hidden=True),
    _question(
        "q-hidden-03", "summary-hidden",
        "What bolt grade does the QX-9 bracket use?",
        notes="同上；fast 的候选上限先把它排除。",
        required=[_evidence("col-f-misc",
                            "The QX-9 bracket uses grade 8.8 bolts.")],
        summary_hidden=True),
    # ---- no-evidence-in-scope：范围内没有任何证据 ----
    _question(
        "q-none-01", "no-evidence-in-scope",
        "What does the ZX-404 flux modulator maintenance schedule say?",
        notes="没有任何集合含所需证据；穷查应 complete + insufficient，且不得编证据。",
        decoys=[]),
    _question(
        "q-none-02", "no-evidence-in-scope",
        "Where is the ZX-404 warranty extension recorded?",
        notes="同上；检索成功但零命中（evidence_set_ref=null）。"),
    _question(
        "q-none-03", "no-evidence-in-scope",
        "Which team owns the ZX-404 migration?",
        notes="同上；used_budget 里字节仍记 0（生产 P5 也没测字节）。"),
    # ---- conflicting-versions：两版互相矛盾的值都要拿到 ----
    _question(
        "q-conflict-01", "conflicting-versions",
        "What is the maximum input voltage for the PM-2 module?",
        notes="两版矛盾值都写在必需证据里；两条都在 fast 范围内。",
        required=[_evidence("col-c-power",
                            "The PM-2 maximum input voltage is 240 V."),
                  _evidence("col-d-software",
                            "The PM-2 maximum input voltage is 120 V.")],
        conflict=True),
    _question(
        "q-conflict-02", "conflicting-versions",
        "What is the rated speed of the PM-2 motor?",
        notes="同上；矛盾对都在 fast 范围内。",
        required=[_evidence("col-c-thermal",
                            "The PM-2 motor rated speed is 3000 rpm."),
                  _evidence("col-d-network",
                            "The PM-2 motor rated speed is 2800 rpm.")],
        conflict=True),
    _question(
        "q-conflict-03", "conflicting-versions",
        "What is the service life of the PM-2 bearing?",
        notes="第二版在 node-e（fast 外）→ fast 只能拿到一版，穷查两版都拿。",
        required=[_evidence("col-c-power",
                            "The PM-2 bearing service life is 20000 hours."),
                  _evidence("col-e-archive",
                            "The PM-2 bearing service life is 15000 hours.")],
        conflict=True),
    # ---- private-decoy：私有集合里的近似证据，从进不了 scope ----
    _question(
        "q-private-01", "private-decoy",
        "What is the master key rotation period for the SR-1 vault?",
        notes="私有集合里有一条措辞几乎相同的证据；它既不许被探，也不许被返回。",
        required=[_evidence("col-b-hardware",
                            "The SR-1 vault master key rotates every 30 days.")],
        private_decoys=[_evidence("col-a-private",
                                  "The SR-1 vault master key rotates every 30 days (internal copy).")]),
    _question(
        "q-private-02", "private-decoy",
        "What is the SR-1 vault door weight rating?",
        notes="同上；必需证据在 node-b。",
        required=[_evidence("col-b-service",
                            "The SR-1 vault door is rated for 900 kg.")],
        private_decoys=[_evidence("col-a-private",
                                  "The SR-1 vault door rating is 900 kg (internal copy).")]),
    _question(
        "q-private-03", "private-decoy",
        "Who signs the SR-1 access log?",
        notes="同上；必需证据在 node-d。",
        required=[_evidence("col-d-software",
                            "The SR-1 access log is signed by the shift supervisor.")],
        private_decoys=[_evidence("col-a-private",
                                  "The SR-1 access log is signed by the internal auditor.")]),
]


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _evidence_id(question_id: str, kind: str, index: int) -> str:
    return f"ev-{question_id}-{kind}{index:02d}"


def _build_manifest(members: list[dict]) -> dict:
    manifest = {
        "schema": "ddp-scope-coverage/1#ScopeManifest",
        "scope_id": SCOPE_REF,
        "caller_scope_hash": "sha256:" + hashlib.sha256(
            b"routing-eval-fixture-caller").hexdigest(),
        "created_at": _iso(FROZEN_AT),
        "valid_until": _iso(FROZEN_AT + timedelta(seconds=3600)),
        "registry_revision_vector": [
            {"node_id": node, "registry_revision": 1, "fetched_at": _iso(FROZEN_AT)}
            for node in sorted({member["origin_node_id"] for member in members})
        ],
        "expanded_members": sorted(
            members, key=lambda item: (item["origin_node_id"], item["collection_id"],
                                       item["operation"])),
        "unexpanded_subtrees": [],
        "enumeration_state": "sealed",
    }
    manifest["manifest_digest"] = plans.digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"})
    return manifest


def build_dataset() -> dict:
    """构造夹具并把内容摘要算进 `dataset_digest`；纯函数、无 I/O。"""
    collections: dict[str, dict] = {}
    for collection_id, spec in _PUBLIC_COLLECTIONS.items():
        collections[collection_id] = {
            "origin_node_id": spec["node"], "publication": "published",
            "topics": list(spec["topics"]), "languages": list(spec["languages"]),
            "index_revision": f"idx-{collection_id}-r1",
        }
    for collection_id, spec in _PRIVATE_COLLECTIONS.items():
        collections[collection_id] = {
            "origin_node_id": spec["node"], "publication": "private",
            "topics": list(spec["topics"]), "languages": list(spec["languages"]),
            "index_revision": f"idx-{collection_id}-r1",
        }
    scope_members = [
        {"origin_node_id": spec["node"], "collection_id": collection_id,
         "operation": OPERATION}
        for collection_id, spec in _PUBLIC_COLLECTIONS.items()
    ]
    manifest = _build_manifest(scope_members)

    evidence: dict[str, dict] = {}
    documents: dict[str, dict] = {}

    def add_evidence(evidence_id: str, spec: dict) -> None:
        collection = collections[spec["collection"]]
        document_id = f"doc-{evidence_id}"
        text = spec["text"]
        evidence[evidence_id] = {
            "evidence_id": evidence_id, "collection_id": spec["collection"],
            "origin_node_id": collection["origin_node_id"],
            "document_id": document_id, "text": text, "block_type": "text",
            "locator": {"kind": "page_block", "physical_page_index": 0,
                        "seq": len(documents) + 1, "bbox": [10, 10, 400, 40],
                        "page_size": {"width": 600, "height": 800}},
            "excerpt_digest": plans.content_digest(text.encode("utf-8")),
            "source_digest": plans.content_digest(f"source:{document_id}".encode("utf-8")),
            "parse_revision": f"parse-{document_id}",
        }
        documents.setdefault(document_id, {
            "document_id": document_id, "collection_id": spec["collection"],
            "title": f"Synthetic evidence document for {evidence_id}",
            "evidence_ids": [],
        })["evidence_ids"].append(evidence_id)

    questions: list[dict] = []
    for spec in _QUESTIONS:
        question_id = spec["question_id"]
        required, decoy_ids, private_ids = [], [], []
        for index, item in enumerate(spec["required"], start=1):
            evidence_id = _evidence_id(question_id, "r", index)
            add_evidence(evidence_id, item)
            required.append(evidence_id)
        for index, item in enumerate(spec["decoys"], start=1):
            evidence_id = _evidence_id(question_id, "d", index)
            add_evidence(evidence_id, item)
            decoy_ids.append(evidence_id)
        for index, item in enumerate(spec["private_decoys"], start=1):
            evidence_id = _evidence_id(question_id, "p", index)
            add_evidence(evidence_id, item)
            private_ids.append(evidence_id)
        questions.append({
            "question_id": question_id, "class": spec["class"],
            "query": spec["query"], "scope_ref": SCOPE_REF, "notes": spec["notes"],
            "required_evidence": required, "decoy_evidence": decoy_ids,
            "private_decoy_evidence": private_ids,
            "evidence_collections": {
                evidence_id: f"{evidence[evidence_id]['origin_node_id']}/"
                             f"{evidence[evidence_id]['collection_id']}"
                for evidence_id in required},
            "conflict": spec["conflict"], "summary_hidden": spec["summary_hidden"],
        })

    descriptors = [{
        "schema": "ddp-discovery/1#CollectionDescriptor",
        "origin_node_id": spec["node"], "collection_id": collection_id,
        "topics": list(spec["topics"]), "languages": list(spec["languages"]),
        "index_revision": f"idx-{collection_id}-r1", "revision": 1,
        "valid_until": _iso(FROZEN_AT + timedelta(seconds=3600)),
    } for collection_id, spec in _PUBLIC_COLLECTIONS.items()]

    dataset = {
        "schema": FIXTURE_SCHEMA, "revision": FIXTURE_REVISION,
        "frozen_at": _iso(FROZEN_AT), "local_node_id": LOCAL_NODE,
        "operation": OPERATION,
        "nodes": [
            {"node_id": node,
             "collections": sorted(collection_id for collection_id, spec
                                   in {**_PUBLIC_COLLECTIONS, **_PRIVATE_COLLECTIONS}.items()
                                   if spec["node"] == node)}
            for node in sorted({spec["node"] for spec in
                                {**_PUBLIC_COLLECTIONS, **_PRIVATE_COLLECTIONS}.values()})
        ],
        "collections": dict(sorted(collections.items())),
        "documents": [documents[key] for key in sorted(documents)],
        "evidence": dict(sorted(evidence.items())),
        "scopes": {SCOPE_REF: manifest},
        "descriptors": descriptors,
        "simulator": {"name": "fixture-oracle/v1",
                      "candidate_limit": PROBE_CANDIDATE_LIMIT,
                      "description": "returns every evidence of the target collection"},
        "questions": questions,
    }
    dataset["dataset_digest"] = dataset_digest(dataset)
    _validate(dataset)
    return dataset


def dataset_digest(dataset: dict) -> str:
    """数据身份：去掉摘要字段本身的 canonical JSON 摘要。"""
    if not isinstance(dataset, dict):
        raise ValueError("dataset must be an object")
    return plans.digest({key: value for key, value in dataset.items()
                         if key != "dataset_digest"})


def _validate(dataset: dict) -> None:
    """夹具自检：标注必须自洽，坏夹具不许被冻进仓库。"""
    if dataset["schema"] != FIXTURE_SCHEMA:
        raise ValueError("unexpected fixture schema")
    classes = [question["class"] for question in dataset["questions"]]
    if set(classes) != set(CLASSES):
        missing = sorted(set(CLASSES) - set(classes))
        unknown = sorted(set(classes) - set(CLASSES))
        raise ValueError(f"fixture class coverage mismatch: missing={missing} unknown={unknown}")
    for cls in CLASSES:
        if classes.count(cls) < 3:
            raise ValueError(f"class {cls} needs at least 3 questions")
    scope = dataset["scopes"][SCOPE_REF]
    if scope["enumeration_state"] != "sealed" or scope["unexpanded_subtrees"]:
        raise ValueError("fixture scope must be sealed with no unexpanded subtrees")
    in_scope = {(member["origin_node_id"], member["collection_id"])
                for member in scope["expanded_members"]}
    public = {collection_id for collection_id, spec in dataset["collections"].items()
              if spec["publication"] == "published"}
    private = {collection_id for collection_id, spec in dataset["collections"].items()
               if spec["publication"] == "private"}
    if not private:
        raise ValueError("fixture needs at least one private decoy collection")
    for collection_id in private:
        if any(collection_id == member["collection_id"]
               for member in scope["expanded_members"]):
            raise ValueError(f"private collection {collection_id} must not enter any scope")
    seen_required: set[str] = set()
    for question in dataset["questions"]:
        for evidence_id in question["required_evidence"]:
            meta = dataset["evidence"].get(evidence_id)
            if meta is None:
                raise ValueError(f"{question['question_id']} requires unknown {evidence_id}")
            if meta["collection_id"] not in public:
                raise ValueError(f"{question['question_id']} requires non-public evidence")
            pair = (meta["origin_node_id"], meta["collection_id"])
            if pair not in in_scope:
                raise ValueError(f"{question['question_id']} requires evidence outside scope")
            if evidence_id in seen_required:
                raise ValueError(f"required evidence {evidence_id} reused across questions")
            seen_required.add(evidence_id)
            annotated = question["evidence_collections"][evidence_id]
            if annotated != f"{meta['origin_node_id']}/{meta['collection_id']}":
                raise ValueError(f"{question['question_id']} annotation disagrees with fixture")
        for evidence_id in question["private_decoy_evidence"]:
            meta = dataset["evidence"].get(evidence_id)
            if meta is None or meta["collection_id"] not in private:
                raise ValueError(f"{question['question_id']} private decoy is not private")
        if question["conflict"] and len(question["required_evidence"]) != 2:
            raise ValueError(f"{question['question_id']} conflict needs exactly two sides")
        if not question["required_evidence"]:
            # "范围内无证据"必须真的零命中：所有集合的模拟检索都不得返回任何东西。
            for collection_id in dataset["collections"]:
                if rank_evidence(dataset, collection_id, question["query"],
                                 PROBE_CANDIDATE_LIMIT):
                    raise ValueError(
                        f"{question['question_id']} claims no evidence but "
                        f"{collection_id} returns a keyword hit")
            continue
        for evidence_id in question["required_evidence"]:
            meta = dataset["evidence"][evidence_id]
            ranked = rank_evidence(dataset, meta["collection_id"], question["query"],
                                   PROBE_CANDIDATE_LIMIT)
            if evidence_id not in ranked:
                # 只要该集合被探到，必需证据就必须在候选上限内 —— 否则召回
                # 差异会混入"检索截断"，这个评测就不再只测路由。
                raise ValueError(
                    f"{question['question_id']} cannot retrieve required {evidence_id} "
                    f"from {meta['collection_id']} under the fixture simulator")


def freeze(path: Path = FIXTURE_PATH) -> dict:
    """把构造出的夹具写成缩进 JSON；返回写入的数据。"""
    dataset = build_dataset()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    return dataset


def load_frozen(path: Path = FIXTURE_PATH) -> dict:
    """读冻结夹具并重算摘要；对不上就拒绝，绝不继续用坏数据。"""
    dataset = json.loads(path.read_text(encoding="utf-8"))
    declared = dataset.get("dataset_digest")
    actual = dataset_digest(dataset)
    if declared != actual:
        raise ValueError(f"frozen dataset digest mismatch: declared={declared} actual={actual}")
    return dataset


def clone_without_evidence(dataset: dict, evidence_ids: list[str]) -> dict:
    """复制夹具并移除指定证据条目（测试用的变异入口，不修改原对象）。"""
    mutated = copy.deepcopy(dataset)
    for evidence_id in evidence_ids:
        meta = mutated["evidence"].pop(evidence_id, None)
        if meta is None:
            raise KeyError(evidence_id)
        document = next(document for document in mutated["documents"]
                        if document["document_id"] == meta["document_id"])
        document["evidence_ids"] = [item for item in document["evidence_ids"]
                                    if item != evidence_id]
    return mutated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build/check the frozen routing fixture")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--write", action="store_true", help="freeze the built dataset")
    parser.add_argument("--check", action="store_true",
                        help="load the frozen fixture and verify its digest")
    args = parser.parse_args(argv)
    if args.write:
        built = freeze(args.fixture)
        print(f"wrote {args.fixture} ({built['dataset_digest']})")
    if args.check or not args.write:
        loaded = load_frozen(args.fixture)
        print(f"{args.fixture}: digest {loaded['dataset_digest']} verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
