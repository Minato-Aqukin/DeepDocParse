# Recoverable input uploads

Protocol: additive extension of `control-v1.yaml`, migration `0009_upload_creation_reconciliation`.

A caller persists an ASCII `Idempotency-Key` before `POST /api/uploads`, together with
`filename`, `mime`, exact `size` and the complete lowercase hexadecimal `sha256`.
A key is scoped to the authenticated organization, actor kind and actor ID. It is
not a content deduplication key. Different actors uploading identical bytes get
separate upload identities, object keys and multipart sessions.

The canonical create request digest is `sha256:` followed by the hash of the
server-normalized request JSON. Repeated key plus identical normalized fields
returns the original upload; changed fields return `409 idempotency_conflict`.
Older callers may omit the key (and digest), retaining independent-create behavior.
They cannot recover a lost initial response by key.

## State and recovery

`allocation_state` is separate from content status:

| Allocation | Meaning | Allowed recovery |
| --- | --- | --- |
| pending | Identity/quota transaction committed, S3 creation not claimed | Retry the identical create with its original key |
| allocating | A persistent at-most-once claim was acquired | Query the fixed object key; do not start another S3 create |
| unknown | Creation receipt missing, lookup unavailable, zero receipts or multiple receipts | Reconcile the original key; unresolved cases need controlled operator cleanup |
| ready | Multipart receipt persisted | List parts and sign missing/incorrect parts |

An allocation of `ready` still has `input_state=waiting_input`. Finalize first
binds its idempotency key plus engine/options digest, checks every actual S3 part
number and byte count, completes the object and sets `content_verifying`. Only
streaming the entire object and validating size and SHA-256 can set
`content_verified` and emit `DocumentSubmitted`. Multipart ETags are not file
SHA-256 values. Transient digest reads stay verifying and are retried.

`GET /api/uploads/reconcile` carries the creation key in `Idempotency-Key`, not the
URL. It works when the caller lost the upload ID. `GET /api/uploads/{upload_id}`
has the same part refresh behavior when the ID is known. Both enforce current
upload permission and the original actor/organization. Responses use `no-store`.

`completed_parts` contains actual S3 ETags and byte lengths for valid parts.
`parts` signs only missing or incorrect parts; part geometry is persisted, so a
configuration change/restart cannot redefine existing part numbers. An expired
session produces no new signed URLs. `upload_incomplete` at finalize leaves the
same session resumable. If completion committed but the response was lost,
finalize HEADs the same object and resumes size/digest verification. An uncertain
completion returns `completion_unknown`, never a fabricated failed or ready state.

A 404 from reconciliation only reports no currently visible persistent record
for this subject. A concurrent create transaction may still commit. Keep the
original key; do not create a new execution generation because of a network error.

## Non-transactional S3 boundary

The PostgreSQL transaction creates the logical session, random object key and
input-page reservation before S3 is called. Exactly one CAS can transition
`pending` to `allocating`. There is no expiring lease that would allow a second
create while an old request remains in flight. Automatic SDK mutation retries
are disabled. An acquired multipart receipt is persisted with a short detached
context even when the requesting client disconnects.

If the process dies before persisting a receipt, reconciliation lists S3 multipart
uploads by the exact persisted random object key. One exact match can be attached;
zero or multiple matches remain unknown, with no new multipart creation and no
signed URLs. Listing an organization prefix or adopting another actor's matching
file/hash is forbidden.

Unknown sessions intentionally prefer retaining a visible unresolved identity
and quota reservation to duplicating a logical upload. Operator recovery must
first establish that the original create cannot still complete, inspect only the
session's fixed object key, and explicitly clean up obsolete multipart receipts.
This version does not provide an automatic operator cleanup command. It must not
clear the original business-key association or silently restart an unknown row.
Multipart/object garbage collection remains an operational follow-up.

## Quota and admission boundary

Creation checks the actual usage ledger plus all active input reservations under
an organization quota-row lock, and inserts its reservation in the same transaction.
Concurrent same-key requests reuse one reservation. Failed/expired/verified inputs
are excluded from active input reservations. This is input-transfer admission;
it is not execution admission, GPU reservation, final billing or an implementation
of the later TaskPlan/retention contract. The existing usage ledger accounts for
completed work separately.

Signed upload URLs are generated from the control server's configured object-store
endpoint. They authorize a separate storage recipient: a desktop client must check
its approved recipient policy before sending bytes. The upload response alone is
not a signed transfer descriptor or authorization to follow arbitrary remote URLs.
No same-origin upload proxy is implemented here.
