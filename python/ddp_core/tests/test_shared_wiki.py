import copy
import hashlib
import json

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
    limits = limits_for({"max_pages": 2})
    plan = normalize_plan({'pages': [{'title': 'Collector', 'references': [1]},
                                    {'title': 'Validator', 'references': [1]}]}, evidence, limits)
    raw = {'pages': [{'page': i, 'sections': [{'heading': 'Facts', 'sentences': [{'text': 'A supported claim.',
            'references': [1], 'support': [{'reference': 1, 'quote': 'Collector forwards batches'}],
            'source_type': 'source'}]}]} for i in (1, 2)],
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
                                    {'title': 'Validator', 'references': [1]}]}, evidence, limits_for({"max_pages": 2}))
    raw = {'pages': [{'page': i, 'sections': [{'heading': 'Facts', 'sentences': [{'text': 'A claim.',
             'references': [1], 'support': [{'reference': 1, 'quote': 'Collector forwards batches'}]}]}]}
             for i in (1, 2)],
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


@pytest.mark.asyncio
async def test_pages_without_relation_candidates_do_not_require_another_model_decision(evidence):
    from ddp_core.application.wiki import generate_wiki

    topics = ['Data collection overview', 'Validation workflow']

    class Provider:
        async def generate(self, messages, **kwargs):
            stage = json.loads(messages[-1]['content'])['stage']
            if stage == 'plan':
                value = {'pages': [{'title': title, 'references': [1]} for title in topics]}
            elif stage == 'write':
                value = {'pages': [{'page': number, 'sections': [{'heading': 'Facts',
                    'sentences': [{'text': evidence[0]['excerpt'],
                                   'support': ['q1.1']}]}]}
                    for number in (1, 2)]}
            else:
                # The real model invented this selection when given zero candidates.
                value = {'selected_relations': [1]}
            return json.dumps(value), {'name': 'fixture'}

    result = await generate_wiki(Provider(), 'Collection and validation', evidence, limits_for({"max_pages": 2}),
        execution_policy='local_only', allow_remote=False,
        record_attempt=lambda *args: lambda **updates: None)
    assert [page['title'] for page in result['pages']] == topics
    assert result['relations'] == []


def test_same_page_identity_merges_valid_references_and_sections(evidence):
    second = copy.deepcopy(evidence[0])
    second.update(id="original-2", excerpt="Validator checks each batch.")
    second["evidence"]["source_version_id"] = "fixed-2"
    evidence.append(second)
    plan = normalize_plan({"pages": [
        {"title": "Pipeline", "sections": ["Collection"], "references": [1], "source_term": "Collector"},
        {"title": " pipeline ", "sections": ["Validation", "Collection"], "references": [2, 1],
         "source_term": "Validator"}]}, evidence, limits_for({"max_pages": 2}))
    assert [(p["title"], p["references"], p["sections"], p["source_term"]) for p in plan] == [
        ("Pipeline", [1, 2], ["Collection", "Validation"], None)]
    with pytest.raises(ApplicationError) as invalid:
        normalize_plan({"pages": [{"title": "Pipeline", "references": [1]},
                                  {"title": "Pipeline", "references": [99]}]},
                       evidence, limits_for({"max_pages": 2}))
    assert invalid.value.code == "unsupported_generation"


@pytest.mark.parametrize("stage", ["plan", "write"])
def test_multisource_coverage_distinguishes_origins_with_equal_version_ids(evidence, stage):
    evidence[0]["evidence"].update(origin_node_id="node-a", resource_id="manual")
    second = copy.deepcopy(evidence[0])
    second["id"] = "original-2"
    second["evidence"]["origin_node_id"] = "node-b"
    evidence.append(second)
    planned = {"pages": [{"title": "Comparison", "references": [1] if stage == "plan" else [1, 2]}]}
    supported = {"text": "Collector forwards batches.", "references": [1],
                 "support": [{"reference": 1, "quote": "Collector forwards batches"}]}
    with pytest.raises(ApplicationError) as omitted:
        plan = normalize_plan(planned, evidence, limits_for({}))
        normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts",
            "sentences": [supported]}]}]},
            plan, evidence, {})
    assert omitted.value.code == "wiki_generation_invalid"


