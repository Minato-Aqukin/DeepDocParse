"""Kernel validation for requirements.query_plan.subqueries (mirrors ddp-task-probe schema)."""
import copy

import pytest

from ddp_core.application.plans import requirements_query_plan, validate_spec
from ddp_core.application.ports import ApplicationError
from plan_samples import plan_scope


def _spec():
    return plan_scope()["task_spec"]


def test_valid_query_plan_passes():
    spec = _spec()
    spec["requirements"] = {"query_plan": {"subqueries": ["alpha", "beta"]}}
    validate_spec(spec)


def test_no_query_plan_passes():
    validate_spec(_spec())


@pytest.mark.parametrize("bad", [
    {"subqueries": []},
    {"subqueries": ["a"] * 9},
    {"subqueries": [""]},
    {"subqueries": ["x" * 2001]},
    {"subqueries": ["dup", "dup"]},
    {"subqueries": [42]},
    {"subqueries": "alpha"},
    {"other": []},
    [],
    # Zero content tokens: single CJK char, punctuation-only, function-words-only.
    {"subqueries": ["钱"]},
    {"subqueries": ["..."]},
    {"subqueries": ["the"]},
    {"subqueries": ["alpha", "钱"]},
])
def test_bad_query_plan_rejected(bad):
    with pytest.raises(ApplicationError):
        requirements_query_plan(bad)

def test_boundary_lengths_accepted():
    requirements_query_plan({"subqueries": ["x", "y" * 2000]})
    with pytest.raises(ApplicationError):
        requirements_query_plan({"subqueries": ["y" * 2001]})
    with pytest.raises(ApplicationError):
        requirements_query_plan({"subqueries": ["a"] * 9})


def test_validate_spec_rejects_bad_query_plan():
    spec = _spec()
    spec["requirements"] = {"query_plan": {"subqueries": ["dup", "dup"]}}
    with pytest.raises(ApplicationError):
        validate_spec(spec)
    spec2 = _spec()
    spec2["requirements"] = {"query_plan": {"subqueries": ["ok"]}, "bogus": 1}
    with pytest.raises(ApplicationError):
        validate_spec(spec2)
    # query_plan rides along in the digest like any other requirements field
    base = copy.deepcopy(_spec())
    mod = copy.deepcopy(base)
    mod["requirements"] = {"query_plan": {"subqueries": ["alpha"]}}
    from ddp_core.application.plans import task_spec_digest
    assert task_spec_digest(mod) != task_spec_digest(base)
