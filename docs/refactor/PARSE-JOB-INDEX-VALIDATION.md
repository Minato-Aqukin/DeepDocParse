# Fixed ParseJob index ownership

The authoritative index and compilation fields now live on `ParseJob`. Migration
0023 (down 0022) copies the old Document cache only into its exact current job;
other successful archived jobs become pending for reconciliation, never falsely
ready. All newly archived jobs are independently marked pending.

## Integration hooks

- `indexing.index_document(session, storage, http, document_id, *, job_id=None)`:
  explicit fixed job execution; omitted job_id is a legacy current-job adapter.
- `indexing.mark_index_pending(session, job_id) -> generation | None`: atomic
  generation advance and pending/reset state; caller commits and enqueues.
- Queue payload `{document_id, job_id}`, dedupe key `index:{job_id}`. Internal
  callbacks, worker handler and reconciliation now pass the exact job.
- Authoritative fields on ParseJob: index_status, index_error, index_generation,
  index_lease_until, compile_status, compile_degraded, compile_fingerprint,
  layout_version, code_detection. Document fields mirror only current_job_id.
- `claim_for_indexing(..., job_id=...)`, heartbeat and failure writes are fenced
  by ParseJob.index_generation; Document -> ParseJob is the mutation lock order.
- Final replacement deletes `Chunk` rows only for this job. Original Evidence
  records and chunks belonging to other jobs remain intact. A current-job cache
  selection change does not invalidate fixed citations.
- Source permission and generation are checked before every model/embedding
  dispatch and final save. A withdrawn/deleted/unbound resource cannot dispatch
  more requests or save a new index. Shared content does not transfer permission.
- Costs use original initiated_by and the exact resource organization. Embedding
  requests already dispatched before a later batch loses access are recorded.
- Conversation ask readiness and answer persistence fencing use the selected
  ParseJob. Extraction readiness uses its exact DocumentContext job.

The root task owns document/resource API presentation, reindex/switch endpoint
integration and publication readiness; those should consume these hooks rather
than mutate the Document cache as authority.

## Executed checks

`pytest tests/test_parse_job_index.py`: **6 passed**. Cases cover independent A/B
readiness, rebuilding B without destroying A chunks/evidence, original actor and
organization cost attribution, A withdrawal without blocking B, stale A worker
fencing and independent B heartbeat, reverse callback order, revocation between
embedding batches, and B question answering while A's Document cache is failed.

`scripts/check_parse_job_index_pg.py` passed against the explicitly supplied
scratch PostgreSQL database. Three concurrent attempts (A, A, B) yield one A
winner plus an independent B winner. Expiring A's lease permits generation 2;
A generation 1 heartbeat/failure writes fail while B remains generation 1.
The script created and removed its own randomly named schema.

Ruff F/B checks passed on indexing, archive, reconcile, internal callback, worker,
new tests, migration and PostgreSQL verification script.

The broader first integration run reached 133 passed / 6 failed / 1 skipped.
Two synthetic fixtures were corrected to mutate authoritative ParseJob state.
The remaining four failures were in document reindex/switch hooks being updated
concurrently by the root task; they must be rerun by the root integration gate.
GPU model behavior was not simulated as real-world validation; API tests use
explicit HTTP fixtures and the existing compilation implementation.