@pytest.mark.asyncio
async def test_wiki_citation_expansion_stops_at_original_sources(evidence):
    from ddp_core.application.wiki import generate_wiki

    class Provider:
        def __init__(self):
            self.stages = []

        async def generate(self, messages, **kwargs):
            stage = json.loads(messages[-1]["content"])["stage"]
            self.stages.append(stage)
            if stage == "plan":
                value = {"pages": [{"title": "Collector", "references": [1]}]}
            else:
                value = {"pages": [{"page": 1, "sections": [{"heading": "Facts",
                    "sentences": [{"text": "Collector forwards batches.",
                                   "support": ["q1.1"]}]}]}]}
            return json.dumps(value), {"name": "fixture"}

    provider = Provider()
    options = dict(execution_policy="local_only", allow_remote=False,
                   record_attempt=lambda *args: lambda **updates: None)
    result = await generate_wiki(provider, "Collector", evidence, limits_for({}), **options)
    claim = result["pages"][0]["generated_sections"][0]["sentences"][0]
    assert claim["evidence_ids"] == ["original-1"]
    generated = copy.deepcopy(evidence[0])
    generated.update(id=claim["id"], excerpt=claim["text"])
    generated["evidence"].update(source_type=claim["source_type"],
        derived_from=claim["evidence_ids"],
        excerpt_digest="sha256:" + hashlib.sha256(claim["text"].encode()).hexdigest())
    with pytest.raises(ApplicationError) as rejected:
        await generate_wiki(provider, "Expand Collector", [generated], limits_for({}), **options)
    assert rejected.value.code == "wiki_source_unavailable"
    # Reusing the generated page must not trigger even another planning call.
    assert provider.stages == ["plan", "write"]


@pytest.mark.parametrize("depth", [1, 4, 32])
@pytest.mark.asyncio
async def test_wiki_dependency_depth_rejects_ancestry_even_when_labeled_source(evidence, depth):
    from ddp_core.application.wiki import generate_wiki

    ancestor = {"evidence_id": "original-1", "derived_from": None}
    for number in range(depth):
        ancestor = {"evidence_id": f"generated-{number}", "derived_from": ancestor}
    evidence[0]["evidence"]["derived_from"] = ancestor

    class Provider:
        async def generate(self, messages, **kwargs):
            pytest.fail("derived evidence must be rejected before any model request")

    with pytest.raises(ApplicationError) as rejected:
        await generate_wiki(Provider(), "Nested dependencies", evidence, limits_for({}),
            execution_policy="local_only", allow_remote=False,
            record_attempt=lambda *args: lambda **updates: None)
    assert rejected.value.code == "wiki_source_unavailable"


@pytest.mark.parametrize("budget", ["max_evidence", "max_input_chars"])
@pytest.mark.asyncio
async def test_wiki_context_budget_rejects_overflow_before_model_request(evidence, budget):
    from ddp_core.application.wiki import generate_wiki

    limits = limits_for({budget: 1 if budget == "max_evidence" else 1000})
    if budget == "max_evidence":
        second = copy.deepcopy(evidence[0])
        second["id"] = "original-2"
        evidence.append(second)
    else:
        evidence[0]["excerpt"] = "Collector forwards batches. " * 60
        evidence[0]["evidence"]["excerpt_digest"] = (
            "sha256:" + hashlib.sha256(evidence[0]["excerpt"].encode()).hexdigest())

    class Provider:
        async def generate(self, messages, **kwargs):
            pytest.fail("over-budget evidence must not leave for model inference")

    with pytest.raises(ApplicationError) as rejected:
        await generate_wiki(Provider(), "Collector", evidence, limits,
            execution_policy="local_only", allow_remote=False,
            record_attempt=lambda *args: lambda **updates: None)
    assert rejected.value.code == "wiki_budget_exceeded"


def _kernel_row(identifier, excerpt, version="fixed"):
    return {"id": identifier, "excerpt": excerpt, "evidence": {
        "source_type": "source", "derived_from": None, "source_version_id": version,
        "locator": {"physical_page_index": 0, "bbox": [1, 2, 3, 4]},
        "excerpt_digest": "sha256:" + hashlib.sha256(excerpt.encode()).hexdigest()}}


