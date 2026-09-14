# P3/P5 typed plan consent boundary — local component validation

Date: 2026-09-13. Scope: v3 §6.2–6.6, P3 work items 15–16 and the local approval portions of T76/T79/T80. This does not mark P3 remote execution or P5 federation complete.

Implemented in `ddp_core.application.plans`, `ddp_local.consents` and the fixed `ddp_local.plan_http` router. Private `consents.sqlite3` schema 1 is separate from workspace/Wiki schema 2. Existing federated JSON schemas were not changed. HTTP and executor integration requirements are documented in `python/ddp_local/docs/plan-consent-api.md`.

| Invariant | Executable evidence | Result |
|---|---|---|
| No query or metadata probe without exploration approval | Prepare has no grants; dispatch rejects before byte/budget records | PASS |
| Model proposal cannot sign permission | Imported approved state/consent refs rejected; explicit user action and exact reviewed scope required; HTTP signer identity is fixed | PASS |
| Exact task, plan, input, recipient, retention, output and budget binding | Query/plan/payload/input/recipient/output/retention mutation tests; trusted input resolver hashes actual local blobs | PASS |
| Local-only dominates fallback | TaskSpec validation plus durable workspace local-only policy; switching back online does not resurrect revoked grants | PASS |
| Center-only does not expand to third-party execution | Third node in typed plan rejected | PASS |
| B's source policy cannot be bypassed through A to C | Downstream recipients and relays checked against original source policy; denied B→A→C fixture | PASS |
| Local possession does not override remote source authority | Imported version with remote authority cannot use the default local source grant; no plan row persists | PASS |
| Plan/consent expiration, invalidated state and revocation block dispatch | Current plan and source policy rechecked; expiry/revoke/current-policy tests | PASS |
| Identity and idempotency boundaries | Environment/workspace/subject isolation, restart persistence, same-key body conflict, raw-request retry test | PASS |
| Hidden retry and exploration budget consumption | Per-send durable reservation; repeated dispatch key cannot authorize another send; probes consume sub-budget and root budget | PASS |
| Fixed local API | Prepare/approve/get/revoke HTTP lifecycle, forbidden signer field, authenticated owner propagation | PASS |

Verification after implementation:

- `python/ddp_core`: 83 passed.
- `python/ddp_local`: 75 passed.
- New plan/consent tests: 31 passed.
- Changed plan/consent code and tests: Ruff F/B passed.
- Actual generated TaskSpec, TaskPlan, ExplorationConsent and ExecutionConsent were validated against the existing resolved Draft 2020-12 JSON schemas with date-time format checking: all passed.

Limits: no remote HTTP executor is implemented by this component, no network URL-to-node identity is verified here, no provider capability revision is accepted merely from a claim, and no remote AdmissionReceipt, GPU reservation, accepted task, delivered result, publication or cleanup is asserted. Returned local state remains `admission_state=not_submitted`. The integration must perform the final byte authorization immediately before sending and separately verify the receiver endpoint, redirects, model recipients, protocol/capability, remote admission and delivery. Trust-domain grants need prior fixed-node resolution. Current HTTP input refs resolve imported source versions; remote/derived input grant resolution requires a trusted adapter.

Claude read-only review was attempted but its service returned a session-limit error (reset 04:00 Asia/Shanghai), so it is not counted as a passing independent review. OpenCode read-only review (configured `deepseek-flash`) completed and independently ran the then-current 28 tests. It found three concrete gaps, all fixed with regression tests: whole-plan transmission hops now share the root cap; generation reservations are fixed in the approved payload binding and cannot be reduced by the dispatch caller; exploration source/derived payloads now require verified input manifests just like execution. A malformed trusted source policy also fails with an application error rather than KeyError. Its concern about default local source policy is resolved by the authority model: the authenticated local owner may explicitly approve egress of their own data, while remotely inherited authority is denied without a trusted resolver. Its observation that network recipient trust and actual model token limits require executor enforcement remains an explicit integration requirement, not a claim this local ledger can fulfill. The post-review fixes were self-verified; no second independent acceptance is claimed. No commit or push was performed for this component.
