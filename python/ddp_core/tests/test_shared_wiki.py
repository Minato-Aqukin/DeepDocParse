import copy
import hashlib

import pytest

from ddp_core.application.ports import ApplicationError
from ddp_core.application.wiki import limits_for, normalize_pages, normalize_plan, preserve_human_pages, validate_original_evidence


@pytest.fixture
def evidence():
    text = 'Collector forwards batches to Validator.'
    return [{'id': 'original-1', 'excerpt': text, 'evidence': {'source_type': 'source',
        'derived_from': None, 'source_version_id': 'fixed', 'locator': {'physical_page_index': 0, 'bbox': [1, 2, 3, 4]},
        'excerpt_digest': 'sha256:' + hashlib.sha256(text.encode()).hexdigest()}}]


def test_source_only_wiki_rejects_generated_self_citation_and_corrupt_excerpt(evidence):
    limits = limits_for({})
    validate_original_evidence(evidence, limits)
    forged = copy.deepcopy(evidence)
    forged[0]['evidence']['source_type'] = 'generated'
    with pytest.raises(ApplicationError) as generated:
        validate_original_evidence(forged, limits)
    assert generated.value.code == 'wiki_source_unavailable'
    evidence[0]['excerpt'] = 'Changed text'
    with pytest.raises(ApplicationError):
        validate_original_evidence(evidence, limits)


def test_plan_and_relations_bind_only_existing_originals(evidence):
    limits = limits_for({})
    plan = normalize_plan({'pages': [{'title': 'Collector', 'references': [1]},
                                    {'title': 'Validator', 'references': [1]}]}, evidence, limits)
    raw = {'pages': [{'page': i, 'sections': [{'heading': 'Facts', 'sentences': [{'text': 'A supported claim.',
            'references': [1], 'source_type': 'source'}]}]} for i in (1, 2)],
        'relations': [{'subject_page': 1, 'object_page': 2, 'predicate': 'forwards batches to', 'references': [1]}]}
    pages, edges = normalize_pages(raw, plan, evidence, {'name': 'fixture'})
    assert pages[0]['generated_sections'][0]['sentences'][0]['source_type'] == 'generated'
    assert edges[0]['evidence_ids'] == ['original-1'] and not edges[0]['unsupported']
    raw['relations'][0]['references'] = [2]
    with pytest.raises(ApplicationError) as unknown:
        normalize_pages(raw, plan, evidence, {})
    assert unknown.value.code == 'unsupported_generation'
    raw['relations'][0]['references'] = [1]
    raw['relations'][0]['object_page'] = 9
    with pytest.raises(ApplicationError):
        normalize_pages(raw, plan, evidence, {})


def test_manual_pages_never_disappear_to_satisfy_page_budget():
    old = [{'page_key': 'old', 'human_paragraphs': [{'id': 'human', 'text': 'keep'}]}]
    with pytest.raises(ApplicationError) as budget:
        preserve_human_pages([{'page_key': 'new'}], old, 1)
    assert budget.value.code == 'wiki_budget_exceeded'
    pages, conflicts = preserve_human_pages([{'page_key': 'new'}], old, 2)
    assert pages[-1]['human_paragraphs'][0]['text'] == 'keep' and conflicts
    assert old[0]['human_paragraphs'][0]['text'] == 'keep'


def test_relation_placeholder_cannot_pass_using_a_real_citation(evidence):
    plan = normalize_plan({'pages': [{'title': 'Collector', 'references': [1]},
                                    {'title': 'Validator', 'references': [1]}]}, evidence, limits_for({}))
    raw = {'pages': [{'page': i, 'sections': [{'heading': 'Facts', 'sentences': [{'text': 'A claim.',
             'references': [1]}]}]} for i in (1, 2)],
           'relations': [{'subject_page': 1, 'object_page': 2, 'predicate': 'relationship', 'references': [1]}]}
    with pytest.raises(ApplicationError) as placeholder:
        normalize_pages(raw, plan, evidence, {})
    assert placeholder.value.code == 'wiki_relation_unsupported'


def test_bare_relation_id_array_is_only_an_equivalent_envelope():
    from ddp_core.application.wiki import object_from_output, selected_relations
    actual = '```json\n[1,2]\n```'
    parsed = object_from_output(actual, array_field='selected_relations')
    assert parsed == {'selected_relations': [1, 2]}
    candidates = [{'id': 1, 'predicate': 'original one'}, {'id': 2, 'predicate': 'original two'}]
    assert selected_relations(parsed, candidates) == candidates
    with pytest.raises(ApplicationError):
        object_from_output(actual)
    with pytest.raises(ApplicationError):
        selected_relations(object_from_output('[99]', array_field='selected_relations'), candidates)
    with pytest.raises(ApplicationError):
        selected_relations(object_from_output('[{"made_up":1}]', array_field='selected_relations'), candidates)