def _kernel_page(text, references, support):
    plan = normalize_plan({"pages": [{"title": "Pipeline", "references": references}]},
                           _MISMATCH_EVIDENCE, limits_for({}))
    return normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
        {"text": text, "references": references, "support": support}]}]}]}, plan, _MISMATCH_EVIDENCE, {})


_MISMATCH_EVIDENCE = [
    _kernel_row("formula-block",
                "We use the normalized hidden state of [CLS]: eq = norm(Hq[0]). "
                "The relevance score is sdense = <ep, eq>."),
    _kernel_row("distillation-block",
                "Based on ensemble learning, predictions from different retrieval methods "
                "are integrated as a teacher signal via self-knowledge distillation."),
]


def test_support_quote_from_sibling_original_is_rejected_not_rebound():
    # R8 frozen case: the CPU M3 formula claims cited the distillation block while the
    # formulas sat in a sibling block of the same supplied context. The verbatim span
    # exists — but in the wrong original — so the claim must fail, not be relabelled.
    with pytest.raises(ApplicationError) as rejected:
        _kernel_page("Dense score uses sdense = <ep, eq>.", [2],
                     [{"reference": 2, "quote": "sdense = <ep, eq>"}])
    assert rejected.value.code == "unsupported_generation"


def test_support_quote_must_cover_every_cited_original():
    with pytest.raises(ApplicationError) as rejected:
        _kernel_page("Formula and distillation combined.", [1, 2],
                     [{"reference": 1, "quote": "sdense = <ep, eq>"}])
    assert rejected.value.code == "unsupported_generation"


def test_support_quote_keeps_case_for_math_symbols():
    rows = [_kernel_row("case-block", "The routing key uses symbol K for the shard map.")]
    plan = normalize_plan({"pages": [{"title": "Shards", "references": [1]}]}, rows, limits_for({}))
    with pytest.raises(ApplicationError) as rejected:
        normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
            {"text": "The shard key is k.", "references": [1],
             "support": [{"reference": 1, "quote": "symbol k for"}]}]}]}]}, plan, rows, {})
    assert rejected.value.code == "unsupported_generation"
    pages, _ = normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
        {"text": "The shard key is K.", "references": [1],
         "support": [{"reference": 1, "quote": "symbol K for"}]}]}]}]}, plan, rows, {})
    claim = pages[0]["generated_sections"][0]["sentences"][0]
    assert claim["support_check"] == {"mechanical": "verbatim_present", "quotes": 1}


@pytest.mark.parametrize("excerpt", ["E=mc2", "   E=mc2    "])
def test_short_atom_is_quoted_as_the_whole_atom(excerpt):
    rows = [_kernel_row("short-formula", excerpt)]
    plan = normalize_plan({"pages": [{"title": "Formula", "references": [1]}]}, rows, limits_for({}))
    pages, _ = normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
        {"text": "Energy follows E=mc2.", "references": [1],
         "support": [{"reference": 1, "quote": "E=mc2"}]}]}]}]}, plan, rows, {})
    assert pages[0]["generated_sections"][0]["sentences"][0]["evidence_ids"] == ["short-formula"]
    with pytest.raises(ApplicationError) as rejected:
        normalize_pages({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
            {"text": "Energy follows E=mc.", "references": [1],
             "support": [{"reference": 1, "quote": "E=mc"}]}]}]}]}, plan, rows, {})
    assert rejected.value.code == "unsupported_generation"


