-- Upload resource handoff: direct immutable-version uploads.
--
-- Permanent uploads may target one existing owned/live resource. The target is
-- part of the immutable upload identity: it is frozen at session creation and
-- bound into the DocumentSubmitted payload from the stored row at
-- verification, never from finalize input. Changing it on retry is an
-- idempotency conflict (the creation digest covers it).
--
-- Ingest state is NOT a column here: ready-session ingest status is derived
-- from the durable control_outbox row for the same organization +
-- DocumentSubmitted payload.upload_id (delivered_at / rejected_at /
-- last_error). Missing event is pending, never ready.
--
-- No foreign key to corpus tables: control must never hard-bind corpus
-- release order (0001 header), and the corpus consumer re-checks
-- ownership/liveness when the event arrives.
ALTER TABLE control.upload_sessions
    ADD COLUMN target_resource_id TEXT;
-- Only permanent uploads may target a resource. Temporary-compute uploads
-- never become corpus assets and must not carry a target.
ALTER TABLE control.upload_sessions
    ADD CONSTRAINT upload_target_permanent_ck CHECK (
        target_resource_id IS NULL OR purpose = 'permanent'
    );
-- Terminal delivery state lives on the event row, not in error text or a
-- distant next_attempt_at: rejected events leave the claimable queue and stay
-- visible as rejected ingest. NULL for all existing rows.
ALTER TABLE control.control_outbox
    ADD COLUMN rejected_at TIMESTAMPTZ;
-- Ingest-status reads join the session to its DocumentSubmitted event by
-- organization + payload upload_id; index that lookup directly.
CREATE INDEX control_outbox_doc_submitted_upload_idx ON control.control_outbox
    (organization_id, ((payload->>'upload_id')))
    WHERE type = 'DocumentSubmitted';
