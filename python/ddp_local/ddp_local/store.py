"""Local SQLite owns only workspace resources/tasks/evidence, never site accounts."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import json_bytes
from ddp_core.tokenize import backend as tokenizer_backend
from ddp_core.tokenize import code_tokenized, tokenized, tokens
from ddp_local.workspace_schemas import WORKSPACE_SCHEMA_VERSIONS

DDL = """
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE resources(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE versions(id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id),
 filename TEXT NOT NULL, source_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 blob_key TEXT NOT NULL, parse_revision TEXT, state TEXT NOT NULL,
 layout_key TEXT, bundle_key TEXT, source_json TEXT, provider TEXT NOT NULL DEFAULT '{}',
 degraded TEXT NOT NULL DEFAULT '[]', error TEXT, page_count INTEGER, layout_error TEXT,
 created_at REAL NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, version_id TEXT REFERENCES versions(id),
 operation_key TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL, status TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 lease_until REAL, result TEXT, error TEXT, created_at REAL NOT NULL,
 updated_at REAL NOT NULL);
CREATE TABLE evidence(id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
 seq INTEGER NOT NULL, excerpt TEXT NOT NULL, envelope TEXT NOT NULL, chunk TEXT NOT NULL);
CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, version_id UNINDEXED, content,
 tokenize='unicode61');
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, task_id TEXT,
 payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE outputs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
 created_at REAL NOT NULL);
PRAGMA user_version=1;
"""

MAX_UPLOAD_PART = 8 * 1024 * 1024
MAX_UPLOAD_PARTS = 8  # 8 x 8 MiB covers the 32 MiB local budget with bounded overhead.

CONTENT_TABLES = """
CREATE TABLE conversations(id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
 title TEXT NOT NULL, resource_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL
 REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL,
 verified INTEGER NOT NULL DEFAULT 0, degraded TEXT, model_meta TEXT NOT NULL DEFAULT '{}',
 confidence TEXT, query_json TEXT, created_at REAL NOT NULL);
CREATE TABLE assertions(id TEXT PRIMARY KEY, message_id TEXT NOT NULL
 REFERENCES messages(id) ON DELETE CASCADE, position INTEGER NOT NULL,
 text TEXT NOT NULL, evidence_ids TEXT NOT NULL DEFAULT '[]',
 verification_state TEXT NOT NULL DEFAULT 'unverified', verification_mode TEXT,
 unsupported INTEGER NOT NULL DEFAULT 1);
CREATE TABLE citations(id TEXT PRIMARY KEY, assertion_id TEXT NOT NULL
 REFERENCES assertions(id) ON DELETE CASCADE, evidence_id TEXT NOT NULL
 REFERENCES evidence(id) ON DELETE CASCADE, document_id TEXT NOT NULL,
 rank INTEGER NOT NULL DEFAULT 0, score REAL, similarity REAL);
