"""Legacy generated artifacts are private to their recorded author and source scope.

Unattributed historical projections are quarantined. The first content uploader is
not proof of who authored a graph/Wiki, so source visibility alone never grants access.

SQL scoping below is a pre-filter only: selects are narrowed by provider org in SQL
(legacy rows with a NULL org still load), then every surviving row goes through
provider_allowed as the second gate. The residual risk without that gate would be a
provider JSON whose recorded org matches the caller while its bindings point at
another org's resources; require_resource in the loop closes it.

Citation rows carry no org of their own, so the dependency join is scoped through
the caller's visible documents, the same SQL predicate the knowledge reads use.
"""
from sqlalchemy import or_, select
from ddp_corpus.errors import APIError
from ddp_corpus.models import (
    Assertion, Citation, Conversation, Document, Evidence, ExtractionItem, ExtractionRun,
    GraphEdge, KnowledgeEntity, Message, ResourceVersion, WikiEntry, WikiSection, WikiSentence,
)
from ddp_corpus.policy import require_history_document, require_resource, visible_document_condition


async def provider_allowed(session, actor, provider):
    if (not actor.principal_id or provider.get("generated_by") != actor.principal_id
            or provider.get("organization_id") != actor.organization_id
            or not provider.get("source_bindings")):
        return False
    for binding in provider["source_bindings"]:
        try:
            resource = await require_resource(session, actor, binding["resource_id"])
        except APIError:
            return False
        if resource.publication == "withdrawn":
            # Historical versions stay readable after a re-parse or a new version;
            # withdrawal is the explicit "take it down" state, so reads fail closed.
            return False
        version = await session.get(ResourceVersion, binding["source_version_id"], populate_existing=True)
        if (version is None or version.deleted_at is not None
                or version.resource_id != binding["resource_id"]
                or version.document_id != binding["document_id"]
                or version.parse_job_id != binding["parse_revision"]):
            # Missing, deleted, rebound, or re-parsed bindings no longer describe the
            # frozen source the artifact was generated from.
            return False
    return True


async def owned_projection(session, actor, kind, source_id):
    if not actor.principal_id:
        return False
    if kind in ("assertion", "message"):
        assertion = await session.get(Assertion, source_id) if kind == "assertion" else None
        if kind == "assertion" and assertion is None:
            return False
        message = await session.get(Message, assertion.message_id if assertion else source_id)
        conversation = await session.get(Conversation, message.conversation_id) if message else None
        if (conversation is None or conversation.actor_id != actor.id
                or conversation.organization_id != actor.organization_id):
            return False
        try:
            await require_history_document(session, actor, conversation.document_id,
                                           resource_id=conversation.resource_id)
        except APIError:
            return False
        return True
    if kind == "extract_field":
        item = await session.get(ExtractionItem, source_id.partition(":")[0])
        run = await session.get(ExtractionRun, item.run_id) if item else None
        if (run is None or run.actor_id != actor.id or run.organization_id != actor.organization_id):
            return False
        resources = (run.resource_context or {}).get("resources") or {}
        documents = set((await session.execute(select(ExtractionItem.document_id).where(
            ExtractionItem.run_id == run.id))).scalars()) | set(resources)
        for doc_id in documents:
            try:
                await require_history_document(session, actor, doc_id, resource_id=resources.get(doc_id))
            except APIError:
                return False
        return True
    return False


async def accessible_knowledge(session, actor):
    def _org_scope(model):
        org = model.provider["organization_id"].as_string()
        return or_(org == actor.organization_id, org.is_(None))

    visible_docs = set((await session.execute(select(Document.id).where(
        visible_document_condition(actor)))).scalars())
    cites = (await session.execute(select(Citation.source_kind, Citation.source_id,
        Evidence.document_id).join(Evidence, Citation.evidence_id == Evidence.id).join(
            Document, Document.id == Evidence.document_id).where(
                visible_document_condition(actor)))).all()
    dependencies = {}
    for kind, source_id, doc_id in cites:
        dependencies.setdefault((kind, source_id), set()).add(doc_id)
    entities = (await session.execute(
        select(KnowledgeEntity).where(_org_scope(KnowledgeEntity)))).scalars().all()
    edges = (await session.execute(
        select(GraphEdge).where(_org_scope(GraphEdge)))).scalars().all()
    entries = (await session.execute(
        select(WikiEntry).where(_org_scope(WikiEntry)))).scalars().all()
    sections = (await session.execute(select(WikiSection).join(
        WikiEntry, WikiSection.entry_id == WikiEntry.id).where(
            _org_scope(WikiEntry)))).scalars().all()
    sentences = (await session.execute(select(WikiSentence).where(
        _org_scope(WikiSentence)))).scalars().all()
    cache = {}

    async def permitted(row):
        provider = row.provider or {}
        cache_key = str(provider)
        if cache_key not in cache:
            cache[cache_key] = await provider_allowed(session, actor, provider)
        return cache[cache_key]

    good_entities = {row.id for row in entities if await permitted(row)}
    good_edges = {row.id for row in edges if row.subject_id in good_entities
                  and row.object_id in good_entities and await permitted(row)}
    section_entry = {row.id: row.entry_id for row in sections}
    permitted_sentences = {row.id for row in sentences if await permitted(row)}
    good_entries = {row.id for row in entries if row.entity_id in good_entities
                    and await permitted(row) and all(sentence.id in permitted_sentences
                        for sentence in sentences if section_entry.get(sentence.section_id) == row.id)}
    section_entry = {row.id: row.entry_id for row in sections}
    good_sentences = {row.id for row in sentences if section_entry.get(row.section_id) in good_entries
                      and await permitted(row)}
    return {"entities": good_entities, "edges": good_edges, "entries": good_entries,
            "sentences": good_sentences, "documents": visible_docs,
            "source_dependencies": dependencies}
