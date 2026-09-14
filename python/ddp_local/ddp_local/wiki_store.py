"""SQLite adapter for immutable Wiki revisions, source retention and model attempts."""

import copy
import base64
import math
import re
import hashlib
import json
import time
import uuid

from ddp_core.application.ports import ApplicationError
from ddp_core.application.wiki import WIKI_PROTOCOL, preserve_human_pages, validate_original_evidence
from ddp_core.bundle import json_bytes

SCHEMA = """
CREATE TABLE wikis(id TEXT PRIMARY KEY,title TEXT NOT NULL,current_revision_id TEXT,
 created_at REAL NOT NULL);
CREATE TABLE wiki_revisions(id TEXT PRIMARY KEY,wiki_id TEXT NOT NULL REFERENCES wikis(id),
 base_revision_id TEXT REFERENCES wiki_revisions(id),task_id TEXT NOT NULL REFERENCES tasks(id),
 body TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE wiki_dependencies(revision_id TEXT NOT NULL REFERENCES wiki_revisions(id),
 page_key TEXT NOT NULL,evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
 local_version_id TEXT NOT NULL REFERENCES versions(id) ON DELETE RESTRICT,frozen TEXT NOT NULL,
 PRIMARY KEY(revision_id,page_key,evidence_id));
CREATE TABLE wiki_human_edits(revision_id TEXT NOT NULL REFERENCES wiki_revisions(id),
 page_key TEXT NOT NULL,actor_id TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL);
CREATE TABLE wiki_attempts(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
 stage TEXT NOT NULL,protocol TEXT NOT NULL,request TEXT NOT NULL,allowance INTEGER NOT NULL,
 output TEXT,provider TEXT,error TEXT,created_at REAL NOT NULL,finished_at REAL);
CREATE TRIGGER wiki_revision_no_update BEFORE UPDATE ON wiki_revisions
 BEGIN SELECT RAISE(ABORT,'Wiki revisions are immutable'); END;
CREATE TRIGGER wiki_revision_no_delete BEFORE DELETE ON wiki_revisions
 BEGIN SELECT RAISE(ABORT,'Wiki revisions are immutable'); END;
CREATE INDEX wiki_revision_order ON wiki_revisions(wiki_id,created_at,id);
CREATE INDEX wiki_dependency_version ON wiki_dependencies(local_version_id);
PRAGMA user_version=2;
"""


def migrate(store):
    with store.tx():
        if store.db.execute("PRAGMA user_version").fetchone()[0] == 1:
            # complete_statement respects the semicolons in trigger bodies.
            statement = ""
            import sqlite3
            for line in SCHEMA.splitlines():
                statement += line + "\n"
                if sqlite3.complete_statement(statement):
                    store.db.execute(statement)
                    statement = ""