CREATE TABLE upload_sessions(id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL,
 declared_size INTEGER NOT NULL, declared_sha256 TEXT, target_resource_id TEXT,
 idempotency_key TEXT, request_digest TEXT NOT NULL, status TEXT NOT NULL,
 parts_json TEXT NOT NULL DEFAULT '{}', resource_id TEXT, version_id TEXT, error TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE wiki_claim_bindings(revision_id TEXT NOT NULL REFERENCES wiki_revisions(id),
 wiki_id TEXT NOT NULL REFERENCES wikis(id), page_key TEXT NOT NULL, claim_id TEXT NOT NULL,
 evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE, excerpt_digest TEXT NOT NULL,
 PRIMARY KEY(revision_id, claim_id, evidence_id));
CREATE INDEX conversation_document_order ON conversations(document_id, updated_at DESC, id DESC);
CREATE INDEX message_conversation_order ON messages(conversation_id, created_at, id);
CREATE INDEX assertion_message_order ON assertions(message_id, position, id);
CREATE INDEX citation_assertion_order ON citations(assertion_id, rank, id);
CREATE INDEX citation_evidence_lookup ON citations(evidence_id);
CREATE INDEX upload_session_key ON upload_sessions(idempotency_key);
CREATE INDEX upload_session_created ON upload_sessions(created_at);
CREATE INDEX wiki_binding_evidence ON wiki_claim_bindings(evidence_id);
PRAGMA user_version=4;
"""


def new_id():
    return uuid.uuid4().hex


def record(row):
    value = dict(row)
    for name in ("provider", "degraded", "source_json", "result"):
        if value.get(name) is not None:
            value[name] = json.loads(value[name])
    return value


class _ChainedStreams:
    """File-like chain over ordered part streams with incremental hashing.

    ``put_stream`` pulls 64 KiB chunks, so only one chunk is ever in RAM.
    The SHA-256 digest and the first five bytes (for the %PDF- peek) are
    accumulated as bytes flow through; nothing is re-read afterwards.
    """

    def __init__(self, streams):
        self._streams = list(streams)
        self._index = 0
        self._digest = hashlib.sha256()
        self._head = b""

    def read(self, size=-1):
        if size is None or size < 0:
            size = 65536
        while self._index < len(self._streams):
            data = self._streams[self._index].read(size)
            if data:
                self._digest.update(data)
                if len(self._head) < 5:
                    self._head += data[: 5 - len(self._head)]
                return data
            self._index += 1
        return b""

    def hexdigest(self):
        return self._digest.hexdigest()

    def head(self):
        return self._head


# Projected document status (parse_status values) back to stored version
# states. Mirrors ``_version_state_to_parse`` in content_http: every stored
# state not listed under pending/running/succeeded projects to failed
# (withdrawn included), so the failed bucket carries the same rows the old
# Python-side filter kept. ``unparsed`` is a stored state (imported sources
# without a parse revision), hence its own pending bucket entry.
_PARSE_STATUS_STATES = {
    "pending": ("queued", "unparsed"),
    "running": ("parsing",),
    "succeeded": ("ready",),
    "failed": ("failed", "withdrawn"),
}


class LocalStore:
    def __init__(self, directory: Path):
        self.lock = RLock()
        database = directory / "workspace.sqlite3"
        for suffix in ("", "-journal", "-wal", "-shm"):
            if Path(str(database) + suffix).is_symlink():
                raise ApplicationError("unsafe_path", "workspace database and sidecars cannot be symlinks")
        # Upgrades take the exclusive side of this lease. Checking task rows alone
        # cannot fence an idle runtime starting work after the updater's preflight.
        with ExitStack() as failed:
            self._lease = failed.enter_context(os.fdopen(
                os.open(directory / ".runtime.lock",
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                "r+b", buffering=0))
            info = os.fstat(self._lease.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ApplicationError("unsafe_path", "workspace runtime lease must be private")
            try:
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApplicationError("workspace_updating", "workspace is being updated") from exc
            self.db = sqlite3.connect(
                database, timeout=10, isolation_level=None, check_same_thread=False
            )
            failed.callback(self.db.close)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.tx():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    # Fresh DDL only carries the v1 baseline; the additive
                    # migrations below stamp the current version.
                    for statement in DDL.split(";"):
                        if statement.strip():
                            self.db.execute(statement)
                elif version not in WORKSPACE_SCHEMA_VERSIONS["workspace.sqlite3"]:
                    raise ApplicationError(
                        "workspace_schema_unsupported", "unsupported local database version"
                    )
            from ddp_local.wiki_store import migrate
            migrate(self)
            self._migrate_content_tables()
            database.chmod(0o600)
            with self.tx():
                for key, value in (
                    ("workspace_id", new_id()),
                    ("environment_id", "local-" + new_id()),
                    ("tokenizer", tokenizer_backend()),
                ):
                    self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
            self._migrate_keyword_index()
            self.workspace_id = self.meta("workspace_id")
            self.environment_id = self.meta("environment_id")
            failed.pop_all()

    def _migrate_content_tables(self):
        """Create the center-content tables on workspaces made before this slice.

        Runs after the Wiki migration, so wiki_revisions exists for the
        wiki_claim_bindings foreign key. A version-2 workspace gets the
        additive migration here. The Wiki CAS triggers reference
        wiki_revisions only, so creating these tables never touches them.
        Version 4 adds cached layout facts (page_count, layout_error) to
        versions; fresh databases already carry them via DDL.
        """
        with self.tx():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2):
                # A version-1 database never ran the content migration: run the
                # same center-content statements the version-2 path runs (minus
                # the version stamp), then let the shared stamp below land on
                # the current version. Splitting per complete statement keeps
                # trigger bodies intact, as the Wiki migration does.
                statement = ""
                for line in CONTENT_TABLES.splitlines():
                    if line.strip().startswith("PRAGMA user_version"):
                        continue
                    statement += line + "\n"
                    if sqlite3.complete_statement(statement):
                        self.db.execute(statement)
                        statement = ""
            current = self.db.execute("PRAGMA user_version").fetchone()[0]
            if current in (0, 1, 2, 3):
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(versions)")}
                if "page_count" not in columns:
                    self.db.execute("ALTER TABLE versions ADD COLUMN page_count INTEGER")
                if "layout_error" not in columns:
                    self.db.execute("ALTER TABLE versions ADD COLUMN layout_error TEXT")
                upgraded = current in (1, 2, 3)
                self.db.execute("PRAGMA user_version=4")
                if upgraded:
                    stale = self.db.execute(
                        "SELECT id,layout_key FROM versions "
                        "WHERE layout_key IS NOT NULL AND page_count IS NULL "
                        "AND layout_error IS NULL"
                    ).fetchall()
                    for row in stale:
                        count, error, _meta = self._layout_facts(row["layout_key"])
                        self.db.execute(
                            "UPDATE versions SET page_count=?,layout_error=? WHERE id=?",
                            (count, error, row["id"]),
                        )

    def close(self):
        try:
            self.db.close()
        finally:
            self._lease.close()

    def _migrate_keyword_index(self):
        current = tokenizer_backend()
        if self.meta("tokenizer") == current:
            return
        try:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ApplicationError(
                "index_incompatible", "close other runtimes before migrating this workspace's tokenizer"
            ) from exc
        try:
            with self.tx():
                previous = self.meta("tokenizer")
                if previous == current:
                    return
                self.db.execute("DELETE FROM evidence_fts")
                for row in self.db.execute("SELECT id,version_id,chunk FROM evidence"):
                    chunk = json.loads(row["chunk"])
                    search_text = chunk.get("search_text", chunk["text"])
                    content = code_tokenized(search_text) if chunk.get("block_type") == "code" else tokenized(search_text)
                    self.db.execute("INSERT INTO evidence_fts VALUES(?,?,?)",
                                    (row["id"], row["version_id"], content))
                self.db.execute("UPDATE metadata SET value=? WHERE key='tokenizer'", (current,))
                self.event("index.rebuilt", None, {"previous_tokenizer": previous, "tokenizer": current})
        finally:
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_SH)

    def meta(self, key):
        with self.lock:
            return self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    @contextmanager
    def tx(self):
        with self.lock:
            savepoint = "nested_" + new_id() if self.db.in_transaction else None
            self.db.execute("SAVEPOINT " + savepoint if savepoint else "BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("RELEASE " + savepoint if savepoint else "COMMIT")
            except BaseException:
                if savepoint:
                    self.db.execute("ROLLBACK TO " + savepoint)
                    self.db.execute("RELEASE " + savepoint)
                else:
                    self.db.execute("ROLLBACK")
                raise

    def event(self, kind, task_id, payload):
        self.db.execute(
            "INSERT INTO events(kind,task_id,payload,created_at) VALUES(?,?,?,?)",
            (kind, task_id, json.dumps(payload), time.time()),
        )

    def create_resource(
        self,
        *,
        filename,
        blob_key,
        size,
        operation_key,
        bundle_key=None,
        source=None,
        records=None,
        layout_key=None,
    ):
        if not operation_key or len(operation_key) > 128:
            raise ApplicationError("invalid_key", "operation key must contain 1–128 characters")
        request_digest = hashlib.sha256(json_bytes([filename, blob_key, bundle_key])).hexdigest()
        with self.tx():
            previous = self.db.execute(
                "SELECT * FROM tasks WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ApplicationError(
                        "idempotency_conflict", "same key belongs to a different input"
                    )
                return record(previous)
            resource_id, version_id, revision_id, task_id = (new_id() for _ in range(4))
            now = time.time()
            ready = records is not None
            page_count, layout_error, _meta = self._layout_facts(layout_key)
            self.db.execute("INSERT INTO resources VALUES(?,?,?)", (resource_id, filename, now))
            self.db.execute(
                """INSERT INTO versions(id,resource_id,filename,source_digest,size_bytes,
                blob_key,parse_revision,state,layout_key,bundle_key,source_json,page_count,
                layout_error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    resource_id,
                    filename,
                    blob_key,
                    size,
                    blob_key,
                    source["parse_revision"] if source else revision_id,
                    ("unparsed" if source and source["parse_revision"] is None else "ready")
                    if ready
                    else "queued",
                    layout_key,
                    bundle_key,
                    json.dumps(source) if source else None,
                    page_count,
                    layout_error,
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO tasks(id,kind,version_id,operation_key,request_digest,status,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    "import" if ready else "parse",
                    version_id,
                    operation_key,
                    request_digest,
                    "succeeded" if ready else "queued",
                    now,
                    now,
                ),
            )
            if ready:
                self._index(version_id, records)
            self.event(
                "task.created",
                task_id,
                {"version_id": version_id, "status": "succeeded" if ready else "queued"},
            )
            return dict(self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def append_version(
        self,
        *,
        resource_id,
        filename,
        blob_key,
        size,
        operation_key,
        bundle_key=None,
        source=None,
        records=None,
        layout_key=None,
    ):
        """A second immutable version under an existing logical resource.

        The new version gets its own id, parse revision, task and (when
        ``records`` is given) evidence rows. Earlier versions are never
        rewritten: their bytes, digest, size and identity stay fixed so file
        plans and Wiki dependency manifests keep pointing at the same facts.
        """
        if not operation_key or len(operation_key) > 128:
            raise ApplicationError("invalid_key", "operation key must contain 1–128 characters")
        request_digest = hashlib.sha256(
            json_bytes([resource_id, filename, blob_key, bundle_key])
        ).hexdigest()
        with self.tx():
            previous = self.db.execute(
                "SELECT * FROM tasks WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ApplicationError(
                        "idempotency_conflict", "same key belongs to a different input"
                    )
                return record(previous)
            owner = self.db.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)
            ).fetchone()
            if owner is None:
                raise ApplicationError("not_found", "resource not found in this workspace")
            version_id, revision_id, task_id = (new_id() for _ in range(3))
            now = time.time()
            ready = records is not None
            page_count, layout_error, _meta = self._layout_facts(layout_key)
            self.db.execute(
                """INSERT INTO versions(id,resource_id,filename,source_digest,size_bytes,
                blob_key,parse_revision,state,layout_key,bundle_key,source_json,page_count,
                layout_error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    version_id,
                    resource_id,
                    filename,
                    blob_key,
                    size,
                    blob_key,
                    source["parse_revision"] if source else revision_id,
                    ("unparsed" if source and source["parse_revision"] is None else "ready")
                    if ready
                    else "queued",
                    layout_key,
                    bundle_key,
                    json.dumps(source) if source else None,
                    page_count,
                    layout_error,
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO tasks(id,kind,version_id,operation_key,request_digest,status,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    "import" if ready else "parse",
                    version_id,
                    operation_key,
                    request_digest,
                    "succeeded" if ready else "queued",
                    now,
                    now,
                ),
            )
            if ready:
                self._index(version_id, records)
            self.event(
                "task.created",
                task_id,
                {"version_id": version_id, "status": "succeeded" if ready else "queued"},
            )
            return dict(self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def resource(self, resource_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)
            ).fetchone()
            if row is None:
                raise ApplicationError("not_found", "resource not found in this workspace")
            return dict(row)

    def resource_versions(self, resource_id):
        self.resource(resource_id)
        with self.lock:
            return [
                record(r)
                for r in self.db.execute(
                    "SELECT * FROM versions WHERE resource_id=? ORDER BY created_at DESC",
                    (resource_id,),
                )
            ]

    def resource_command(self, action, target, *, operation_key, blobs=None):
        """Resource mutation and its durable receipt commit in the same transaction.

        Delete commands reclaim unreferenced blob bytes when the caller passes
        the blob store, but only after the delete and its receipt commit: the
        sweep touches the filesystem, which cannot roll back with the row
        delete, so it must not observe (or run inside) the uncommitted delete.
        A refused sweep is reported as ``blob_gc: {error: code}`` alongside
        the stored receipt rather than failing the delete.
        """
        operations = {"version.withdraw": self.withdraw_version, "version.delete": self.delete_version,
                      "resource.delete": self.delete_resource}
        if action not in operations:
            raise ApplicationError("unsupported_operation", "unsupported local resource operation")
        failure = None
        sweep = None
        with self.tx():
            task, created = self.begin_generation(action, {"target": target}, operation_key)
            if not created:
                return self.generation_result(task)
            try:
                result = operations[action](target)
            except ApplicationError as exc:
                self.fail(task, exc.code, str(exc))
                failure = exc
            else:
                output_id = new_id()
                result = {**result, "task_id": task["id"]}
                self.db.execute("INSERT INTO outputs VALUES(?,?,?,?)",
                                (output_id, action, json.dumps(result), time.time()))
                self.db.execute("UPDATE tasks SET status='succeeded',result=?,lease_until=NULL,updated_at=? WHERE id=?",
                                (json.dumps({"output_id": output_id}), time.time(), task["id"]))
                self.event("task.succeeded", task["id"], {"output_id": output_id})
                sweep = action in {"version.delete", "resource.delete"} and blobs is not None
        if failure is not None:
            raise failure
        if sweep:
            result["blob_gc"] = self._sweep_after_delete(blobs)
            with self.tx():
                task = self.task(result["task_id"])
                output_id = task["result"]["output_id"]
                self.db.execute("UPDATE outputs SET body=? WHERE id=?",
                                (json.dumps(result), output_id))
        return result

    def withdraw_version(self, version_id):
        """Revoke a source version without rewriting or deleting history.

        Withdrawn versions stay projected (bytes, digest, size, identity) but
        stop authorizing new retrieval, Wiki freezes and file-plan consent:
        ``authorize_versions`` only accepts ``ready`` versions. Queued/running
        tasks bound to the version are fenced to ``cancelled`` so a restart
        cannot silently parse it back to ``ready``. Existing Wiki revisions
        keep their dependency rows and read back as ``stale``.
        """
        with self.tx():
            version = self.version(version_id)
            if version["state"] == "withdrawn":
                return version
            now = time.time()
            for row in self.db.execute(
                "SELECT id FROM tasks WHERE version_id=? AND status IN ('queued','running')",
                (version_id,),
            ).fetchall():
                self.db.execute(
                    "UPDATE tasks SET "
                    "status='cancelled',generation=generation+1,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self.event("task.cancelled", row["id"], {"reason": "source_withdrawn"})
            self.db.execute(
                "UPDATE versions SET state='withdrawn',error='withdrawn' WHERE id=?",
                (version_id,),
            )
            self.event("version.withdrawn", None, {"version_id": version_id})
            return self.version(version_id)

    def _guard_delete(self, version_ids):
        placeholders = ",".join("?" for _ in version_ids)
        if self.db.execute(
            f"SELECT 1 FROM wiki_dependencies WHERE local_version_id IN ({placeholders}) LIMIT 1",
            (*version_ids,),
        ).fetchone():
            raise ApplicationError(
                "source_in_use", "Wiki revisions retain this immutable evidence version"
            )
        if self.db.execute(
            f"SELECT 1 FROM tasks WHERE version_id IN ({placeholders}) "
            "AND status IN ('queued','running') LIMIT 1",
            (*version_ids,),
        ).fetchone():
            raise ApplicationError(
                "task_in_progress", "cancel the active task before deleting this version"
            )
        if self.db.execute(
            "SELECT 1 FROM citations JOIN evidence ON evidence.id=citations.evidence_id "
            f"WHERE evidence.version_id IN ({placeholders}) LIMIT 1",
            (*version_ids,),
        ).fetchone():
            raise ApplicationError(
                "source_in_use", "answered conversations retain this immutable evidence version"
            )

    def _sweep_after_delete(self, blobs):
        """Best-effort post-commit blob sweep; a failure never un-deletes rows.

        The delete transaction already committed before this runs, so a sweep
        refusal (an unreadable upload session payload fails closed) is
        reported inside the delete result instead of raising: raising here
        would report failure for a delete that already happened.
        """
        try:
            return self.sweep_unreferenced_blobs(blobs)
        except ApplicationError as exc:
            return {"error": exc.code}

    def delete_version(self, version_id, *, blobs=None):
        """Physically remove one version that nothing retains.

        Refuses with ``source_in_use`` while any Wiki dependency manifest
        references the version, and with ``task_in_progress`` while a task is
        still queued/running. Content-addressed blob bytes outliving this
        delete are reclaimed by ``sweep_unreferenced_blobs`` when the caller
        passes the blob store; without it the bytes stay on disk (they may
        be shared with other versions and are never trusted by name). The
        sweep runs after the delete commits, so a sweep failure is reported
        as ``blob_gc: {error: code}`` with ``deleted: True``, never raised.
        """
        with self.tx():
            version = self.version(version_id)
            self._guard_delete([version_id])
            self.db.execute("DELETE FROM evidence_fts WHERE version_id=?", (version_id,))
            self.db.execute("DELETE FROM evidence WHERE version_id=?", (version_id,))
            self._delete_conversations_for_versions([version_id])
            self.db.execute("DELETE FROM tasks WHERE version_id=?", (version_id,))
            self.db.execute("DELETE FROM versions WHERE id=?", (version_id,))
            resource_id = version["resource_id"]
            if not self.db.execute(
                "SELECT 1 FROM versions WHERE resource_id=? LIMIT 1", (resource_id,)
            ).fetchone():
                self.db.execute("DELETE FROM resources WHERE id=?", (resource_id,))
            self.event(
                "version.deleted", None,
                {"version_id": version_id, "resource_id": resource_id},
            )
            result = {"version_id": version_id, "resource_id": resource_id, "deleted": True}
        if blobs is not None:
            result["blob_gc"] = self._sweep_after_delete(blobs)
        return result

    def delete_resource(self, resource_id, *, blobs=None):
        """Remove a whole logical resource once no Wiki or active task retains it.

        Blob bytes outliving this delete are reclaimed by
        ``sweep_unreferenced_blobs`` when the caller passes the blob store;
        without it they stay on disk (shared with other rows or already
        orphaned, but never trusted by name). The sweep runs after the delete
        commits, so a sweep failure is reported as ``blob_gc: {error: code}``
        with ``deleted: True``, never raised.
        """
        with self.tx():
            self.resource(resource_id)
            version_ids = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id FROM versions WHERE resource_id=?", (resource_id,)
                ).fetchall()
            ]
            if version_ids:
                self._guard_delete(version_ids)
                placeholders = ",".join("?" for _ in version_ids)
                self.db.execute(
                    f"DELETE FROM evidence_fts WHERE version_id IN ({placeholders})",
                    (*version_ids,),
                )
                self.db.execute(
                    f"DELETE FROM evidence WHERE version_id IN ({placeholders})",
                    (*version_ids,),
                )
                self.db.execute(
                    f"DELETE FROM tasks WHERE version_id IN ({placeholders})",
                    (*version_ids,),
                )
                self._delete_conversations_for_versions(version_ids)
                self.db.execute(
                    "DELETE FROM versions WHERE resource_id=?", (resource_id,)
                )
            self.db.execute("DELETE FROM resources WHERE id=?", (resource_id,))
            self.event("resource.deleted", None, {"resource_id": resource_id})
            result = {
                "resource_id": resource_id,
                "deleted": True,
                "deleted_versions": sorted(version_ids),
            }
        if blobs is not None:
            result["blob_gc"] = self._sweep_after_delete(blobs)
        return result
    # -- Center-content: conversations, answers, uploads ----------------------
    def _conversation_ids_for_versions(self, version_ids):
        placeholders = ",".join("?" for _ in version_ids)
        resource_rows = self.db.execute(
            f"SELECT DISTINCT resource_id FROM versions WHERE id IN ({placeholders})",
            (*version_ids,),
        ).fetchall()
        resource_ids = [row["resource_id"] for row in resource_rows]
        found = set()
        for row in self.db.execute(
            "SELECT id, document_id, resource_id FROM conversations"
        ).fetchall():
            if row["document_id"] in set(version_ids) or (
                row["resource_id"] and row["resource_id"] in set(resource_ids)
            ):
                found.add(row["id"])
        return sorted(found)

    def _delete_conversations_for_versions(self, version_ids):
        conversation_ids = self._conversation_ids_for_versions(version_ids)
        if not conversation_ids:
            return
        placeholders = ",".join("?" for _ in conversation_ids)
        assertion_ids = [
            row["id"]
            for row in self.db.execute(
                f"SELECT id FROM assertions WHERE message_id IN "
                f"(SELECT id FROM messages WHERE conversation_id IN ({placeholders}))",
                (*conversation_ids,),
            ).fetchall()
        ]
        if assertion_ids:
            self.db.execute(
                f"DELETE FROM citations WHERE assertion_id IN "
                f"({','.join('?' for _ in assertion_ids)})",
                (*assertion_ids,),
            )
            self.db.execute(
                f"DELETE FROM assertions WHERE id IN ({','.join('?' for _ in assertion_ids)})",
                (*assertion_ids,),
            )
        self.db.execute(
            f"DELETE FROM messages WHERE conversation_id IN ({placeholders})",
            (*conversation_ids,),
        )
        self.db.execute(
            f"DELETE FROM conversations WHERE id IN ({placeholders})",
            (*conversation_ids,),
        )
        for conversation_id in conversation_ids:
            self.event("conversation.deleted", None, {"conversation_id": conversation_id})

    def create_conversation(self, document_id, *, resource_id=None):
        now = time.time()
        with self.tx():
            conversation_id = new_id()
            self.db.execute(
                "INSERT INTO conversations(id,document_id,title,resource_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (conversation_id, document_id, "新会话", resource_id, now, now),
            )
            return self.conversation(conversation_id)

    def conversation(self, conversation_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise ApplicationError("not_found", "conversation not found in this workspace")
            return dict(row)

    def conversations_for_document(self, document_id):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM conversations WHERE document_id=? ORDER BY updated_at DESC,id DESC",
                    (document_id,),
                ).fetchall()
            ]

    def delete_conversation(self, conversation_id):
        with self.tx():
            self.conversation(conversation_id)
            assertion_ids = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id FROM assertions WHERE message_id IN "
                    "(SELECT id FROM messages WHERE conversation_id=?)",
                    (conversation_id,),
                ).fetchall()
            ]
            if assertion_ids:
                self.db.execute(
                    f"DELETE FROM citations WHERE assertion_id IN "
                    f"({','.join('?' for _ in assertion_ids)})",
                    (*assertion_ids,),
                )
                self.db.execute(
                    f"DELETE FROM assertions WHERE id IN ({','.join('?' for _ in assertion_ids)})",
                    (*assertion_ids,),
                )
            self.db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            self.db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
            self.event("conversation.deleted", None, {"conversation_id": conversation_id})
            return {"conversation_id": conversation_id, "deleted": True}

    def add_user_message(self, conversation_id, content):
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            raise ApplicationError("invalid_question", "question must contain 1–2000 characters")
        now = time.time()
        with self.tx():
            current = self.conversation(conversation_id)
            message_id = new_id()
            self.db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,verified,degraded,"
                "model_meta,confidence,query_json,created_at)"
                " VALUES(?,?, 'user', ?, 0, NULL, '{}', NULL, NULL, ?)",
                (message_id, conversation_id, content, now),
            )
            if current["title"] == "新会话":
                self.db.execute(
                    "UPDATE conversations SET title=?,updated_at=? WHERE id=?",
                    (content[:40], now, conversation_id),
                )
            else:
                self.db.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now, conversation_id),
                )
            return dict(
                self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            )

    def add_assistant_message(self, conversation_id, *, content, query_decision,
                              citations, assertions, degraded, verified, model_meta,
                              confidence):
        now = time.time()
        with self.tx():
            self.conversation(conversation_id)
            message_id = new_id()
            self.db.execute(
                "INSERT INTO messages(id,conversation_id,role,content,verified,degraded,"
                "model_meta,confidence,query_json,created_at)"
                " VALUES(?,?, 'assistant', ?, ?, ?, ?, ?, ?, ?)",
                (message_id, conversation_id, content, int(bool(verified)),
                 degraded, json.dumps(model_meta or {}),
                 json.dumps(confidence) if confidence is not None else None,
                 json.dumps(query_decision or {}), now),
            )
            stored_assertions = []
            for rank, item in enumerate(assertions):
                assertion_id = new_id()
                evidence_ids = list(dict.fromkeys(item.get("evidence_ids") or []))
                verification = item.get("verification") or {}
                self.db.execute(
                    "INSERT INTO assertions(id,message_id,position,text,evidence_ids,"
                    "verification_state,verification_mode,unsupported)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (assertion_id, message_id, item.get("position", rank), item.get("text", ""),
                     json.dumps(evidence_ids), verification.get("state", "unverified"),
                     verification.get("mode"), int(bool(item.get("unsupported", True)))),
                )
                for position, evidence_id in enumerate(evidence_ids):
                    hit = next(
                        (c for c in citations if c.get("evidence_id") == evidence_id), None
                    )
                    if hit is None:
                        continue
                    self.db.execute(
                        "INSERT INTO citations(id,assertion_id,evidence_id,document_id,rank,score,similarity)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (new_id(), assertion_id, evidence_id, hit.get("document_id", ""),
                         position, hit.get("score"), hit.get("similarity")),
                    )
                stored_assertions.append(
                    dict(
                        self.db.execute(
                            "SELECT * FROM assertions WHERE id=?", (assertion_id,)
                        ).fetchone()
                    )
                )
            self.db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
            )
            message = dict(
                self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            )
            return message, stored_assertions

    def conversation_messages(self, conversation_id):
        self.conversation(conversation_id)
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at,id",
                    (conversation_id,),
                ).fetchall()
            ]

    def assertions_for_message(self, message_id):
        with self.lock:
            return [
                {**dict(row), "evidence_ids": json.loads(row["evidence_ids"])}
                for row in self.db.execute(
                    "SELECT * FROM assertions WHERE message_id=? ORDER BY position,id",
                    (message_id,),
                ).fetchall()
            ]

    def citations_for_assertion(self, assertion_id):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM citations WHERE assertion_id=? ORDER BY rank,id",
                    (assertion_id,),
                ).fetchall()
            ]

    def latest_evidence_ids(self, conversation_id):
        """Ordered evidence of the latest assistant message; one turn only, no chaining."""
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant'"
                " ORDER BY created_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row is None:
                return []
            ordered = []
            for assertion in self.db.execute(
                "SELECT evidence_ids FROM assertions WHERE message_id=? ORDER BY position,id",
                (row["id"],),
            ).fetchall():
                for evidence_id in json.loads(assertion["evidence_ids"]):
                    if evidence_id not in ordered:
                        ordered.append(evidence_id)
            return ordered

    def backlinks_for_evidence(self, evidence_id):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT citations.*, assertions.text AS label, assertions.message_id,"
                    " messages.conversation_id FROM citations"
                    " JOIN assertions ON assertions.id=citations.assertion_id"
                    " JOIN messages ON messages.id=assertions.message_id"
                    " WHERE citations.evidence_id=? ORDER BY messages.created_at,citations.id",
                    (evidence_id,),
                ).fetchall()
            ]

    def wiki_claims_for_evidence(self, evidence_id):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT wiki_claim_bindings.*, wikis.title AS wiki_title FROM wiki_claim_bindings"
                    " JOIN wikis ON wikis.id=wiki_claim_bindings.wiki_id"
                    " WHERE wiki_claim_bindings.evidence_id=? ORDER BY wiki_id,claim_id",
                    (evidence_id,),
                ).fetchall()
            ]

    def record_wiki_claim_bindings(self, revision_id, wiki_id, pages, frozen_by_id):
        """Persist current-revision claim bindings for backlinks; only for readable revisions."""
        import hashlib as _hashlib

        self.db.execute("DELETE FROM wiki_claim_bindings WHERE revision_id=?", (revision_id,))
        seen = set()
        for page in pages:
            for section in page.get("generated_sections") or []:
                for claim in section.get("sentences") or []:
                    for evidence_id in claim.get("evidence_ids") or []:
                        frozen = frozen_by_id.get(evidence_id)
                        if frozen is None or (revision_id, claim["id"], evidence_id) in seen:
                            continue
                        seen.add((revision_id, claim["id"], evidence_id))
                        self.db.execute(
                            "INSERT INTO wiki_claim_bindings VALUES(?,?,?,?,?,?)",
                            (revision_id, wiki_id, page["page_key"], claim["id"], evidence_id,
                             _hashlib.sha256(frozen["excerpt"].encode()).hexdigest()),
                        )
    # -- Center-content: upload sessions (same-origin direct PUT) --------------
    def create_upload_session(self, *, filename, mime, declared_size, declared_sha256,
                              target_resource_id, idempotency_key):
        if target_resource_id is not None:
            self.resource(target_resource_id)
        request_digest = hashlib.sha256(
            json_bytes([filename, mime, declared_size, declared_sha256, target_resource_id])
        ).hexdigest()
        now = time.time()
        with self.tx():
            if idempotency_key:
                previous = self.db.execute(
                    "SELECT * FROM upload_sessions WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if previous is not None:
                    if previous["request_digest"] != request_digest:
                        raise ApplicationError(
                            "idempotency_conflict", "same key belongs to a different upload"
                        )
                    return dict(previous)
            session_id = new_id()
            self.db.execute(
                "INSERT INTO upload_sessions(id,filename,mime,declared_size,declared_sha256,"
                "target_resource_id,idempotency_key,request_digest,status,parts_json,"
                "resource_id,version_id,error,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?, 'uploading', '{}', NULL, NULL, NULL, ?, ?)",
                (session_id, filename, mime, declared_size, declared_sha256,
                 target_resource_id, idempotency_key, request_digest, now, now),
            )
            return dict(
                self.db.execute(
                    "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                ).fetchone()
            )

    def upload_session(self, session_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise ApplicationError("not_found", "upload session not found in this workspace")
            return dict(row)

    def store_upload_part(self, session_id, part_number, key, size):
        if type(part_number) is not int or part_number < 1 or part_number > MAX_UPLOAD_PARTS:
            raise ApplicationError("invalid_part", "part_number must be within 1..%d" % MAX_UPLOAD_PARTS)
        if type(size) is not int or size < 1 or size > MAX_UPLOAD_PART:
            raise ApplicationError("input_too_large", "upload part exceeds the 8 MiB part budget")
        with self.tx():
            session = self.upload_session(session_id)
            if session["status"] not in {"uploading", "created"}:
                raise ApplicationError("upload_finalized", "this upload session already finalized")
            parts = json.loads(session["parts_json"])
            if str(part_number) not in parts and len(parts) >= MAX_UPLOAD_PARTS:
                raise ApplicationError("input_too_large", "upload exceeds the part-count budget")
            parts[str(part_number)] = {"blob_key": key, "size": size}
            self.db.execute(
                "UPDATE upload_sessions SET parts_json=?,status='uploading',updated_at=? WHERE id=?",
                (json.dumps(parts), time.time(), session_id),
            )
            return parts[str(part_number)]

    def finalize_upload_session(self, session_id, open_part_stream, put_stream):
        """Stream part bytes through hash+concat, verify, and create the resource/version.

        Runs synchronously: the local runtime has no object-storage round trip
        to poll, so ingest_status is ready (or rejected) on return. The parsed
        content still needs the background parse task; that is the parse task
        row, not the upload session. Part bytes live in the content-addressed
        blob store; the session only keeps their keys.

        Part streams are chained into ``put_stream`` (bounded at the 32 MiB
        local budget), so peak memory is O(chunk): no part is ever fully
        joined in RAM. Declared size, SHA-256 and the %PDF- magic are all
        verified from the streamed bytes; the magic check only peeks at the
        first five bytes of the first stream. Up to 8 x 8 MiB (64 MiB) may be
        staged on disk, but the 32 MiB total is enforced during the streaming
        concat, so finalize fails closed before memory grows.

        The session snapshot is taken in a short transaction, byte streaming
        runs outside any DB transaction, and verification plus the
        resource/version commit in a fresh transaction. Rejection markers use
        their own transaction so an outer rollback cannot undo the
        failed-status UPDATE (otherwise ingest_status would stay pending).
        A single part reuses its blob directly: its key already is its
        SHA-256, so no second put_stream (which would hit FileExistsError and
        re-verify via read); the bytes are still streamed once for size,
        digest and magic.
        """
        from ddp_local.blobs import MAX_INPUT

        def _mark_failed(code):
            with self.tx():
                self.db.execute(
                    "UPDATE upload_sessions SET status='failed',error=?,updated_at=? WHERE id=?",
                    (code, time.time(), session_id),
                )

        try:
            with self.tx():
                session = self.upload_session(session_id)
                if session["status"] == "ready":
                    return dict(
                        self.db.execute(
                            "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                        ).fetchone()
                    )
                if session["status"] not in {"uploading", "created"}:
                    raise ApplicationError("upload_finalized", "this upload session already finalized")
                parts = json.loads(session["parts_json"])
                if not parts:
                    raise ApplicationError("upload_incomplete", "no upload parts received yet")
                if len(parts) > MAX_UPLOAD_PARTS:
                    raise ApplicationError("input_too_large", "upload exceeds the part-count budget")
                numbers = sorted(int(value) for value in parts)
                keys = [parts[str(number)] for number in numbers]
                if any(not isinstance(entry, dict) or not isinstance(entry.get("blob_key"), str)
                       for entry in keys):
                    raise ApplicationError("upload_incomplete", "upload parts are missing blob references")
                snapshot = {
                    "declared_size": session["declared_size"],
                    "declared_sha256": session["declared_sha256"],
                    "target_resource_id": session["target_resource_id"],
                    "filename": session["filename"],
                }
        except ApplicationError as exc:
            if exc.code == "input_too_large" or (
                exc.code == "upload_incomplete" and "blob references" in str(exc)
            ):
                _mark_failed(exc.code)
            raise

        streams: list = []
        try:
            for entry in keys:
                streams.append(open_part_stream(entry["blob_key"]))
            chained = _ChainedStreams(streams)
            if len(keys) == 1:
                size = 0
                while True:
                    chunk = chained.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_INPUT:
                        _mark_failed("input_too_large")
                        raise ApplicationError("input_too_large", "upload exceeds the 32 MiB local budget")
                digest = chained.hexdigest()
                head = chained.head()
                blob_key = keys[0]["blob_key"]
                if digest != blob_key:
                    _mark_failed("blob_corrupt")
                    raise ApplicationError("blob_corrupt", "stored upload part digest does not match")
            else:
                try:
                    blob_key, size = put_stream(chained, maximum=MAX_INPUT)
                except ApplicationError as exc:
                    if exc.code == "input_too_large":
                        _mark_failed("input_too_large")
                    raise
                digest = chained.hexdigest()
                head = chained.head()
        finally:
            for stream in streams:
                try:
                    stream.close()
                except Exception:
                    pass

        try:
            with self.tx():
                session = self.upload_session(session_id)
                if session["status"] == "ready":
                    return dict(
                        self.db.execute(
                            "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                        ).fetchone()
                    )
                if session["status"] not in {"uploading", "created"}:
                    raise ApplicationError("upload_finalized", "this upload session already finalized")
                if size != snapshot["declared_size"]:
                    raise ApplicationError("size_mismatch", "uploaded bytes differ from the declared size")
                if not head.startswith(b"%PDF-"):
                    raise ApplicationError("invalid_pdf", "local uploads accept PDF bytes only")
                if snapshot["declared_sha256"] and digest != snapshot["declared_sha256"]:
                    raise ApplicationError("digest_mismatch", "uploaded bytes differ from the declared digest")
                self.db.execute(
                    "UPDATE upload_sessions SET status='verifying',updated_at=? WHERE id=?",
                    (time.time(), session_id),
                )
                if snapshot["target_resource_id"]:
                    task = self.append_version(
                        resource_id=snapshot["target_resource_id"], filename=snapshot["filename"],
                        blob_key=blob_key, size=size,
                        operation_key="upload:" + session_id,
                        bundle_key=None, source=None, records=None, layout_key=None,
                    )
                    created_resource, created_version = snapshot["target_resource_id"], task["version_id"]
                else:
                    task = self.create_resource(
                        filename=snapshot["filename"], blob_key=blob_key, size=size,
                        operation_key="upload:" + session_id,
                        bundle_key=None, source=None, records=None, layout_key=None,
                    )
                    created_resource = self.db.execute(
                        "SELECT resource_id FROM versions WHERE id=?", (task["version_id"],)
                    ).fetchone()["resource_id"]
                    created_version = task["version_id"]
                self.db.execute(
                    "UPDATE upload_sessions SET status='ready',resource_id=?,version_id=?,"
                    "updated_at=? WHERE id=?",
                    (created_resource, created_version, time.time(), session_id),
                )
                return dict(
                    self.db.execute(
                        "SELECT * FROM upload_sessions WHERE id=?", (session_id,)
                    ).fetchone()
                )
        except ApplicationError as exc:
            if exc.code in {"size_mismatch", "invalid_pdf", "digest_mismatch",
                            "upload_incomplete", "input_too_large", "blob_corrupt"}:
                try:
                    _mark_failed(exc.code)
                except ApplicationError:
                    pass
            raise


    def versions(self):
        with self.lock:
            return [
                record(r)
                for r in self.db.execute("SELECT * FROM versions ORDER BY created_at DESC")
            ]

    def versions_page(self, *, filename_like="", statuses=(), limit=50, offset=0):
        """Paginated version rows for list paths; never loads every version row.

        ``filename_like`` is a caller-supplied substring matched with LIKE
        (escaped, case-insensitive); ``statuses`` filters the projected
        document status (parse_status values ``pending``/``running``/
        ``succeeded``/``failed``, same projection ``_version_state_to_parse``
        the list rows carry). Unknown statuses reject; unparseable stored
        states surface under ``failed`` exactly as the projection maps them.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ApplicationError("invalid_window", "offset/limit window is invalid")
        if type(offset) is not int or offset < 0:
            raise ApplicationError("invalid_window", "offset/limit window is invalid")
        clauses, args = [], []
        if filename_like:
            escaped = "".join(
                "\\" + c if c in ("\\", "%", "_") else c for c in filename_like
            )
            clauses.append("filename LIKE ? ESCAPE '\\' COLLATE NOCASE")
            args.append("%" + escaped + "%")
        wanted = [s for s in statuses if isinstance(s, str) and s]
        if statuses and not wanted:
            raise ApplicationError("invalid_status", "unknown document status filter")
        if wanted:
            known = {"pending", "running", "succeeded", "failed"}
            unknown = sorted(set(wanted) - known)
            if unknown:
                raise ApplicationError("invalid_status", "unknown document status filter")
            states: set[str] = set()
            for status in wanted:
                states.update(_PARSE_STATUS_STATES[status])
            clauses.append("state IN (%s)" % ",".join("?" for _ in sorted(states)))
            args.extend(sorted(states))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.lock:
            return [
                record(r)
                for r in self.db.execute(
                    "SELECT * FROM versions" + where +
                    " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]

    def document_stats(self):
        """Aggregate counts for the documents summary without layout-blob reads."""
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS documents,"
                " COALESCE(SUM(COALESCE(page_count, 0)), 0) AS pages,"
                " COALESCE(SUM(CASE WHEN state='ready' THEN 1 ELSE 0 END), 0) AS askable"
                " FROM versions"
            ).fetchone()
            return {"documents": row["documents"], "pages": row["pages"], "askable": row["askable"]}

    def version(self, version_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise ApplicationError("not_found", "version not found in this workspace")
            return record(row)

    def authorize_versions(self, version_ids):
        rows = [self.version(i) for i in dict.fromkeys(version_ids)]
        if any(v["state"] != "ready" for v in rows):
            raise ApplicationError(
                "version_not_ready", "one or more selected versions are not ready"
            )
        return [v["id"] for v in rows]

    def task(self, task_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ApplicationError("not_found", "task not found")
            return record(row)

    def tasks(self):
        with self.lock:
            return [
                record(r)
                for r in self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 200")
            ]

    def receipt(self, operation_key):
        """Read an existing admission; absence must never create or repeat work."""
        if not operation_key or len(operation_key) > 128:
            raise ApplicationError("invalid_key", "operation key must contain 1–128 characters")
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row is None:
                raise ApplicationError("not_found", "operation has not been admitted here")
            return record(row)

    def client_cursor(self, sequence):
        return f"local.{self.workspace_id}.{sequence}"

    def _client_sequence(self, cursor):
        prefix = f"local.{self.workspace_id}."
        if not isinstance(cursor, str) or not cursor.startswith(prefix):
            raise ApplicationError("cursor_expired", "cursor does not belong to this workspace")
        value = cursor[len(prefix):]
        if not re.fullmatch(r"0|[1-9][0-9]{0,15}", value) or int(value) > 2**53 - 1:
            raise ApplicationError("cursor_expired", "cursor is invalid; obtain a new snapshot")
        return int(value)

    def client_snapshot(self, capabilities, *, after=None):
        """One WAL read snapshot binds the projection to its durable event cursor.

        A second process may commit during these reads; BEGIN plus the first
        SELECT prevents mixing its new resources with our older sequence.
        """
        previous = self._client_sequence(after) if after is not None else None
        with self.lock:
            self.db.execute("BEGIN")
            try:
                sequence = self.db.execute(
                    "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
                ).fetchone()[0]
                first = self.db.execute("SELECT min(seq) FROM events").fetchone()[0]
                if previous is not None and (
                    previous > sequence or previous < (first or sequence + 1) - 1
                ):
                    raise ApplicationError(
                        "cursor_expired", "event history is unavailable; obtain a new snapshot"
                    )
                tasks = []
                for row in self.db.execute("SELECT * FROM tasks ORDER BY created_at DESC,id"):
                    task = record(row)
                    # Heartbeats change these fields without a user-visible event.
                    # Keep one semantic projection for each acknowledged cursor.
                    task.pop("lease_until", None)
                    task.pop("updated_at", None)
                    tasks.append(task)
                projection = {
                    "cursor": self.client_cursor(sequence),
                    "sequence": sequence,
                    "state": {"resources": self.versions(), "tasks": tasks,
                              "capabilities": capabilities},
                }
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        if previous is not None:
            projection["previous_sequence"] = previous
        return projection

    def claim(self):
        with self.tx():
            now = time.time()
            # A generation may have reached its provider before the runtime crashed.
            # Surface the uncertain interruption; never repeat a paid remote call automatically.
            interrupted = self.db.execute(
                "SELECT id FROM tasks WHERE kind!='parse' AND "
                "status='running' AND lease_until<?",
                (now,),
            ).fetchall()
            for item in interrupted:
                self.db.execute(
                    "UPDATE tasks SET "
                    "status='failed',error='execution_interrupted',updated_at=? WHERE id=?",
                    (now, item["id"]),
                )
                self.event("task.failed", item["id"], {"code": "execution_interrupted"})
            row = self.db.execute(
                """SELECT t.* FROM tasks t JOIN versions v ON v.id=t.version_id
                WHERE t.kind='parse' AND v.state!='withdrawn' AND
                (t.status='queued' OR (t.status='running' AND t.lease_until<?))
                ORDER BY t.created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            if row["attempts"] >= 3:
                self.db.execute(
                    "UPDATE tasks SET status='failed',error='attempt_limit',updated_at=? "
                    "WHERE id=?",
                    (now, row["id"]),
                )
                self.db.execute(
                    "UPDATE versions SET state='failed',error='attempt_limit' WHERE id=?",
                    (row["version_id"],),
                )
                self.event("task.failed", row["id"], {"code": "attempt_limit"})
                return None
            self.db.execute(
                """UPDATE tasks SET status='running',generation=generation+1,
                attempts=attempts+1,lease_until=?,updated_at=? WHERE id=?""",
                (now + 45, now, row["id"]),
            )
            self.db.execute("UPDATE versions SET state='parsing' WHERE id=?", (row["version_id"],))
            self.event("task.running", row["id"], {})
            return record(
                self.db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            )

    def renew(self, task_id, generation):
        with self.tx():
            cursor = self.db.execute(
                "UPDATE tasks SET lease_until=?,updated_at=? WHERE id=? AND generation=? "
                "AND status='running'",
                (time.time() + 45, time.time(), task_id, generation),
            )
            return cursor.rowcount == 1

    def cancel(self, task_id):
        with self.tx():
            row = self.task(task_id)
            if row["status"] in ("queued", "running"):
                self.db.execute(
                    "UPDATE tasks SET "
                    "status='cancelled',generation=generation+1,updated_at=? WHERE id=?",
                    (time.time(), task_id),
                )
                self.db.execute(
                    "UPDATE versions SET state='failed',error='cancelled' WHERE id=?",
                    (row["version_id"],),
                )
                self.event("task.cancelled", task_id, {})
            return self.task(task_id)

    def fail(self, task, code, message):
        with self.tx():
            count = self.db.execute(
                "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=? AND "
                "generation=? AND status='running'",
                (code, time.time(), task["id"], task["generation"]),
            ).rowcount
            if count:
                self.db.execute(
                    "UPDATE versions SET state='failed',error=? WHERE id=?",
                    (code, task["version_id"]),
                )
                self.event("task.failed", task["id"], {"code": code, "message": message})

    def _index(self, version_id, records):
        if self.db.execute("SELECT 1 FROM wiki_dependencies WHERE local_version_id=? LIMIT 1", (version_id,)).fetchone():
            raise ApplicationError("source_in_use", "Wiki revisions retain this immutable evidence version")
        self.db.execute("DELETE FROM evidence_fts WHERE version_id=?", (version_id,))
        self.db.execute("DELETE FROM evidence WHERE version_id=?", (version_id,))
        for record in records:
            e, chunk = record["evidence"], record["chunk"]
            # The database key is local; the original stable identity stays in the envelope.
            local_id = hashlib.sha256(json_bytes([version_id, e["evidence_id"]])).hexdigest()[:32]
            self.db.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                (
                    local_id,
                    version_id,
                    chunk["seq"],
                    record["excerpt"],
                    json.dumps(e),
                    json.dumps(chunk),
                ),
            )
            self.db.execute(
                "INSERT INTO evidence_fts VALUES(?,?,?)",
                (local_id, version_id, chunk["text_tokenized"]),
            )

    def _layout_facts(self, layout_key):
        """Cached (page_count, layout_error, meta) for one layout blob, bounded read.

        No layout yet means no facts (NULL/NULL/{}): the version simply has no
        compiled pages. A layout that exists but cannot be parsed is visible
        as layout_error instead of raising; list paths must never silently 0.
        ``meta`` carries layout_version/code_detection so the projection needs
        no layout-blob read; write paths merge it into the stored provider JSON.
        Reads through this store's blob directory fd (never a second store
        open, which would mkdir/chmod the directory mid-transaction).
        """
        import re as _re
        import stat as _stat

        empty = {"layout_version": "", "code_detection": "unavailable"}
        if not isinstance(layout_key, str) or _re.fullmatch("[0-9a-f]{64}", layout_key) is None:
            return None, "layout_unreadable", dict(empty)
        blob_dir = Path(self.db.execute("PRAGMA database_list").fetchone()[2]).parent / "blobs"
        try:
            dir_fd = os.open(blob_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return None, "layout_unreadable", dict(empty)
        try:
            fd = os.open(layout_key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError:
            os.close(dir_fd)
            return None, "layout_unreadable", dict(empty)
        try:
            if not _stat.S_ISREG(os.fstat(fd).st_mode):
                return None, "layout_unreadable", dict(empty)
            os.set_blocking(fd, True)
            raw = b""
            while len(raw) <= 2 * 1024 * 1024:
                data = os.read(fd, min(65536, 2 * 1024 * 1024 + 1 - len(raw)))
                if not data:
                    break
                raw += data
            if len(raw) > 2 * 1024 * 1024:
                return None, "layout_too_large", dict(empty)
            wrapped = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "layout_unreadable", dict(empty)
        finally:
            os.close(fd)
            os.close(dir_fd)
        if not isinstance(wrapped, dict):
            return None, "layout_unreadable", dict(empty)
        layout = wrapped.get("layout", wrapped)
        if layout is None:
            if wrapped.get("state") == "missing":
                return None, None, dict(empty)
            return None, "layout_unreadable", dict(empty)
        if not isinstance(layout, dict):
            return None, "layout_unreadable", dict(empty)
        pages = layout.get("pdf_info", [])
        if not isinstance(pages, list):
            return None, "layout_unreadable", dict(empty)
        indices = {p.get("page_idx", i) for i, p in enumerate(pages) if isinstance(p, dict)}
        meta = {"layout_version": str(layout.get("layout_version") or ""),
                "code_detection": str(layout.get("code_detection") or "unavailable")}
        return len(indices), None, meta

    def publish_parse(self, task, *, layout_key, bundle_key, records, provider, degraded):
        with self.tx():
            row = self.task(task["id"])
            if row["status"] != "running" or row["generation"] != task["generation"]:
                return False
            self._index(task["version_id"], records)
            page_count, layout_error, meta = self._layout_facts(layout_key)
            stored_provider = {**(provider or {}), **meta}
            self.db.execute(
                "UPDATE versions SET "
                "state='ready',layout_key=?,bundle_key=?,provider=?,degraded=?,error=NULL,"
                "page_count=?,layout_error=? "
                "WHERE id=?",
                (
                    layout_key,
                    bundle_key,
                    json.dumps(stored_provider),
                    json.dumps(degraded),
                    page_count,
                    layout_error,
                    task["version_id"],
                ),
            )
            self.db.execute(
                "UPDATE tasks SET "
                "status='succeeded',result=?,lease_until=NULL,updated_at=? WHERE id=?",
                (json.dumps({"evidence_count": len(records)}), time.time(), task["id"]),
            )
            self.event("task.succeeded", task["id"], {"evidence_count": len(records)})
            return True

    def mark_layout_error(self, version_id, code):
        """Record a later-proven corrupt layout without touching parse state.

        Called when a stored layout blob later proves unreadable on a detail
        path (invariant 2: degradation visible). Proven corruption overwrites
        the cached page count: a stale count would project as healthy pages
        while hiding the outage. An already-recorded error keeps its code;
        repeated corruption reports must not flip the first diagnosis.
        """
        if not isinstance(code, str) or not code or len(code) > 64:
            raise ApplicationError("invalid_code", "layout error code must be 1..64 characters")
        with self.tx():
            self.version(version_id)
            self.db.execute(
                "UPDATE versions SET page_count=NULL,layout_error=? WHERE id=? "
                "AND layout_error IS NULL",
                (code, version_id),
            )

    def evidence(self, evidence_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if row is None:
                raise ApplicationError("not_found", "evidence not found in this workspace")
            return {
                "id": row["id"],
                "version_id": row["version_id"],
                "excerpt": row["excerpt"],
                "evidence": json.loads(row["envelope"]),
            }

    def keyword_search(self, query, version_ids, limit):
        if self.meta("tokenizer") != tokenizer_backend():
            raise ApplicationError(
                "index_incompatible", "tokenizer changed; rebuild this local index"
            )
        query_tokens = list(dict.fromkeys(tokens(query)))[:64]
        if not query_tokens or not version_ids:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in query_tokens)
        placeholders = ",".join("?" for _ in version_ids)
        with self.lock:
            rows = self.db.execute(
                f"""SELECT e.*,bm25(evidence_fts) AS rank,v.resource_id,v.parse_revision
                FROM evidence_fts JOIN evidence e ON e.id=evidence_fts.evidence_id
                JOIN versions v ON v.id=e.version_id WHERE evidence_fts MATCH ?
                AND e.version_id IN ({placeholders}) ORDER BY rank,e.id LIMIT ?""",
                (match, *version_ids, limit),
            ).fetchall()
            hits = []
            for row in rows:
                c = json.loads(row["chunk"])
                hits.append(
                    {
                        **c,
                        "chunk_id": row["id"],
                        "evidence_id": row["id"],
                        "document_id": row["resource_id"],
                        "version_id": row["version_id"],
                        "parse_job_id": row["parse_revision"],
                        "score": -row["rank"],
                        "similarity": None,
                    }
                )
            return hits

    def events(self, after=0):
        with self.lock:
            return [
                dict(r, payload=json.loads(r["payload"]))
                for r in self.db.execute(
                    "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 200", (after,)
                )
            ]

    def save_output(self, kind, result):
        identifier = new_id()
        with self.tx():
            self.db.execute(
                "INSERT INTO outputs VALUES(?,?,?,?)",
                (identifier, kind, json.dumps(result), time.time()),
            )
        return {"id": identifier, **result}

    def begin_generation(self, kind, request, operation_key, *, selection=None):
        if not operation_key or len(operation_key) > 128:
            raise ApplicationError("invalid_key", "operation key must contain 1–128 characters")
        request_digest = hashlib.sha256(json_bytes([kind, request])).hexdigest()
        with self.tx():
            previous = self.db.execute(
                "SELECT * FROM tasks WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if previous:
                if previous["request_digest"] != request_digest:
                    raise ApplicationError(
                        "idempotency_conflict", "same key belongs to a different generation"
                    )
                return record(previous), False
            identifier, now = new_id(), time.time()
            self.db.execute(
                """INSERT INTO tasks(id,kind,operation_key,request_digest,status,generation,
                attempts,lease_until,created_at,updated_at) VALUES(?,?,?,?,'running',1,1,?,?,?)""",
                (identifier, kind, operation_key, request_digest, now + 45, now, now),
            )
            self.event("execution.selected", identifier, {**request, "endpoint": (selection or {}).get("endpoint"), "provider_selection": selection})
            return self.task(identifier), True

    def finish_generation(self, task, result, *, publish=None, replace_result=False):
        with self.tx():
            current = self.task(task["id"])
            if current["status"] != "running" or current["generation"] != task["generation"]:
                raise ApplicationError(
                    "execution_cancelled", "execution was cancelled or superseded"
                )
            if publish is not None:
                published = publish(task, result)
                result = published if replace_result else {**result, **published}
            identifier = new_id()
            output = {**result, "id": identifier, "task_id": task["id"]}
            if len(json_bytes(output)) > 4 * 1024 * 1024:
                raise ApplicationError("output_too_large", "completed output exceeds the 4 MiB transport budget")
            self.db.execute(
                "INSERT INTO outputs VALUES(?,?,?,?)",
                (identifier, task["kind"], json.dumps(output), time.time()),
            )
            self.db.execute(
                "UPDATE tasks SET "
                "status='succeeded',result=?,lease_until=NULL,updated_at=? WHERE id=?",
                (json.dumps({"output_id": identifier}), time.time(), task["id"]),
            )
            self.event("task.succeeded", task["id"], {"output_id": identifier})
            return output

    def generation_result(self, task):
        if task["status"] != "succeeded":
            raise ApplicationError(
                "task_in_progress" if task["status"] == "running" else "execution_failed",
                "inspect task " + task["id"] + " before explicitly retrying",
            )
        identifier = task["result"]["output_id"]
        with self.lock:
            return json.loads(
                self.db.execute("SELECT body FROM outputs WHERE id=?", (identifier,)).fetchone()[0]
            )

    def _live_blob_keys(self):
        """Keys pinned by versions or staged upload parts. Caller holds the lock."""
        live = set()
        for row in self.db.execute(
            "SELECT blob_key,layout_key,bundle_key FROM versions"
        ).fetchall():
            for key in (row["blob_key"], row["layout_key"], row["bundle_key"]):
                if isinstance(key, str) and re.fullmatch("[0-9a-f]{64}", key):
                    live.add(key)
        for row in self.db.execute("SELECT parts_json FROM upload_sessions").fetchall():
            raw = row["parts_json"] or "{}"
            if not isinstance(raw, str) or len(raw) > 1024 * 1024:
                raise ApplicationError(
                    "upload_incomplete", "an upload session payload is unreadable; sweep stops")
            try:
                parts = json.loads(raw)
            except Exception as exc:
                raise ApplicationError(
                    "upload_incomplete", "an upload session payload is unreadable; sweep stops") from exc
            if not isinstance(parts, dict):
                raise ApplicationError(
                    "upload_incomplete", "an upload session payload is unreadable; sweep stops")
            for entry in parts.values():
                key = entry.get("blob_key") if isinstance(entry, dict) else None
                if isinstance(key, str) and re.fullmatch("[0-9a-f]{64}", key):
                    live.add(key)
        return live

    def sweep_unreferenced_blobs(self, blobs, *, grace_seconds=24 * 3600,
                                 _after_snapshot=None):
        """Mark-sweep unreferenced content-addressed blobs off the blob dir.

        Live keys are versions(blob_key, layout_key, bundle_key) plus
        upload_sessions.parts_json blob_keys. An unreadable or oversize
        session payload fails the sweep closed instead of deleting out from
        under a staged part. Only ``[0-9a-f]{64}`` files that are
        unreferenced AND older (mtime) than the grace period are unlinked;
        the grace window protects the crash gap between put_stream and
        store_upload_part. Each unlink is claimed first: the key is
        re-checked as still unreferenced under the store lock (iron rule 6),
        so a version committed after the snapshot keeps its blob even when
        put_stream hard-linked the bytes without touching mtime. Unlink goes
        through the pinned directory fd, never follows symlinks, and never
        touches non-hex names (including ``.pending-*``). Delivery partials
        are out of scope: they live in ``<workspace>/delivery-partials/``
        (see remote_compute.partial_path), not in the blob store.
        """
        if not isinstance(grace_seconds, (int, float)) or grace_seconds < 0:
            raise ApplicationError("invalid_window", "grace period must be non-negative")
        with self.lock:
            live = self._live_blob_keys()
        if _after_snapshot is not None:
            _after_snapshot()
        try:
            names = os.listdir(blobs.fd)
        except OSError as exc:
            raise ApplicationError("blob_unreadable", "blob directory is unreadable") from exc
        scanned, removed, freed = len(names), 0, 0
        for name in names:
            if not isinstance(name, str) or re.fullmatch("[0-9a-f]{64}", name) is None:
                continue
            if name in live:
                continue
            try:
                info = os.stat(name, dir_fd=blobs.fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if time.time() - info.st_mtime < grace_seconds:
                continue
            with self.lock:
                if name in self._live_blob_keys():
                    continue
                try:
                    os.unlink(name, dir_fd=blobs.fd)
                except OSError:
                    continue
            removed += 1
            freed += info.st_size
        try:
            os.fsync(blobs.fd)
        except OSError:
            pass
        return {"scanned": scanned, "live": len(live), "removed": removed, "bytes": freed}
