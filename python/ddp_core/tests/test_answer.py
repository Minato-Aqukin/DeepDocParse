"""`GroundedAnswerStream` / `grounded_answer_schema` 的行为测试。

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


def _doc(*claims: tuple[str, list[str]], status: str = "answered") -> str:
    payload: dict = {"status": status}
    if claims or status == "answered":
        payload["claims"] = [
            {"text": text, "evidence_ids": ids} for text, ids in claims
        ]
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
            "status": "answered",
            "claims": [
                {
                    "text": "引号\"反斜杠\\换行\n雪人☃é\\u全角Ａ「」",
                    "evidence_ids": ["ev-1"],
                },
                {"text": "emoji A\U0001F600B 全角空格\u3000尾", "evidence_ids": ["ev-2"]},
            ],
        },
        ensure_ascii=False,
    )
    # emoji 以 \uXXXX 代理对形式出现，且切分断在代理对与转义中间时也不许错
    raw = json.dumps(
        {"status": "answered", "claims": [{"text": "A\U0001F600B", "evidence_ids": ["ev-1"]}]},
        ensure_ascii=True,
    )
    assert "\\ud83d" in raw.lower()
    whole = _run(raw, [], ["ev-1"])
    assert whole[0]["text"] == "A\U0001F600B"
    for cut in range(len(raw) + 1):
        assert _run(raw, [cut], ["ev-1"]) == whole, f"split at {cut} diverged"
    assert _run(doc, list(range(1, len(doc))), IDS) == _run(doc, [], IDS)


def test_first_claim_streams_before_status_is_known():
    doc = '{"claims": [{"text": "先出的结论", "evidence_ids": ["ev-1"]}, {"text": "后出的", "evidence_ids": ["ev-2"]}], "status": "answered"}'
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
    raw = _doc(("a", ["ev-1"]))[:-1] + ', "status": "answered"}'
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(raw)] == ["a"]
    with pytest.raises(AnswerFormatError):
        stream.finish()
    # 违反点之前没有合法 claim 时当次就抛
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('{"status": "answered", "status": "answered", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}]}')


def test_duplicate_claim_key_is_rejected():
    raw = '{"status": "answered", "claims": [{"text": "a", "text": "b", "evidence_ids": ["ev-1"]}]}'
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(raw)


def test_unknown_top_level_key_is_rejected():
    # 违反点之前已有合法 claim：当次 feed 先返回它，违反留到下一次调用再抛
    raw = '{"status": "answered", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}], "note": "x"}'
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(raw)] == ["a"]
    with pytest.raises(AnswerFormatError):
        stream.finish()
    # 违反点之前没有合法 claim 时当次就抛
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('{"note": "x", "status": "answered", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}]}')


def test_unknown_claim_key_is_rejected():
    raw = '{"status": "answered", "claims": [{"text": "a", "evidence_ids": ["ev-1"], "score": 0.9}]}'
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
    raw = '{"status": "answered", "claims": [{"text": "a", "evidence_ids": [1]}]}'
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
            stream.feed(f'{{"status": "answered", "claims": [{claim}]}}')


def test_answered_with_empty_claims_is_rejected():
    for raw in ('{"status": "answered", "claims": []}', '{"status": "answered"}'):
        stream = GroundedAnswerStream(IDS)
        with pytest.raises(AnswerFormatError):
            stream.feed(raw)


def test_insufficient_evidence_with_claims_is_rejected():
    raw = '{"status": "insufficient_evidence", "claims": [{"text": "a", "evidence_ids": ["ev-1"]}]}'
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed(raw)


def test_claims_before_insufficient_status_is_rejected_but_kept():
    raw = '{"claims": [{"text": "a", "evidence_ids": ["ev-1"]}], "status": "insufficient_evidence"}'
    stream = GroundedAnswerStream(IDS)
    # status 到之前 claim 已合法闭合：先返回它，违反留到 finish 再抛
    assert [c["text"] for c in stream.feed(raw)] == ["a"]
    with pytest.raises(AnswerFormatError):
        stream.finish()


def test_bare_insufficient_evidence_refuses():
    for raw in ('{"status": "insufficient_evidence"}', '{"status": "insufficient_evidence", "claims": []}'):
        stream = GroundedAnswerStream(IDS)
        assert stream.feed(raw) == []
        assert stream.finish() == []
        assert stream.insufficient_evidence is True


def test_insufficient_is_not_set_by_feed_alone():
    stream = GroundedAnswerStream(IDS)
    stream.feed('{"status": "insufficient_evidence"}')
    assert stream.insufficient_evidence is False


def test_unknown_status_is_rejected():
    stream = GroundedAnswerStream(IDS)
    with pytest.raises(AnswerFormatError):
        stream.feed('{"status": "maybe"}')


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


def test_truncation_at_finish_is_an_error_not_a_partial_answer():
    doc = _doc(("a", ["ev-1"]), ("b", ["ev-2"]))
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(doc[: len(doc) // 2])] == []
    with pytest.raises(AnswerFormatError):
        stream.finish()


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
    good_prefix = '{"status": "answered", "claims": [{"text": "好的", "evidence_ids": ["ev-1"]}, '
    stream = GroundedAnswerStream(IDS)
    assert [c["text"] for c in stream.feed(good_prefix)] == ["好的"]
    # 违反在第二次 feed 才可判定：之前没有新闭合的 claim，当次就抛
    with pytest.raises(AnswerFormatError):
        stream.feed('{"text": "坏的", "evidence_ids": ["ev-nope"]}]}')
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
    answered, insufficient = schema["anyOf"]
    claim = answered["properties"]["claims"]["items"]
    # 非法 id 不在 enum 里：guided decoding 根本写不出它，解码器是第二道门
    assert claim["properties"]["evidence_ids"]["items"] == {"type": "string", "enum": ["a", "b"]}
    assert answered["properties"]["claims"]["minItems"] == 1
    assert insufficient["properties"]["status"] == {"type": "string", "enum": ["insufficient_evidence"]}
    for node in (answered, insufficient, claim):
        assert node["additionalProperties"] is False


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
