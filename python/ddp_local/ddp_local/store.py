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
 degraded TEXT NOT NULL DEFAULT '[]', error TEXT, created_at REAL NOT NULL);
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
PRAGMA user_version=3;
"""


def new_id():
    return uuid.uuid4().hex


def record(row):
    value = dict(row)
    for name in ("provider", "degraded", "source_json", "result"):
        if value.get(name) is not None:
            value[name] = json.loads(value[name])
    return value


class LocalStore:
    def __init__(self, directory: Path):
        self.lock = RLock()
        database = directory / "workspace.sqlite3"
        if database.is_symlink():
            raise ApplicationError("unsafe_path", "workspace database cannot be a symlink")
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
        """
        with self.tx():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                statement = ""
                for line in CONTENT_TABLES.splitlines():
                    statement += line + "\n"
                    if sqlite3.complete_statement(statement):
                        self.db.execute(statement)
                        statement = ""

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
            self.db.execute("INSERT INTO resources VALUES(?,?,?)", (resource_id, filename, now))
            self.db.execute(
                """INSERT INTO versions(id,resource_id,filename,source_digest,size_bytes,
                blob_key,parse_revision,state,layout_key,bundle_key,source_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
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
            self.db.execute(
                """INSERT INTO versions(id,resource_id,filename,source_digest,size_bytes,
                blob_key,parse_revision,state,layout_key,bundle_key,source_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def resource_command(self, action, target, *, operation_key):
        """Resource mutation and its durable receipt commit in the same transaction."""
        operations = {"version.withdraw": self.withdraw_version, "version.delete": self.delete_version,
                      "resource.delete": self.delete_resource}
        if action not in operations:
            raise ApplicationError("unsupported_operation", "unsupported local resource operation")
        failure = None
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
        if failure is not None:
            raise failure
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

    def delete_version(self, version_id):
        """Physically remove one version that nothing retains.

        Refuses with ``source_in_use`` while any Wiki dependency manifest
        references the version, and with ``task_in_progress`` while a task is
        still queued/running. Content-addressed blob bytes are left on disk:
        they may be shared with other versions and are never trusted by name.
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
            return {"version_id": version_id, "resource_id": resource_id, "deleted": True}

    def delete_resource(self, resource_id):
        """Remove a whole logical resource once no Wiki or active task retains it."""
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
            return {
                "resource_id": resource_id,
                "deleted": True,
                "deleted_versions": sorted(version_ids),
            }
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
        if type(part_number) is not int or part_number < 1 or part_number > 10000:
            raise ApplicationError("invalid_part", "part_number must be within 1..10000")
        with self.tx():
            session = self.upload_session(session_id)
            if session["status"] not in {"uploading", "created"}:
                raise ApplicationError("upload_finalized", "this upload session already finalized")
            parts = json.loads(session["parts_json"])
            parts[str(part_number)] = {"blob_key": key, "size": size}
            self.db.execute(
                "UPDATE upload_sessions SET parts_json=?,status='uploading',updated_at=? WHERE id=?",
                (json.dumps(parts), time.time(), session_id),
            )
            return parts[str(part_number)]

    def finalize_upload_session(self, session_id, read_blob, write_bytes):
        """Assemble buffered parts, verify size, and create the resource/version.

        Runs synchronously: the local runtime has no object-storage round trip
        to poll, so ingest_status is ready (or rejected) on return. The parsed
        content still needs the background parse task; that is the parse task
        row, not the upload session. Part bytes live in the content-addressed
        blob store; the session only keeps their keys.
        """
        from ddp_local.blobs import MAX_INPUT

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
            chunks = []
            for number in sorted(int(value) for value in parts):
                chunks.append(read_blob(parts[str(number)]["blob_key"], MAX_INPUT))
            data = b"".join(chunks)
            if len(data) > MAX_INPUT:
                self.db.execute(
                    "UPDATE upload_sessions SET status='failed',error='input_too_large',"
                    "updated_at=? WHERE id=?",
                    (time.time(), session_id),
                )
                raise ApplicationError("input_too_large", "upload exceeds the 32 MiB local budget")
            if len(data) != session["declared_size"]:
                self.db.execute(
                    "UPDATE upload_sessions SET status='failed',error='size_mismatch',"
                    "updated_at=? WHERE id=?",
                    (time.time(), session_id),
                )
                raise ApplicationError(
                    "size_mismatch", "uploaded bytes differ from the declared size"
                )
            if not data.startswith(b"%PDF-"):
                self.db.execute(
                    "UPDATE upload_sessions SET status='failed',error='invalid_pdf',"
                    "updated_at=? WHERE id=?",
                    (time.time(), session_id),
                )
                raise ApplicationError("invalid_pdf", "local uploads accept PDF bytes only")
            if session["declared_sha256"] and (
                hashlib.sha256(data).hexdigest() != session["declared_sha256"]
            ):
                self.db.execute(
                    "UPDATE upload_sessions SET status='failed',error='digest_mismatch',"
                    "updated_at=? WHERE id=?",
                    (time.time(), session_id),
                )
                raise ApplicationError(
                    "digest_mismatch", "uploaded bytes differ from the declared digest"
                )
            self.db.execute(
                "UPDATE upload_sessions SET status='verifying',updated_at=? WHERE id=?",
                (time.time(), session_id),
            )
            blob_key, size = write_bytes(data)
            if session["target_resource_id"]:
                task = self.append_version(
                    resource_id=session["target_resource_id"], filename=session["filename"],
                    blob_key=blob_key, size=size,
                    operation_key="upload:" + session_id,
                    bundle_key=None, source=None, records=None, layout_key=None,
                )
                created_resource, created_version = session["target_resource_id"], task["version_id"]
            else:
                task = self.create_resource(
                    filename=session["filename"], blob_key=blob_key, size=size,
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


    def versions(self):
        with self.lock:
            return [
                record(r)
                for r in self.db.execute("SELECT * FROM versions ORDER BY created_at DESC")
            ]

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

    def publish_parse(self, task, *, layout_key, bundle_key, records, provider, degraded):
        with self.tx():
            row = self.task(task["id"])
            if row["status"] != "running" or row["generation"] != task["generation"]:
                return False
            self._index(task["version_id"], records)
            self.db.execute(
                "UPDATE versions SET "
                "state='ready',layout_key=?,bundle_key=?,provider=?,degraded=?,error=NULL "
                "WHERE id=?",
                (
                    layout_key,
                    bundle_key,
                    json.dumps(provider),
                    json.dumps(degraded),
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
