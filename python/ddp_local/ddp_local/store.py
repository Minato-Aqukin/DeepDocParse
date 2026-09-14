"""Local SQLite owns only workspace resources/tasks/evidence, never site accounts."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from ddp_core.application.ports import ApplicationError
from ddp_core.bundle import json_bytes
from ddp_core.tokenize import backend as tokenizer_backend
from ddp_core.tokenize import tokens

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
        self.db = sqlite3.connect(
            database, timeout=10, isolation_level=None, check_same_thread=False
        )
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
            elif version not in {1, 2}:
                raise ApplicationError(
                    "workspace_schema_unsupported", "unsupported local database version"
                )
        from ddp_local.wiki_store import migrate
        migrate(self)
        database.chmod(0o600)
        with self.tx():
            for key, value in (
                ("workspace_id", new_id()),
                ("environment_id", "local-" + new_id()),
                ("tokenizer", tokenizer_backend()),
            ):
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
        self.workspace_id = self.meta("workspace_id")
        self.environment_id = self.meta("environment_id")

    def close(self):
        self.db.close()

    def meta(self, key):
        with self.lock:
            return self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
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
                """SELECT * FROM tasks WHERE kind='parse' AND
                (status='queued' OR (status='running' AND lease_until<?))
                ORDER BY created_at LIMIT 1""",
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
