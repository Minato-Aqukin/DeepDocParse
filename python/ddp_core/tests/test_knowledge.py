import pytest


from ddp_core.knowledge import (
    EntityMention, edge_result, merge_mentions, neighbor_ids, normalize_entity_name,
    wiki_sentence,
)


def test_entity_normalization_and_explicit_alias_merge_are_auditable():
    assert normalize_entity_name(" Qwen3-VL / 8B ") == "qwen3vl8b"
    groups = merge_mentions([
        EntityMention("DeepDocParse", "system", ("DDP",)),
        EntityMention("DDP", "system"),
    ])
    assert len(groups) == 1
    assert groups[0].merged_by == "alias" and "DDP" in groups[0].aliases


def test_similar_but_unconfirmed_names_are_not_silently_merged():
    groups = merge_mentions([
        EntityMention("Model-1", "model"), EntityMention("Model-2", "model")])
    assert len(groups) == 2


def test_low_confidence_model_alias_is_visible_and_splittable():
    group = merge_mentions([
        EntityMention("DeepDocParse", "system", ("DDP",), "model", 0.48),
    ])[0]
    assert group.merged_by == "model"
    assert group.merge_confidence == 0.48
    assert group.entity_merge_uncertain is True


def test_negative_relation_returns_not_found_instead_of_inventing_edge():
    result = edge_result(subject_id="a", predicate="", object_id=None,
                         evidence_ids=[], confidence=0.9, provider={"model": "fixture"})
    assert result == {"status": "not_found", "edge": None}


def test_edge_and_wiki_without_evidence_are_type_level_unsupported():
    edge = edge_result(subject_id="a", predicate="uses", object_id="b",
                       evidence_ids=[], confidence=2, provider={"model": "fixture"})["edge"]
    sentence = wiki_sentence(text="生成句。", evidence_ids=[])
    assert edge["unsupported"] is True and edge["confidence"] == 1.0
    assert sentence["unsupported"] is True and sentence["evidence_ids"] == []


def test_neighbor_depth_is_bounded_and_deterministic():
    edges = [("a", "b"), ("b", "c"), ("c", "d")]
    assert neighbor_ids("a", edges, 1) == {"a", "b"}
    assert neighbor_ids("a", edges, 2) == {"a", "b", "c"}
    with pytest.raises(ValueError):
        neighbor_ids("a", edges, 4)
