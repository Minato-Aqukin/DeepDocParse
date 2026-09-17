"""Private durable, identity-scoped consent ledger. No network or remote admission.

The only grant operation is explicit approval of a previously prepared digest.
Transport adapters must authorize the exact bytes immediately before dispatch;
this ledger records reservations conservatively even if the subsequent send fails.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from ddp_core.application.plans import (
    PAYLOADS, RETENTION, canonical_bytes, content_digest, digest, instant, reject,
    task_plan_digest, task_spec_digest, utc_instant, validate_scope, validate_plan, obj,
)


class ConsentStore:
    def __init__(self, directory: Path, *, local_node_id: str, source_policy_resolver=None, input_resolver=None, clock=time.time):
        self.local_node_id = local_node_id
        self.source_policy_resolver = source_policy_resolver
        self.input_resolver = input_resolver
        self.clock = clock
        self.lock = RLock()
        path = Path(directory) / "consents.sqlite3"
        for suffix in ("", "-journal", "-wal", "-shm"):
            if Path(str(path) + suffix).is_symlink():
                reject("unsafe_path", "consent database and sidecars cannot be symlinks")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.tx():
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                reject("workspace_schema_unsupported", "unsupported consent ledger schema")
            self.db.execute("""CREATE TABLE IF NOT EXISTS plans(
                owner TEXT NOT NULL, plan_id TEXT NOT NULL, scope_digest TEXT NOT NULL,
                scope_json TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
                consents_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
                PRIMARY KEY(owner, plan_id))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS commands(
                owner TEXT NOT NULL, command_key TEXT NOT NULL, request_digest TEXT NOT NULL,
                result_json TEXT NOT NULL, PRIMARY KEY(owner, command_key))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS dispatches(
                owner TEXT NOT NULL, plan_id TEXT NOT NULL, phase TEXT NOT NULL,
                request_key TEXT NOT NULL, request_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                generation_tokens INTEGER NOT NULL, discovery INTEGER NOT NULL,
                created_at REAL NOT NULL, PRIMARY KEY(owner, plan_id, phase, request_key))""")
            self.db.execute("CREATE TABLE IF NOT EXISTS workspace_policy(key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            self.db.execute("INSERT OR IGNORE INTO workspace_policy VALUES('local_only',0)")
            self.db.execute("PRAGMA user_version=1")

    def set_local_only(self, enabled: bool):
        """Trusted workspace setting; never derived from a model suggestion."""
        if type(enabled) is not bool:
            reject("invalid_plan", "local-only policy must be boolean")
        with self.tx():
            self.db.execute("UPDATE workspace_policy SET value=? WHERE key='local_only'", (int(enabled),))
            if enabled:
                # Returning online later cannot silently resurrect an approval.
                self.db.execute("UPDATE plans SET revoked=1")

    def _local_only(self):
        return bool(self.db.execute("SELECT value FROM workspace_policy WHERE key='local_only'").fetchone()[0])

    def _validate_inputs(self, scope):
        if scope["input_manifest"] and self.input_resolver is None:
            reject("input_changed", "fixed inputs require a trusted snapshot resolver")
        for item in scope["input_manifest"]:
            content = self.input_resolver(item["ref"])
            if content_digest(content) != item["digest"] or len(content) != item["size_bytes"]:
                reject("input_changed", "trusted input bytes differ from the proposed manifest")

    def close(self):
        self.db.close()

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

    @staticmethod
    def _owner(identity):
        if (not isinstance(identity, dict) or set(identity) != {"environment_id", "workspace_id", "subject"}
                or any(not isinstance(v, str) or not v or len(v) > 512 for v in identity.values())):
            reject("unauthorized", "a verified environment, workspace and subject are required")
        return canonical_bytes(identity).decode()

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            reject("invalid_key", "an idempotency key of 1–128 characters is required")

    def _existing_command(self, owner, key, request):
        self._key(key)
        row = self.db.execute("SELECT * FROM commands WHERE owner=? AND command_key=?", (owner, key)).fetchone()
        if row and row["request_digest"] != digest(request):
            reject("idempotency_conflict", "same identity and key refer to a different command")
        return json.loads(row["result_json"]) if row else None

    def _command(self, owner, key, request, result):
        self.db.execute("INSERT INTO commands VALUES(?,?,?,?)", (owner, key, digest(request), canonical_bytes(result).decode()))

    def _row(self, owner, plan_id):
        row = self.db.execute("SELECT * FROM plans WHERE owner=? AND plan_id=?", (owner, plan_id)).fetchone()
        if not row:
            reject("not_found", "plan does not exist in this identity")
        return row

    def _policies(self, scope):
        # The local user owns local data. Remote policy references are *never*
        # trusted just because the caller/model included a nonempty string.
        policies = {
            "local:" + self.local_node_id: {
                "source_node_id": self.local_node_id,
                "allowed_recipients": sorted({n for edge in scope["plan"]["data_edges"] for n in [edge["to_node_id"], *edge.get("relay_via", [])]}),
                "allowed_payload": sorted(PAYLOADS), "allowed_retention": sorted(RETENTION),
                "valid_until": scope["plan"]["valid_until"],
            }
        }
        if self.source_policy_resolver:
            policies.update(self.source_policy_resolver(scope))
        return policies

    @staticmethod
    def _scope_shape(scope):
        obj(scope, ("task_spec", "plan", "input_manifest", "payload_bindings", "output_locations", "retention", "exploration"), ("transport_bindings",))
        if "transport_bindings" in scope and (not isinstance(scope["transport_bindings"], list) or not 1 <= len(scope["transport_bindings"]) <= 100):
            reject("invalid_plan", "transport bindings must be a bounded list")
        if not isinstance(scope["plan"], dict) or not isinstance(scope["task_spec"], dict):
            reject("invalid_plan", "typed task and plan objects are required")
        if not isinstance(scope["task_spec"].get("consent_refs"), dict):
            reject("invalid_plan", "task consent references are required")
        if not isinstance(scope["plan"].get("data_edges"), list) or "valid_until" not in scope["plan"]:
            reject("invalid_plan", "plan edges and expiry are required")

    def _admit(self, owner, identity, scope):
        """Validate and persist one scope inside the caller's transaction."""
        plan, spec = scope["plan"], scope["task_spec"]
        if plan.get("planning_state") not in {"draft", "ready", "awaiting_approval"} or any(spec["consent_refs"].values()) or plan.get("execution_consent_ref"):
            reject("consent_required", "prepare cannot import grants or approved state")
        if spec.get("workspace_ref") != identity["workspace_id"]:
            reject("unauthorized", "task workspace differs from authenticated identity")
        plan["task_spec_digest"] = task_spec_digest(spec)
        plan["plan_digest"] = task_plan_digest(plan)
        validate_plan(plan, spec, local_node_id=self.local_node_id, now=self.clock())
        validate_scope(scope, local_node_id=self.local_node_id, now=self.clock(), source_policies=self._policies(scope))
        if self._local_only() and spec["execution_policy"]["mode"] != "local_only":
            reject("local_only", "workspace policy forbids preparing remote plans")
        self._validate_inputs(scope)
        checksum = digest(scope)
        existing = self.db.execute("SELECT * FROM plans WHERE owner=? AND plan_id=?", (owner, plan["plan_id"])).fetchone()
        if existing:
            if existing["scope_digest"] != checksum:
                reject("plan_changed", "plan identity is immutable; prepare a new revision identifier")
            return self._view(existing)
        self.db.execute("INSERT INTO plans(owner,plan_id,scope_digest,scope_json,created_at) VALUES(?,?,?,?,?)", (owner, plan["plan_id"], checksum, canonical_bytes(scope).decode(), self.clock()))
        return self._view(self._row(owner, plan["plan_id"]))

    def prepare(self, identity, scope, *, operation_key):
        """Prepare is side-effect free except local persistence; it cannot grant."""
        owner = self._owner(identity)
        # Copy before deriving digests, so later caller mutations cannot change a grant.
        scope = json.loads(canonical_bytes(scope))
        self._scope_shape(scope)
        request = {"action": "prepare", "scope": json.loads(canonical_bytes(scope))}
        with self.tx():
            previous = self._existing_command(owner, operation_key, request)
            if previous:
                return self._view(self._row(owner, previous["plan_id"]))
            result = self._admit(owner, identity, scope)
            self._command(owner, operation_key, request, {"plan_id": scope["plan"]["plan_id"]})
            return result

    def propose(self, identity, request, build, *, operation_key):
        """Persist a trusted template's scope for a typed request; cannot grant.

        The command digest covers the typed request, not the generated scope: the
        template draws a fresh plan ID and expiry, so a same-key replay must return
        the first plan instead of comparing two different generated scopes.
        """
        owner = self._owner(identity)
        request = json.loads(canonical_bytes(request))
        command = {"action": "propose", "request": request}
        with self.tx():
            previous = self._existing_command(owner, operation_key, command)
            if previous:
                return self._view(self._row(owner, previous["plan_id"]))
            scope = json.loads(canonical_bytes(build(request, self.clock())))
            self._scope_shape(scope)
            result = self._admit(owner, identity, scope)
            self._command(owner, operation_key, command, {"plan_id": scope["plan"]["plan_id"]})
            return result

    def list_plans(self, identity, *, limit=50):
        """Newest-first summaries for recovery views; never another owner's plans."""
        owner = self._owner(identity)
        if type(limit) is not int or not 1 <= limit <= 50:
            reject("invalid_plan", "plan list limit must be 1-50")
        with self.lock:
            rows = self.db.execute("SELECT * FROM plans WHERE owner=? ORDER BY created_at DESC, plan_id LIMIT ?", (owner, limit)).fetchall()
            total = self.db.execute("SELECT COUNT(*) FROM plans WHERE owner=?", (owner,)).fetchone()[0]
        items = []
        for row in rows:
            view = self._view(row)
            scope = view["scope"]
            items.append({"plan_id": view["plan_id"], "scope_digest": view["scope_digest"],
                          "planning_state": view["planning_state"], "revoked": view["revoked"],
                          "approved_phases": sorted(view["consents"]), "created_at": row["created_at"],
                          "valid_until": scope["plan"]["valid_until"],
                          "recipients": sorted({item["recipient_node_id"] for item in scope["payload_bindings"]}),
                          "query": scope["task_spec"].get("query") if isinstance(scope["task_spec"].get("query"), str) else None})
        return {"items": items, "visible_total": total}

    def command_receipt(self, identity, operation_key):
        """Admitted prepare/propose/approve/revoke key -> current plan view, else None."""
        owner = self._owner(identity)
        self._key(operation_key)
        with self.lock:
            row = self.db.execute("SELECT result_json FROM commands WHERE owner=? AND command_key=?", (owner, operation_key)).fetchone()
            if not row:
                return None
            return self._view(self._row(owner, json.loads(row["result_json"])["plan_id"]))

    def _view(self, row):
        scope = json.loads(row["scope_json"])
        consents = json.loads(row["consents_json"])
        expired = min(instant(scope["plan"]["valid_until"]), instant(scope["plan"]["budget"]["deadline"]), instant(scope["exploration"]["valid_until"])) <= self.clock()
        state = "invalidated" if row["revoked"] or expired else "approved" if "execution" in consents else "exploring" if "exploration" in consents else "ready"
        return {"plan_id": row["plan_id"], "scope_digest": row["scope_digest"], "scope": scope,
                "planning_state": state, "consents": consents, "revoked": bool(row["revoked"]),
                "admission_state": "not_submitted"}

    def get(self, identity, plan_id):
        with self.lock:
            return self._view(self._row(self._owner(identity), plan_id))

    def approve(self, identity, plan_id, *, phase, confirmed_scope_digest, user_confirmed, operation_key):
        if user_confirmed is not True or phase not in {"exploration", "execution"}:
            reject("consent_required", "explicit user approval and a known phase are required")
        owner = self._owner(identity)
        request = {"action": "approve", "plan_id": plan_id, "phase": phase, "scope_digest": confirmed_scope_digest, "user_confirmed": True}
        with self.tx():
            previous = self._existing_command(owner, operation_key, request)
            row = self._row(owner, plan_id)
            scope = self._active(row, confirmed_scope_digest)
            if previous:
                return self._view(row)
            consents = json.loads(row["consents_json"])
            if phase not in consents:
                now = self.clock()
                common = {"consent_id": uuid.uuid4().hex, "granted_by": identity["subject"], "granted_at": utc_instant(now)}
                if phase == "exploration":
                    consents[phase] = {"schema": "ddp-task-probe/1#ExplorationConsent", **common, **scope["exploration"]}
                else:
                    plan = scope["plan"]
                    recipients = {plan["root_coordinator_node_id"], plan["final_result_writer"]}
                    recipients.update(step["executor_node_id"] for step in plan["steps"])
                    recipients.update(n for edge in plan["data_edges"] for n in [edge["to_node_id"], *edge.get("relay_via", [])])
                    consents[phase] = {"schema": "ddp-plan-admission/1#ExecutionConsent", **common,
                        "plan_digest": plan["plan_digest"], "valid_until": plan["valid_until"],
                        "allowed_recipients": sorted(recipients), "allowed_edges": [edge["edge_id"] for edge in plan["data_edges"]],
                        "output_locations": scope["output_locations"], "retention": scope["retention"]}
                self.db.execute("UPDATE plans SET consents_json=? WHERE owner=? AND plan_id=?", (canonical_bytes(consents).decode(), owner, plan_id))
            self._command(owner, operation_key, request, {"plan_id": plan_id})
            return self._view(self._row(owner, plan_id))

    def _active(self, row, scope_digest):
        if row["revoked"]:
            reject("consent_revoked", "approval was revoked; prepare and approve a new plan")
        if row["scope_digest"] != scope_digest:
            reject("plan_changed", "approval must name the exact reviewed scope")
        scope = json.loads(row["scope_json"])
        if self._local_only() and scope["task_spec"]["execution_policy"]["mode"] != "local_only":
            reject("local_only", "current workspace policy forbids remote plans")
        validate_scope(scope, local_node_id=self.local_node_id, now=self.clock(), source_policies=self._policies(scope))
        self._validate_inputs(scope)
        return scope

    def revoke(self, identity, plan_id, *, operation_key):
        owner = self._owner(identity)
        request = {"action": "revoke", "plan_id": plan_id}
        with self.tx():
            previous = self._existing_command(owner, operation_key, request)
            self._row(owner, plan_id)
            if not previous:
                self.db.execute("UPDATE plans SET revoked=1 WHERE owner=? AND plan_id=?", (owner, plan_id))
                self._command(owner, operation_key, request, {"plan_id": plan_id})
            return self._view(self._row(owner, plan_id))

    def authorize_dispatch(self, identity, plan_id, *, phase, payload_id, recipient_node_id,
                           payload: bytes, operation_key, confirmed_scope_digest,
                           current_spec, current_plan, input_bytes, output_location,
                           retention, generation_tokens=None, discovery=False, local_only=False,
                           current_transport=None):
        """Revalidate current bytes/policy/identity and reserve each actual send.

Return only the verified bytes. Caller must disable redirects and bind recipient
identity to its verified endpoint. A retry needs a fresh operation key: reusing
one already-reserved dispatch fails closed, since a lost response may have sent.
Remote admission retries/reconciliation are a separate executor responsibility.
"""
        owner = self._owner(identity)
        self._key(operation_key)
        if (generation_tokens is not None and (type(generation_tokens) is not int or generation_tokens < 0)) or type(discovery) is not bool:
            reject("budget_exceeded", "invalid dispatch resource reservation")
        if local_only and recipient_node_id != self.local_node_id:
            reject("local_only", "current workspace policy forbids remote dispatch")
        actual_digest = content_digest(payload)
        request = {"payload_id": payload_id, "recipient": recipient_node_id, "digest": actual_digest,
                   "scope_digest": confirmed_scope_digest, "output_location": output_location,
                   "retention": retention, "tokens": generation_tokens, "discovery": discovery,
                   "transport": current_transport}
        with self.tx():
            row = self._row(owner, plan_id)
            scope = self._active(row, confirmed_scope_digest)
            consent = json.loads(row["consents_json"]).get(phase)
            if not consent:
                reject("consent_required", "this dispatch phase has no explicit user approval")
            if instant(consent["valid_until"]) <= self.clock():
                reject("consent_expired", "approval has expired")
            validate_plan(current_plan, current_spec, local_node_id=self.local_node_id, now=self.clock())
            if task_spec_digest(current_spec) != task_spec_digest(scope["task_spec"]) or task_plan_digest(current_plan) != task_plan_digest(scope["plan"]):
                reject("plan_changed", "current task or plan differs from approved content")
            if output_location not in scope["output_locations"] or retention != scope["retention"]:
                reject("policy_denied", "output location or retention exceeds approval")
            expected_inputs = {item["ref"]: item for item in scope["input_manifest"]}
            if not isinstance(input_bytes, dict) or set(input_bytes) != set(expected_inputs):
                reject("input_changed", "current fixed input set differs from approved manifest")
            for ref, content in input_bytes.items():
                item = expected_inputs[ref]
                if content_digest(content) != item["digest"] or len(content) != item["size_bytes"]:
                    reject("input_changed", "current input bytes differ from approved snapshot")
            binding = next((item for item in scope["payload_bindings"] if item["payload_id"] == payload_id), None)
            if not binding or binding["phase"] != phase or binding["recipient_node_id"] != recipient_node_id:
                reject("policy_denied", "payload or recipient is outside the reviewed scope")
            if binding.get("transport_ref"):
                expected_transport = next((item for item in scope.get("transport_bindings", [])
                                           if item["transport_ref"] == binding["transport_ref"]), None)
                if not expected_transport or current_transport != expected_transport:
                    reject("policy_denied", "current endpoint, workspace or principal differs from approval")
            elif current_transport is not None:
                reject("policy_denied", "this plan has no reviewed transport binding")
            if binding["digest"] != actual_digest or binding["size_bytes"] != len(payload):
                reject("input_changed", "outgoing bytes differ from the approved payload")
            fixed_tokens = binding.get("generation_tokens", 0)
            if generation_tokens is not None and generation_tokens != fixed_tokens:
                reject("budget_exceeded", "caller cannot alter the approved token reservation")
            generation_tokens = fixed_tokens
            request["tokens"] = fixed_tokens
            previous = self.db.execute("SELECT * FROM dispatches WHERE owner=? AND plan_id=? AND phase=? AND request_key=?", (owner, plan_id, phase, operation_key)).fetchone()
            if previous:
                code = "idempotency_conflict" if previous["request_digest"] != digest(request) else "dispatch_already_reserved"
                reject(code, "dispatch has already been reserved; reconcile before retrying")
            total = self.db.execute("SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b, COALESCE(SUM(generation_tokens),0) t FROM dispatches WHERE owner=? AND plan_id=?", (owner, plan_id)).fetchone()
            budget = scope["plan"]["budget"]
            if total["n"] + 1 > budget["max_requests"] or total["b"] + len(payload) > budget["max_bytes"] or total["t"] + generation_tokens > budget.get("max_generation_tokens", 0):
                reject("budget_exceeded", "dispatch exceeds the root request budget")
            if phase == "exploration":
                used = self.db.execute("SELECT COALESCE(SUM(1-discovery),0) p, COALESCE(SUM(discovery),0) d, COALESCE(SUM(size_bytes),0) b FROM dispatches WHERE owner=? AND plan_id=? AND phase='exploration'", (owner, plan_id)).fetchone()
                budget = scope["exploration"]["budget"]
                if used["p"] + (not discovery) > budget["max_probe_requests"] or used["d"] + discovery > budget.get("max_discovery_requests", 0) or used["b"] + len(payload) > budget["max_egress_bytes"]:
                    reject("budget_exceeded", "dispatch exceeds the exploration budget")
            self.db.execute("INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?,?)", (owner, plan_id, phase, operation_key, digest(request), len(payload), generation_tokens, int(discovery), self.clock()))
            return payload
