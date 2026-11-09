"""`GroundedAnswerStream` / `grounded_answer_schema` 的行为测试（grounded-claims/2）。

每条都钉着一个 plausible bug：切分相关的（增量解析最容易在这里错）、
放宽校验的（多一条引用就多一个假出处）、以及"违反发生时已产出部分丢失"的。
"""
import json

import pytest

from ddp_core.answer import (
    AnswerFormatError,
    GroundedAnswerStream,
    grounded_answer_schema,
)


def _doc(*claims: tuple[str, list[str]], unanswered: tuple[str, ...] = ()) -> str:
    payload: dict = {
        "claims": [
            {"text": text, "evidence_ids": ids} for text, ids in claims
        ],
        "unanswered": list(unanswered),
    }
    return json.dumps(payload, ensure_ascii=False)


def _run(doc: str, cuts: list[int], ids: list[str]) -> list[dict]:
    """按给定切分点喂完整个文档，返回收集到的全部 claim。"""
    stream = GroundedAnswerStream(ids)
    out: list[dict] = []
    prev = 0
    for cut in cuts:
        out.extend(stream.feed(doc[prev:cut]))
        prev = cut
    out.extend(stream.feed(doc[prev:]))
    out.extend(stream.finish())
    return out


IDS = ["ev-1", "ev-2"]


def test_fragmentation_does_not_change_result():
    doc = _doc(("第一条结论。", ["ev-1"]), ("第二条结论 [1]。", ["ev-2", "ev-1"]))
    whole = _run(doc, [], IDS)
    assert [c["text"] for c in whole] == ["第一条结论。", "第二条结论 [1]。"]
    assert whole[0]["evidence_ids"] == ["ev-1"]
    # 去重保首序：重复 id 只留第一次出现的位置
    assert whole[1]["evidence_ids"] == ["ev-2", "ev-1"]
    assert [c["position"] for c in whole] == [0, 1]
    assert all(c["unsupported"] is False for c in whole)

    char_by_char = _run(doc, list(range(1, len(doc))), IDS)
    assert char_by_char == whole
    for cut in range(len(doc) + 1):
        assert _run(doc, [cut], IDS) == whole, f"2-way split at {cut} diverged"


def test_escapes_and_split_surrogate_pair_decode_identically():
    doc = json.dumps(
        {
            "claims": [
                {
                    "text": "引号\"反斜杠\\换行\n雪人☃é\\u全角Ａ「」",
                    "evidence_ids": ["ev-1"],
                },
                {"text": "emoji A\U0001F600B 全角空格\u3000尾", "evidence_ids": ["ev-2"]},
            ],
            "unanswered": [],
        },
        ensure_ascii=False,
    )
    # emoji 以 \uXXXX 代理对形式出现，且切分断在代理对与转义中间时也不许错
    raw = json.dumps(
        {"claims": [{"text": "A\U0001F600B", "evidence_ids": ["ev-1"]}], "unanswered": []},
        ensure_ascii=True,
    )
    assert "\\ud83d" in raw.lower()
    whole = _run(raw, [], ["ev-1"])
    assert whole[0]["text"] == "A\U0001F600B"
    for cut in range(len(raw) + 1):
        assert _run(raw, [cut], ["ev-1"]) == whole, f"split at {cut} diverged"
    assert _run(doc, list(range(1, len(doc))), IDS) == _run(doc, [], IDS)


def test_first_claim_streams_before_document_is_complete():
    doc = '{"claims": [{"text": "先出的结论", "evidence_ids": ["ev-1"]}, {"text": "后出的", "evidence_ids": ["ev-2"]}], "unanswered": []}'
    cut = doc.index("}, {") + 1  # 第一条 claim 的 } 刚闭合
    stream = GroundedAnswerStream(IDS)
    first = stream.feed(doc[:cut])
    assert [c["text"] for c in first] == ["先出的结论"]
    rest = stream.feed(doc[cut:])
    assert [c["text"] for c in rest] == ["后出的"]
    assert stream.finish() == []
    assert stream.insufficient_evidence is False


def test_duplicate_top_level_key_is_rejected():
    # 违反点之前已有合法 claim：当次 feed 先返回它，违反留到下一次调用再抛
    raw = _doc(("a", ["ev-1"]))[:-1] + ', "unanswered": []}'
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(raw)] == ["a"]
    with pytest.raises(AnswerFormatError):
        stream.finish()
    # 违反点之前没有合法 claim 时当次就抛
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('{"claims": [], "claims": [], "unanswered": []}')


