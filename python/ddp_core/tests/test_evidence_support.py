"""Support units count content independence without erasing useful passages."""
from ddp_core.application import coverage, plans


def evidence(name, *, source=None, excerpt=None):
    item = {"origin_node_id": "node-" + name, "resource_id": "resource-" + name,
            "source_version_id": "version-" + name, "evidence_id": name}
    if source is not None:
        item["source_digest"] = plans.content_digest(source.encode())
    if excerpt is not None:
        item["excerpt_digest"] = plans.content_digest(excerpt.encode())
    return item


def test_same_source_retains_different_excerpts_without_claiming_two_sources():
    first = evidence("a", source="same PDF", excerpt="first passage")
    second = evidence("b", source="same PDF", excerpt="second passage")
    duplicate = evidence("c", source="same PDF", excerpt="first passage")
    groups, representatives = coverage.support_groups([first, second, duplicate])
    assert len(groups) == 1
    assert [ref["evidence_id"] for ref in groups[0]["copies"]] == ["a", "b", "c"]
    assert [ref["evidence_id"] for ref in groups[0]["representatives"]] == ["a", "b"]
    assert representatives == [first, second]


def test_equal_excerpts_join_source_groups_transitively():
    first = evidence("a", source="PDF one", excerpt="shared sentence")
    second = evidence("b", source="PDF two", excerpt="shared sentence")
    third = evidence("c", source="PDF two", excerpt="another passage")
    independent = evidence("d", source="PDF three", excerpt="different sentence")
    groups, representatives = coverage.support_groups([first, second, third, independent])
    assert [[ref["evidence_id"] for ref in group["copies"]] for group in groups] == [
        ["a", "b", "c"], ["d"]]
    assert representatives == [first, third, independent]
    reversed_groups, _ = coverage.support_groups([independent, third, second, first])
    assert {group["support_id"] for group in groups} == {
        group["support_id"] for group in reversed_groups}


def test_missing_digests_do_not_manufacture_equality():
    first, second = evidence("a"), evidence("b")
    groups, representatives = coverage.support_groups([first, second])
    assert [[ref["evidence_id"] for ref in group["copies"]] for group in groups] == [["a"], ["b"]]
    assert groups[0]["support_id"] != groups[1]["support_id"]
    assert representatives == [first, second]
    assert coverage.support_groups([]) == ([], [])
