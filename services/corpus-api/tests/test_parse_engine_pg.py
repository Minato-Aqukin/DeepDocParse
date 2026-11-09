"""Registered engine identities must survive real PostgreSQL ingestion unchanged."""
import os

import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ddp_corpus import db
from ddp_corpus.config import settings
from ddp_corpus.models import new_id
from tests.conftest import actor_headers, submit_document
from tests.test_documents import _mock_service


@pytest.fixture
async def engine(monkeypatch):
    dsn = os.getenv("CORPUS_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("CORPUS_TEST_DATABASE_URL must point to a migrated scratch PostgreSQL database")
    database = create_async_engine(dsn)
    assert database.dialect.name == "postgresql", "SQLite does not enforce VARCHAR lengths"
    monkeypatch.setattr(db, "_engine", database)
    monkeypatch.setattr(db, "_sessionmaker", async_sessionmaker(database, expire_on_commit=False))
    try:
        yield database
    finally:
        await database.dispose()


@respx.mock
async def test_long_registered_engine_is_accepted_and_retained_in_document_history(
    actor_client, app_state, monkeypatch,
):
    _mock_service()
    actor, organization = new_id(), new_id()
    engine_name = "provider/native-table-parser@" + "0123456789abcdef" * 8
    monkeypatch.setattr(settings, "default_parse_engine", engine_name)
    response = await submit_document(
        actor_client, app_state.storage, b"%PDF-1.4\n" + new_id().encode(),
        actor_id=actor, organization_id=organization,
    )
    assert response.status_code == 200, response.text
    document_id = response.json()["result_id"]
    monkeypatch.setattr(settings, "default_parse_engine", "another-runtime")
    detail = await actor_client.get(
        f"/api/documents/{document_id}/jobs",
        headers=actor_headers(actor, organization_id=organization),
    )
    assert detail.status_code == 200, detail.text
    assert [job["engine"] for job in detail.json()] == [engine_name]