def test_duplicate_claim_key_is_rejected():
    raw = '{"claims": [{"text": "a", "text": "b", "evidence_ids": ["ev-1"]}], "unanswered": []}'
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(raw)


def test_unknown_top_level_key_is_rejected():
    # 违反点之前已有合法 claim：当次 feed 先返回它，违反留到下一次调用再抛
    raw = '{"claims": [{"text": "a", "evidence_ids": ["ev-1"]}], "unanswered": [], "note": "x"}'
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(raw)] == ["a"]
    with pytest.raises(AnswerFormatError):
        stream.finish()
    # 违反点之前没有合法 claim 时当次就抛
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('{"note": "x", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}], "unanswered": []}')


def test_unknown_claim_key_is_rejected():
    raw = '{"claims": [{"text": "a", "evidence_ids": ["ev-1"], "score": 0.9}], "unanswered": []}'
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(raw)


def test_unknown_evidence_id_is_rejected():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(("a", ["ev-1", "ev-nope"])))


def test_empty_evidence_ids_is_rejected():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(("a", [])))


def test_non_string_evidence_id_is_rejected():
    raw = '{"claims": [{"text": "a", "evidence_ids": [1]}], "unanswered": []}'
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(raw)


def test_empty_text_is_rejected():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(("   ", ["ev-1"])))


def test_missing_claim_member_is_rejected():
    for claim in ('{"text": "a"}', '{"evidence_ids": ["ev-1"]}'):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(f'{{"claims": [{claim}], "unanswered": []}}')


def test_both_arrays_empty_is_rejected_as_bare_result():
    for raw in ('{"claims": [], "unanswered": []}', '{"unanswered": [], "claims": []}'):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(raw)
            stream.finish()


def test_missing_array_is_rejected():
    for raw in ('{"claims": [{"text": "a", "evidence_ids": ["ev-1"]}]}',
                '{"unanswered": ["缺依据。"]}',
                '{"claims": []}'):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(raw)
            stream.finish()


def test_legacy_status_is_an_unknown_key_without_compat():
    for raw in ('{"status": "answered", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}], "unanswered": []}',
                '{"status": "insufficient_evidence", "claims": [], "unanswered": ["缺依据。"]}',
                '{"status": "maybe", "claims": [], "unanswered": ["缺依据。"]}'):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(raw)
            stream.finish()


def test_insufficient_is_not_set_by_feed_alone():
    stream = GroundedAnswerStream(IDS)
    stream.feed('{"claims": [], "unanswered": ["缺依据。"]}')
    assert stream.insufficient_evidence is False


def test_trailing_garbage_and_second_object_are_rejected():
    good = _doc(("a", ["ev-1"]))
    for raw in (good + " xyz", good + good, good + '"x"'):
        stream = GroundedAnswerStream(IDS)
        # 尾随数据在顶层 } 之后才可判定：当次先返回已闭合的 claim，违反留到下一次
        assert [c["text"] for c in stream.feed(raw)] == ["a"]
        with pytest.raises(AnswerFormatError):
            stream.finish()


def test_code_fence_and_prose_are_rejected():
    good = _doc(("a", ["ev-1"]))
    for raw in ("```json\n" + good + "\n```", "Here is the answer: " + good, good + "\nDone."):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(raw)
            stream.finish()


def test_top_level_array_is_rejected():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('[{"text": "a", "evidence_ids": ["ev-1"]}]')




def test_finish_on_empty_input_is_truncation():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.finish()


def test_valid_claims_survive_a_later_violation_in_one_feed():
    raw = _doc(("好的", ["ev-1"]), ("坏的", ["ev-nope"]))
    stream = GroundedAnswerStream(IDS)
    # 一次调用又返回又抛不可能：先拿到合法的，违反留到下一次调用
    assert [c["text"] for c in stream.feed(raw)] == ["好的"]
    with pytest.raises(AnswerFormatError):
        stream.finish()
def test_valid_claims_survive_a_later_violation_across_feeds():
    good_prefix = '{"claims": [{"text": "好的", "evidence_ids": ["ev-1"]}, '
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(good_prefix)] == ["好的"]
    # 违反在第二次 feed 才可判定：之前没有新闭合的 claim，当次就抛
    with pytest.raises(AnswerFormatError):
        stream.feed('{"text": "坏的", "evidence_ids": ["ev-nope"]}], "unanswered": []}')
    with pytest.raises(AnswerFormatError):
        stream.finish()