class LocalWikiStore:
    def __init__(self, store):
        self.store = store

    def freeze(self, sources, limits):
        if not isinstance(sources, list) or not 1 <= len(sources) <= 50:
            raise ApplicationError("wiki_source_invalid", "select 1..50 fixed local source versions")
        frozen, seen = [], set()
        with self.store.lock:
            for source in sources:
                version_id = source["source_version_id"]
                self.store.authorize_versions([version_id])
                version = self.store.version(version_id)
                if version["resource_id"] != source["resource_id"] or version_id in seen:
                    raise ApplicationError("wiki_source_invalid", "source ownership or unique version binding is invalid")
                seen.add(version_id)
                rows = self.store.db.execute("SELECT id FROM evidence WHERE version_id=? ORDER BY seq,id LIMIT ?",
                                             (version_id, limits["max_evidence"] + 1)).fetchall()
                if not rows:
                    raise ApplicationError("wiki_source_unavailable", "source version has no original evidence")
                frozen.extend({**self.store.evidence(row["id"]), "local_binding": {
                    key: version[key] for key in ("id", "resource_id", "source_digest", "parse_revision")}}
                    for row in rows)
        validate_original_evidence(frozen, limits)
        return frozen

    def check_frozen(self, frozen):
        for item in frozen:
            self.store.authorize_versions([item["version_id"]])
            current = self.store.evidence(item["id"])
            binding = item.get("local_binding")
            version = self.store.version(item["version_id"])
            if binding and any(version[key] != value for key, value in binding.items()):
                raise ApplicationError("wiki_source_unavailable", "selected source version changed during generation")
            if json_bytes(current) != json_bytes({k: v for k, v in item.items() if k != "local_binding"}):
                raise ApplicationError("wiki_source_unavailable", "a frozen original evidence binding changed")

    def get(self, wiki_id, revision_id=None):
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
            if row is None:
                raise ApplicationError("not_found", "Wiki not found in this workspace")
            selected = revision_id or row["current_revision_id"]
            revision = self.store.db.execute("SELECT * FROM wiki_revisions WHERE id=? AND wiki_id=?",
                                             (selected, wiki_id)).fetchone()
            if revision is None:
                raise ApplicationError("not_found", "Wiki revision not found in this workspace")
            if len(revision["body"].encode()) > 2 * 1024 * 1024:
                raise ApplicationError("wiki_response_too_large", "Wiki revision exceeds the 2 MiB read budget")
            body = json.loads(revision["body"])
            stale = {}
            for dep in body["dependency_manifest"]:
                reason = None
                try:
                    current = self.store.evidence(dep["evidence_id"])
                    version = self.store.version(dep["local_version_id"])
                    if version["state"] != "ready":
                        reason = "source_unavailable"
                    elif json_bytes(current["evidence"]) != json_bytes(dep["original"]):
                        reason = "source_binding_changed"
                    elif hashlib.sha256(current["excerpt"].encode()).hexdigest() != dep["local_excerpt_sha256"]:
                        reason = "source_content_changed"
                    elif version["parse_revision"] != dep["local_parse_revision"]:
                        reason = "parse_revision_changed"
                except ApplicationError:
                    reason = "source_unavailable"
                if reason:
                    stale.setdefault(dep["page_key"], []).append(reason)
            body.update(stale=bool(stale), stale_reasons={k: sorted(set(v)) for k, v in stale.items()})
            for page in body["pages"]:
                page["stale"] = page["page_key"] in stale
            return {"wiki": {"id": row["id"], "title": body["title"],
                             "current_revision_id": row["current_revision_id"], "published_revision_id": None},
                    "revision": body, "task_id": revision["task_id"]}

    def _window(self, *, wiki_id=None, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApplicationError("invalid_limit", "Wiki page window must be within 1..100")
        scope = "revisions:" + wiki_id if wiki_id else "wikis"
        anchor = after = None
        if cursor:
            try:
                if len(cursor) > 1024:
                    raise ValueError()
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode() + b"=" * (-len(cursor) % 4)))
                if decoded["workspace"] != self.store.workspace_id or decoded["scope"] != scope:
                    raise ValueError()
                anchor, after = decoded["anchor"], decoded["after"]
                for pair in (anchor, after):
                    if (not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) not in (int, float)
                            or not math.isfinite(pair[0]) or not isinstance(pair[1], str)
                            or not re.fullmatch(r"[a-f0-9]{32}", pair[1])):
                        raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ApplicationError("cursor_expired", "Wiki cursor does not belong to this list or workspace") from exc
        table, predicate, values = ("wiki_revisions", "wiki_id=?", [wiki_id]) if wiki_id else ("wikis", "1=1", [])
        with self.store.lock:
            self.store.db.execute("BEGIN")
            try:
                if anchor is None:
                    top = self.store.db.execute(f"SELECT created_at,id FROM {table} WHERE {predicate} ORDER BY created_at DESC,id DESC LIMIT 1", values).fetchone()
                    if top is None:
                        self.store.db.execute("COMMIT")
                        return {"items": [], "visible_total": 0, "has_more": False, "next_cursor": None}
                    anchor = [top["created_at"], top["id"]]
                window = predicate + " AND (created_at<? OR (created_at=? AND id<=?))"
                parameters = [*values, anchor[0], anchor[0], anchor[1]]
                total = self.store.db.execute(f"SELECT count(*) FROM {table} WHERE {window}", parameters).fetchone()[0]
                if after is not None:
                    window += " AND (created_at<? OR (created_at=? AND id<?))"
                    parameters.extend([after[0], after[0], after[1]])
                rows = self.store.db.execute(f"SELECT id,created_at FROM {table} WHERE {window} ORDER BY created_at DESC,id DESC LIMIT ?", [*parameters, limit + 1]).fetchall()
                has_more, selected, items = len(rows) > limit, rows[:limit], []
                for row in selected:
                    if wiki_id:
                        revision = self.store.db.execute("SELECT id,wiki_id,base_revision_id,task_id,created_at FROM wiki_revisions WHERE id=?", (row["id"],)).fetchone()
                        items.append(dict(revision))
                    else:
                        item = self.store.db.execute("""SELECT w.id,w.title,w.current_revision_id,
                            r.created_at,json_extract(r.body,'$.base_revision_id') AS base_revision_id,
                            json_extract(r.body,'$.kind') AS kind,
                            json_array_length(r.body,'$.pages') AS page_count,
                            json_array_length(r.body,'$.relations') AS relation_count
                            FROM wikis w JOIN wiki_revisions r ON r.id=w.current_revision_id WHERE w.id=?""", (row["id"],)).fetchone()
                        items.append({"wiki": {"id": item["id"], "title": item["title"],
                            "current_revision_id": item["current_revision_id"], "published_revision_id": None},
                            "revision": {"id": item["current_revision_id"], "wiki_id": item["id"],
                            "title": item["title"], "base_revision_id": item["base_revision_id"], "kind": item["kind"],
                            "created_at": item["created_at"], "page_count": item["page_count"],
                            "relation_count": item["relation_count"], "semantic_review": "needs_review", "source_type": "generated"}})
                next_cursor = None
                if has_more:
                    last = selected[-1]
                    next_cursor = base64.urlsafe_b64encode(json_bytes({"workspace": self.store.workspace_id,
                        "scope": scope, "anchor": anchor, "after": [last["created_at"], last["id"]]})).decode().rstrip("=")
                self.store.db.execute("COMMIT")
                return {"items": items, "visible_total": total, "has_more": has_more, "next_cursor": next_cursor}
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise

    def list(self, *, limit=50, cursor=None):
        return self._window(limit=limit, cursor=cursor)

    def revisions(self, wiki_id, *, limit=50, cursor=None):
        self.get(wiki_id)
        return self._window(wiki_id=wiki_id, limit=limit, cursor=cursor)

    def check_base(self, wiki_id, base_revision_id):
        prior = self.get(wiki_id)
        if prior["wiki"]["current_revision_id"] != base_revision_id:
            raise ApplicationError("revision_conflict", "Wiki changed; reload before writing")
        return prior["revision"]

    def publish(self, task, *, title, result, frozen, wiki_id=None, base_revision_id=None,
                kind="generated", edit=None):
        """Called only inside finish_generation's transaction, before its success event."""
        self.check_frozen(frozen)
        now, revision_id = time.time(), uuid.uuid4().hex
        prior = self.check_base(wiki_id, base_revision_id) if wiki_id else None
        if prior:
            pages, conflicts = preserve_human_pages(result["pages"], prior["pages"], result["limits"]["max_pages"])
        else:
            wiki_id = uuid.uuid4().hex
            self.store.db.execute("INSERT INTO wikis VALUES(?,?,NULL,?)", (wiki_id, title, now))
            pages, conflicts = copy.deepcopy(result["pages"]), []
        # A human edit already explicitly replaced the requested page paragraphs.
        if kind == "human_edit":
            pages, conflicts = copy.deepcopy(result["pages"]), copy.deepcopy(result.get("merge_conflicts", []))
        deps = []
        for page in pages:
            for item in frozen:
                version = self.store.version(item["version_id"])
                deps.append({"page_key": page["page_key"], "evidence_id": item["id"],
                             "local_version_id": item["version_id"], "local_resource_id": version["resource_id"],
                             "local_parse_revision": version["parse_revision"],
                             "local_excerpt_sha256": hashlib.sha256(item["excerpt"].encode()).hexdigest(),
                             "original": item["evidence"]})
        if prior:
            retained = {p["page_key"] for p in pages if p.get("human_paragraphs")}
            deps.extend(copy.deepcopy(d) for d in prior["dependency_manifest"] if d["page_key"] in retained)
        unique = {(d["page_key"], d["evidence_id"]): d for d in deps}
        deps = list(unique.values())
        body = {"id": revision_id, "wiki_id": wiki_id, "base_revision_id": base_revision_id,
                "kind": kind, "title": title, "created_by": "workspace:" + self.store.workspace_id,
                "created_at": now, "provider": result.get("provider", {}), "limits": result["limits"],
                "pages": pages, "relations": result.get("relations", []), "dependency_manifest": deps,
                "merge_conflicts": conflicts, "semantic_review": "needs_review", "source_type": "generated",
                "protocol": result.get("protocol", "legacy-cited-wiki/1"),
                "decoder_revision": result.get("decoder_revision", "legacy-citations/1")}
        if len(json_bytes(body)) > 2 * 1024 * 1024:
            raise ApplicationError("wiki_budget_exceeded", "Wiki revision exceeds its 2 MiB storage/read budget")
        self.store.db.execute("INSERT INTO wiki_revisions VALUES(?,?,?,?,?,?)",
            (revision_id, wiki_id, base_revision_id, task["id"], json_bytes(body).decode(), now))
        count = self.store.db.execute("UPDATE wikis SET current_revision_id=?,title=? WHERE id=? AND current_revision_id IS ?",
                                     (revision_id, title, wiki_id, base_revision_id)).rowcount
        if count != 1:
            raise ApplicationError("revision_conflict", "Wiki changed before this revision could commit")
        for dep in deps:
            self.store.db.execute("INSERT INTO wiki_dependencies VALUES(?,?,?,?,?)",
                (revision_id, dep["page_key"], dep["evidence_id"], dep["local_version_id"], json.dumps(dep)))
        if edit:
            self.store.db.execute("INSERT INTO wiki_human_edits VALUES(?,?,?,?,?)",
                (revision_id, edit["page_key"], body["created_by"], json.dumps(edit["before"]), json.dumps(edit["after"])))
        self.store.event("wiki.revision_committed", task["id"], {"wiki_id": wiki_id, "revision_id": revision_id})
        return self.get(wiki_id, revision_id)

    def attempt(self, task, stage, messages, allowance):
        identifier = uuid.uuid4().hex
        with self.store.tx():
            current = self.store.task(task["id"])
            if current["status"] != "running" or current["generation"] != task["generation"]:
                raise ApplicationError("execution_cancelled", "Wiki task was cancelled before model dispatch")
            self.store.db.execute("INSERT INTO wiki_attempts(id,task_id,stage,protocol,request,allowance,created_at) VALUES(?,?,?,?,?,?,?)",
                (identifier, task["id"], stage, WIKI_PROTOCOL, json.dumps(messages), allowance, time.time()))
            self.store.event("wiki.model_attempt", task["id"], {"attempt_id": identifier, "stage": stage})
        def finish(*, output=None, provider=None, error=None):
            with self.store.tx():
                self.store.db.execute("UPDATE wiki_attempts SET output=coalesce(?,output),provider=coalesce(?,provider),error=?,finished_at=? WHERE id=?",
                    (output, json.dumps(provider) if provider else None, error, time.time(), identifier))
        return finish

    def attempts(self, task_id):
        self.store.task(task_id)
        with self.store.lock:
            return [dict(row) for row in self.store.db.execute(
                "SELECT id,stage,protocol,allowance,error,created_at,finished_at FROM wiki_attempts WHERE task_id=? ORDER BY created_at,id", (task_id,))]
