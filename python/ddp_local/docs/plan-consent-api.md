# Local plan review and consent ledger

The authenticated private loopback API adds four fixed user actions:

| Method and path | Action |
|---|---|
| `POST /api/v1/plans/prepare` | Persist a reviewable immutable scope; never sends or approves anything |
| `GET /api/v1/plans/{plan_id}` | Read that workspace owner's scope and approval state |
| `POST /api/v1/plans/{plan_id}/approve` | Explicitly approve `exploration` or `execution` for `confirmed_scope_digest` with `user_confirmed: true` |
| `POST /api/v1/plans/{plan_id}/revoke` | Permanently revoke this plan's approvals |

Mutations require one `Idempotency-Key`. The key namespace includes authenticated environment, workspace and subject. A key reused for different content fails with `idempotency_conflict`; an identical retry returns current state. The body cannot set the approving identity. A generated plan, a model suggestion, `planning_state=approved`, or a fabricated consent reference cannot grant permission.

`prepare` accepts a private scope envelope containing `task_spec`, `plan`, `input_manifest`, `payload_bindings`, `output_locations`, `retention`, and `exploration`. The embedded TaskSpec, TaskPlan and issued consents use the existing `ddp-task-probe/1` and `ddp-plan-admission/1` contracts. The scope envelope is a local approval record, not a new federated receipt. Its shape and a concrete example are in `tests/plan_samples.py`.

- Each input manifest item is `{ref, digest, size_bytes}`. The local HTTP adapter resolves `ref` as an imported source version. The trusted blob resolver rereads and rehashes actual immutable bytes on preparation, approval and dispatch. A missing resolver cannot approve self-declared input metadata. Derived evidence and remote inputs require a trusted adapter resolver; the current local HTTP route does not claim support for them.
- Each payload binding is `{payload_id, phase, recipient_node_id, payload_kind, digest, size_bytes, edge_id?, generation_tokens?}`. Execution requires the exact edge ID. Payloads addressed to generation steps require a positive fixed token reservation; dispatch derives it from this approved binding and cannot self-report a smaller value. Every actual outgoing payload has its own digest and recipient. Exploration scope names exact nodes, allowed categories, expiry, request and byte budgets. Trust domains must be resolved to a fixed node set before this implementation can accept them.
- Plan operations and dependency transitions are registered typed templates. Cross-node dependencies require compatible data edges. The local implementation conservatively reserves the sum of all planned transmission hops, including relays and parallel branches, against `max_hops`; a plan can raise that bound only through a new explicit approval. Source-policy references are resolved by a trusted adapter, never imported from request metadata. Every downstream recipient and relay must be permitted by the original source, so a denied B→C transfer cannot be laundered through B→A→C. Missing remote policy support fails closed.
- Output location references and retention are part of the reviewed scope. These references do not themselves grant resource publication rights, remote file access, or the right to retain another authority's data.

Canonical digests use UTF-8 JSON, sorted object keys, compact separators and no NaN. Array order is preserved. TaskSpec excludes `consent_refs`; TaskPlan excludes `plan_digest`, `planning_state` and `execution_consent_ref` to avoid circular grant references. All authority-bearing content, concrete input and outgoing payload digests, recipients, budget, expiry, retention and output references are covered by the immutable whole-scope digest. Approval state remains separate from the proposed plan body. Revising a prepared scope requires a new `plan_id`; it never mutates an already approved row.

The private `consents.sqlite3` sidecar uses mode 0600, transactional durable writes, and independent schema version 1. It does not change the workspace/Wiki database. `ConsentStore.set_local_only(True)` is a trusted workspace policy action: it persists strict local mode and revokes existing approvals. Returning online does not resurrect them. A caller's `local_only=False` cannot override TaskSpec or persisted workspace policy.

## Executor integration

Immediately before each actual send, call `ConsentStore.authorize_dispatch` with the authenticated identity, exact plan and scope IDs, phase, payload ID, verified recipient node ID, immutable payload bytes, current TaskSpec/TaskPlan, actual fixed input bytes, output location, retention, and a fresh per-send operation key. It rechecks current source policy, input hashes, both digests, expiry, revocation, workspace policy and recipient scope, and atomically reserves the shared root request/byte/token budget plus exploration sub-budget. Only send the returned bytes.

This return value is the result of that immediate check, not a durable bearer capability. Do not cache it or authorize when merely queuing asynchronous work: recheck when dispatching. An already reserved operation key fails `dispatch_already_reserved`; a different body under that key conflicts. Since a previous network send may have occurred, reconcile remote state before retrying, and reserve a fresh send against the remaining budget. Failed sends are conservatively charged; they do not produce free retries.

The transport adapter must separately verify that the selected URL/TLS credentials correspond to the approved node identity, disable unapproved redirects, select an approved operation, bind model-service recipients, enforce each reserved generation limit in the actual model request (and account for actual output), check receiver capability/policy revisions and reconcile remote admission/result receipts. This ledger performs no HTTP calls and returns `admission_state=not_submitted`. It does not assert remote acceptance, GPU reservation, execution, delivery, publication or cleanup. An existing local generation route is not silently converted into remote execution.