def test_stream_is_closed_after_error_or_finish():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed("not json")
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(("a", ["ev-1"])))
    with pytest.raises(AnswerFormatError):
        stream.finish()

    done = GroundedAnswerStream(IDS)
    done.feed(_doc(("a", ["ev-1"])))
    done.finish()
    with pytest.raises(AnswerFormatError):
        done.feed(" ")
    with pytest.raises(AnswerFormatError):
        done.finish()


def test_claim_text_too_long_is_rejected():
    from ddp_core import answer as mod

    ok = "x" * mod.MAX_CLAIM_TEXT_CHARS
    stream = GroundedAnswerStream(IDS)
    out = stream.feed(_doc((ok, ["ev-1"])))
    out.extend(stream.finish())
    assert out[0]["text"] == ok

    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(("x" * (mod.MAX_CLAIM_TEXT_CHARS + 1), ["ev-1"])))


def test_too_many_claims_is_rejected():
    from ddp_core import answer as mod

    claims = [(f"c{i}", ["ev-1"]) for i in range(mod.MAX_CLAIMS + 1)]
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(*claims))
        stream.finish()

    crowded = [(f"c{i}", ["ev-1"]) for i in range(mod.MAX_CLAIMS)]
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(_doc(*crowded, unanswered=("还有一处缺依据。",)))
        stream.finish()

def test_total_input_bound_is_enforced():
    from ddp_core import answer as mod

    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(" " * (mod.MAX_TOTAL_CHARS + 1))


def test_bracket_marker_in_text_creates_no_binding():
    stream = GroundedAnswerStream(IDS)
    out = stream.feed(_doc(("结果见 [2]，与 [1] 无关", ["ev-1"])))
    out.extend(stream.finish())
    assert out[0]["text"] == "结果见 [2]，与 [1] 无关"
    assert out[0]["evidence_ids"] == ["ev-1"]


def test_schema_constrains_evidence_ids_to_visible_set():
    schema = grounded_answer_schema(["a", "b"])
    assert schema["required"] == ["claims", "unanswered"]
    assert schema["additionalProperties"] is False
    claim = schema["properties"]["claims"]["items"]
    # 非法 id 不在 enum 里：guided decoding 根本写不出它，解码器是第二道门
    assert claim["properties"]["evidence_ids"]["items"] == {"type": "string", "enum": ["a", "b"]}
    assert schema["properties"]["unanswered"]["items"] == {"type": "string", "minLength": 1}
    assert "status" not in schema["properties"]
    assert "anyOf" not in schema
    for node in (schema, claim):
        assert node["additionalProperties"] is False
    conflicted = grounded_answer_schema(["a", "b"], allow_conflicts=True)
    assert set(conflicted["properties"]) == {"claims", "unanswered", "conflicts"}
    assert "conflicts" not in schema["properties"]


def test_schema_has_no_string_length_bounds_the_llama_cpp_grammar_cannot_compile():
    """结构断言，不是行为测试：能证明「可编译」的只有真 llama.cpp（单测里没有它）。

    2026-09-24 真 qwen3-1.7b 实测：`text.maxLength: 2000` 让每个问答请求都 400
    「failed to parse grammar」，去掉后同一请求正常出 JSON。长度上界由解码器执行。
    """
    def walk(node):
        if isinstance(node, dict):
            assert "maxLength" not in node, node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(grounded_answer_schema(["a", "b"]))


def test_conflicts_survive_fragmentation_and_are_not_claims():
    doc = json.dumps({
        "conflicts": [{"evidence_ids": ["ev-2", "ev-1", "ev-2"]}],
        "claims": [{"text": "The sources disagree on voltage.", "evidence_ids": IDS}],
        "unanswered": [],
    })
    stream = GroundedAnswerStream(IDS, allow_conflicts=True)
    claims = []
    for fragment in doc:
        claims.extend(stream.feed(fragment))
    claims.extend(stream.finish())
    assert [claim["text"] for claim in claims] == ["The sources disagree on voltage."]
    assert stream.conflicts == [["ev-2", "ev-1"]]