@pytest.mark.asyncio
async def test_verbatim_support_still_marks_semantic_review_needs_review():
    from ddp_core.application.wiki import generate_wiki

    class Provider:
        async def generate(self, messages, **kwargs):
            stage = json.loads(messages[-1]["content"])["stage"]
            if stage == "plan":
                return json.dumps({"pages": [{"title": "Pipeline", "references": [1, 2]}]}), {"name": "fixture"}
            return json.dumps({"pages": [{"page": 1, "sections": [{"heading": "Facts", "sentences": [
                {"text": "Dense score uses sdense = <ep, eq>.",
                 "support": ["q1.1"]},
                {"text": "Distillation integrates a teacher signal.",
                 "support": ["q2.1"]}]}]}]}), {"name": "fixture"}

    result = await generate_wiki(
        Provider(), "Pipeline", copy.deepcopy(_MISMATCH_EVIDENCE), limits_for({}),
        execution_policy="local_only", allow_remote=False,
        record_attempt=lambda *args: lambda **updates: None)
    assert result["semantic_review"] == "needs_review"
    claims = result["pages"][0]["generated_sections"][0]["sentences"]
    assert [claim["support_check"]["mechanical"] for claim in claims] == ["verbatim_present"] * 2
    assert [claim["evidence_ids"] for claim in claims] == [
        [_MISMATCH_EVIDENCE[0]["id"]], [_MISMATCH_EVIDENCE[1]["id"]]]


@pytest.mark.asyncio
async def test_selected_support_preserves_pdf_noncharacters_without_copying_by_model():
    from ddp_core.application.wiki import generate_wiki

    excerpt = "Inference time on the STS\ufffe B development set uses a batch size of 1."
    rows = [_kernel_row("pdf-original", excerpt)]

    class Provider:
        async def generate(self, messages, **kwargs):
            stage = json.loads(messages[-1]["content"])["stage"]
            if stage == "plan":
                value = {"pages": [{"title": "Inference measurement", "references": [1]}]}
            else:
                value = {"pages": [{"page": 1, "sections": [{"heading": "Setup",
                    "sentences": [{"text": "The measurement uses batch size 1.",
                                   "support": ["q1.1"]}]}]}]}
            return json.dumps(value), {"name": "fixture"}

    result = await generate_wiki(
        Provider(), "Inference measurement", rows, limits_for({}),
        execution_policy="local_only", allow_remote=False,
        record_attempt=lambda *args: lambda **updates: None)
    claim = result["pages"][0]["generated_sections"][0]["sentences"][0]
    assert claim["support"] == [{"reference": 1, "evidence_id": "pdf-original", "quote": excerpt}]
    assert claim["evidence_ids"] == ["pdf-original"]


@pytest.mark.asyncio
async def test_selected_support_rejects_a_second_citation_channel():
    from ddp_core.application.wiki import generate_wiki

    class Provider:
        async def generate(self, messages, **kwargs):
            stage = json.loads(messages[-1]["content"])["stage"]
            if stage == "plan":
                value = {"pages": [{"title": "Pipeline", "references": [1, 2]}]}
            else:
                value = {"pages": [{"page": 1, "sections": [{"heading": "Formula",
                    "sentences": [{"text": "Dense score uses sdense = <ep, eq>.",
                                   "references": [2], "support": ["q1.1"]}]}]}]}
            return json.dumps(value), {"name": "fixture"}

    with pytest.raises(ApplicationError) as rejected:
        await generate_wiki(
            Provider(), "Pipeline", copy.deepcopy(_MISMATCH_EVIDENCE), limits_for({}),
            execution_policy="local_only", allow_remote=False,
            record_attempt=lambda *args: lambda **updates: None)
    assert rejected.value.code == "wiki_generation_invalid"


@pytest.mark.parametrize("pages,message_part", [
    ([{"title": "", "references": [1]}], "planned page 1 title"),
    ([{"title": "Pipeline", "sections": ["Fine", "  "], "references": [1]}],
     "planned page 1 section 2"),
    ([{"title": "Pipeline", "references": [1], "source_term": "x" * 256}],
     "planned page 1 source_term"),
    ([{"title": "Trailing colon:", "references": [1]}], "planned page 1 source anchor from title"),
])
def test_normalize_plan_rejection_names_page_and_field(evidence, pages, message_part):
    """Planner text faults keep code `wiki_generation_invalid` but name the page
    and field, so a malformed outline is locatable without the raw response."""
    with pytest.raises(ApplicationError) as rejected:
        normalize_plan({"pages": pages}, evidence, limits_for({}))
    assert rejected.value.code == "wiki_generation_invalid"
    assert message_part in str(rejected.value)
