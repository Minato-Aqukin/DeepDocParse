"""Explicit coordination retains substantive facets without invented facts."""
import pytest

from ddp_core.search import _query_facets, search_query, search_query_with_truncation


@pytest.mark.parametrize("separator", [", ", ", and ", " and "])
def test_thin_comparison_keeps_requested_gpio_facets(separator):
    question = ("How many multi-function GPIOs does Raspberry Pi Pico expose"
                + separator + "how many programmable GPIOs does ESP32 have"
                + ", and what is the difference?")
    assert _query_facets(question) == ["multi-function GPIOs", "programmable GPIOs"]


def test_one_substantive_facet_does_not_invent_another_fact():
    question = "How many programmable GPIOs does ESP32 have, and what is the difference?"
    assert _query_facets(question) == [question]


def test_document_lists_are_not_question_coordination():
    question = "Compare Raspberry Pi Pico, ESP32 and STM32 GPIO counts"
    assert _query_facets(question) == [question]


@pytest.mark.parametrize("question", [
    "Which board has 26 multi-function GPIO pins, which supports wireless protocols?",
    "What limits the ADC input voltage, what makes the pins input-only?",
    "Which ESP32 pins are input-only, which lack internal pull-up resistors?",
])
def test_relative_or_appositive_clauses_after_a_comma_are_not_new_facets(question):
    assert _query_facets(question) == [question]


def test_comma_count_question_needs_an_interrogative_question():
    statement = "Pico exposes 26 multi-function GPIOs, how many programmable GPIOs does ESP32 have"
    assert _query_facets(statement) == [statement]


@pytest.mark.parametrize("extra", [False, True])
async def test_truncation_counts_distinct_hits_without_changing_facet_priority(extra):
    question = "What is the supply voltage and what is the wireless protocol?"
    routes = {
        question: ["overview", "power"] if extra else ["power", "radio"],
        "is the supply voltage": ["power"],
        "is the wireless protocol": ["radio"],
    }

    class RankedIndex:
        async def search(self, _session, *, query, limit, **_kwargs):
            return [{"chunk_id": cid, "score": 0.03, "similarity": 0.8}
                    for cid in routes[query][:limit]]

    async def embed(texts):
        return [[1.0] for _ in texts]

    kwargs = dict(embed=embed, query=question, document_id=None,
                  limit=2, candidates=4, min_similarity=0.4)
    plain, _ = await search_query(None, RankedIndex(), **kwargs)
    hits, degraded, truncated = await search_query_with_truncation(
        None, RankedIndex(), **kwargs)
    assert [hit["chunk_id"] for hit in hits] == ["power", "radio"]
    assert hits == plain
    assert truncated is extra
    assert degraded is None