@pytest.mark.parametrize("conflicts", [
    [{"evidence_ids": ["ev-1", "foreign"]}],
    [{"evidence_ids": ["ev-1", "ev-1"]}],
    [{"evidence_ids": []}],
    [{"evidence_ids": ["ev-1", "ev-2"], "text": "None found"}],
    [{"evidence_ids": "ev-1"}],
    [None],
    {},
])
def test_malformed_conflicts_invalidate_the_complete_document(conflicts):
    doc = json.dumps({
        "claims": [{"text": "A fact.", "evidence_ids": ["ev-1"]}],
        "unanswered": [],
        "conflicts": conflicts,
    })
    stream = GroundedAnswerStream(IDS, allow_conflicts=True)
    with pytest.raises(AnswerFormatError):
        stream.feed(doc)
        stream.finish()
    assert stream.conflicts == []


def test_single_center_decoder_does_not_accept_unrequested_conflicts():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(json.dumps({
            "claims": [{"text": "The sources disagree.", "evidence_ids": IDS}],
            "unanswered": [],
            "conflicts": [{"evidence_ids": IDS}],
        }))
        stream.finish()


@pytest.mark.parametrize("order", ["claims_first", "conflicts_first"])
@pytest.mark.parametrize("conflicts", [[], [{"evidence_ids": IDS}]])
def test_empty_facts_must_not_carry_conflicts_key(order, conflicts):
    items = [
        ("claims", []), ("unanswered", ["现有证据不足以确定保修年限。"]),
        ("conflicts", conflicts),
    ]
    if order == "conflicts_first":
        items.reverse()
    stream = GroundedAnswerStream(IDS, allow_conflicts=True)
    with pytest.raises(AnswerFormatError):
        stream.feed(json.dumps(dict(items)))
        stream.finish()


@pytest.mark.parametrize("conflicts_first", [False, True])
def test_conflict_sources_must_be_cited_by_answer_claims(conflicts_first):
    claims = [{"text": "One source rates PM-2 at 240 V.", "evidence_ids": ["ev-1"]}]
    conflicts = [{"evidence_ids": IDS}]
    pairs = [("claims", claims), ("unanswered", []), ("conflicts", conflicts)]
    if conflicts_first:
        pairs.reverse()
    stream = GroundedAnswerStream(IDS, allow_conflicts=True)
    with pytest.raises(AnswerFormatError):
        stream.feed(json.dumps(dict(pairs)))
        stream.finish()
    assert stream.conflicts == []


def test_partial_answer_preserves_supported_fact_and_explicit_missing_part():
    raw = json.dumps({
        "claims": [{"text": "额定电压是220 V。", "evidence_ids": ["ev-1"]}],
        "unanswered": ["现有资料不足以确定保修年限。"],
    }, ensure_ascii=False)
    expected = [
        {"position": 0, "text": "额定电压是220 V。", "evidence_ids": ["ev-1"], "unsupported": False},
        {"position": 1, "text": "现有资料不足以确定保修年限。", "evidence_ids": [], "unsupported": True},
    ]
    assert _run(raw, [], IDS) == expected
    for cut in range(len(raw) + 1):
        assert _run(raw, [cut], IDS) == expected


@pytest.mark.parametrize("unanswered", [
    [""], ["   "], [3], [{"text": "缺资料", "evidence_ids": ["ev-1"]}],
    ["x" * 2001], ["missing"] * 64,
])
def test_invalid_missing_parts_do_not_relax_factual_claim_requirements(unanswered):
    raw = json.dumps({
        "claims": [{"text": "额定电压是220 V。", "evidence_ids": ["ev-1"]}],
        "unanswered": unanswered,
    })
    decoder = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        decoder.feed(raw)
        decoder.finish()


def test_missing_part_is_not_emitted_from_truncated_document():
    decoder = GroundedAnswerStream(IDS)
    raw = '{"unanswered":["保修期限未知"],"claims":['
    assert decoder.feed(raw) == []
    with pytest.raises(AnswerFormatError):
        decoder.finish()


def test_missing_subquestions_are_explicit_without_fabricated_claims():
    missing = ["现有证据不足以确定官方价格。", "现有证据不足以确定最小起订量。"]
    raw = json.dumps({"claims": [], "unanswered": missing}, ensure_ascii=False)
    for cut in (0, len(raw) // 2, len(raw)):
        stream = GroundedAnswerStream(IDS)
        assert stream.feed(raw[:cut]) == []
        assert stream.feed(raw[cut:]) == []
        assert stream.insufficient_evidence is False
        assert stream.finish() == [
            {"position": index, "text": text, "evidence_ids": [], "unsupported": True}
            for index, text in enumerate(missing)
        ]
        assert stream.insufficient_evidence is True
