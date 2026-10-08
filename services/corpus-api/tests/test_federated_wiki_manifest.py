"""Federated Wiki manifest records the full sent context, not just cited refs.

Regression: `commit_federated_revision` used to build
`wiki_dependencies` only from claim/edge `evidence_ids`, so an uncited
private/withdrawn input sent to the model left no dep row and a later
revoke/version-change neither staled the page nor blocked publish. The local
Wiki path (`wiki.build`) records pages x frozen context actually sent; the
federated commit must do the same.

Note on shape: the shared kernel (`require_source_coverage`) still rejects a
commit whose claims omit a whole selected *source* (origin/resource/version),
so the laundering gap that survives to the manifest is an uncited *block* of
an otherwise-cited source — e.g. a private excerpt sent alongside a cited
public one. The tests below use two blocks of one source, cite only the first,
and pin that both land in the manifest.

Uses the real `commit_federated_revision` + `wiki.dependency_state` (not the
HTTP wiki routes): federated evidence is foreign envelopes, not local sources.
"""
import hashlib

from ddp_corpus import federated_wiki as federated_wiki_plane
from ddp_corpus import wiki as wiki_plane
from ddp_corpus.deps import Actor
from ddp_corpus.models import Wiki
from tests.conftest import ACTOR, ORG

FOREIGN = "node-" + "b" * 48


def _envelope(*, evidence_id, text, seq, publication="published", grant=None):
    digest = hashlib.sha256(text.encode()).hexdigest()
    envelope = {
        "schema": "ddp-evidence/1#FederatedEvidence",
        "evidence_id": evidence_id,
        "origin_node_id": FOREIGN,
        "authority_node_id": FOREIGN,
        "resource_id": "res-shared",
        "source_version_id": "v-shared",
        "source_digest": "sha256:" + hashlib.sha256(b"shared-source").hexdigest(),
        "parse_revision": "parse-shared",
        "excerpt_digest": "sha256:" + digest,
        "locator": {"kind": "page_block", "physical_page_index": 0, "seq": seq,
                    "bbox": [1, 2, 3, 4], "page_size": {"width": 100, "height": 200}},
        "source_type": "source",
        "derived_from": None,
        "source_publication": publication,
        "policy_revision": "policy:1",
    }
    if grant is not None:
        envelope["derivative_grant"] = grant
    return envelope


def _item(envelope, text):
    ref = federated_wiki_plane.source_ref(
        {key: envelope.get(key) for key in (
            "origin_node_id", "resource_id", "source_version_id",
            "parse_revision", "evidence_id")})
    return {"evidence_id": ref, "source_envelope": dict(envelope), "excerpt": text}


def _spec(title="Federated notes"):
    return {
        "schema": "ddp-task-probe/1#TaskSpec", "protocol": "ddp-task/1",
        "operation": "wiki.pages", "workspace_ref": "workspace-a", "query": "notes",
        "resource_scope": {"kind": "site_public"},
        "search_policy": {"mode": "fast", "ordering": "local_first"},
        "execution_policy": {"mode": "trusted_federation", "coordinator_ref": FOREIGN},
        "consent_refs": {"exploration": "explore-1", "execution": "execute-1"},
        "budget_ref": "budget-1",
        "requirements": {"wiki": {"title": title, "max_pages": 2}},
    }


def _result(*, cited_ref):
    return {
        "pages": [{
            "page_key": "overview", "title": "Overview",
            "generated_sections": [{
                "heading": "Facts",
                "sentences": [{"text": "Public fact.", "evidence_ids": [cited_ref]}],
            }],
        }],
        "relations": [],
        "provider": {"model": "test", "kind": "federated_wiki_generation"},
        "limits": {"max_pages": 2},
    }


def _actor():
    return Actor(id=ACTOR, kind="user", organization_id=ORG, role="contributor")


def _evidence():
    public_text, private_text = "Public fact.", "Private fact."
    public_env = _envelope(evidence_id="ev-public", text=public_text, seq=0)
    private_env = _envelope(evidence_id="ev-private", text=private_text, seq=1,
                            publication="private", grant="grant-1")
    return ([_item(public_env, public_text), _item(private_env, private_text)],
            public_text, private_text)


async def _commit(session, monkeypatch, *, root_task_id, title, cited_ref, evidence):
    """Commit with a pinned local node far from FOREIGN so both items stay foreign."""
    from ddp_corpus import node_identity as node_identity_plane

    monkeypatch.setattr(node_identity_plane, "local_node_id", lambda: "node-local-test")
    return await federated_wiki_plane.commit_federated_revision(
        session, _actor(), root_task_id=root_task_id,
        task_spec=_spec(title=title), result=_result(cited_ref=cited_ref), evidence=evidence)


async def test_federated_manifest_includes_uncited_private_input(session, monkeypatch):
    """[public-cited + private-uncited-with-grant] -> dep rows include BOTH."""
    evidence, _, _ = _evidence()
    refs = [item["evidence_id"] for item in evidence]
    out = await _commit(session, monkeypatch, root_task_id="federated-manifest-uncited",
                        title="Federated notes", cited_ref=refs[0], evidence=evidence)
    manifest = out["revision"]["dependency_manifest"]
    assert {row["source_evidence_id"] for row in manifest} == {"ev-public", "ev-private"}
    assert {row["evidence_id"] for row in manifest} == set(refs)
    assert all(row["page_key"] == "overview" for row in manifest)

async def test_revoke_of_uncited_private_source_stales_page(session, monkeypatch):
    """With the uncited private row in the manifest, `dependency_state`
    reports `permission_unresolved` for its page (publish fail-closed)."""
    evidence, _, _ = _evidence()
    refs = [item["evidence_id"] for item in evidence]
    out = await _commit(session, monkeypatch, root_task_id="federated-manifest-revoke",
                        title="Federated revoke notes", cited_ref=refs[0], evidence=evidence)
    wiki = await session.get(Wiki, out["wiki"]["id"])
    _revision, _rows, deps = await wiki_plane.revision_data(session, out["revision"]["id"])
    assert {dep.source_evidence_id for dep in deps} == {"ev-public", "ev-private"}

    async def revoked_recheck(_session, _actor, dep, *, publish=False):
        if dep["source_evidence_id"] == "ev-private":
            return ["permission_unresolved"]
        return []

    stale = await wiki_plane.dependency_state(
        session, _actor(), wiki, deps, federated_recheck=revoked_recheck)
    assert stale == {"overview": ["permission_unresolved"]}
