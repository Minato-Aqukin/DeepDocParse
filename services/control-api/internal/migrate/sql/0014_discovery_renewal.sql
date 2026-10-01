-- Directory pull renewal scheduling and observable failures. Approval and
-- descriptor expiry remain independent; a failed attempt never changes a lease.
ALTER TABLE control.node_members
    ADD COLUMN renewal_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN renewal_last_attempt_at TIMESTAMPTZ,
    ADD COLUMN renewal_next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN renewal_last_error TEXT,
    ADD COLUMN renewal_last_success_at TIMESTAMPTZ;
CREATE INDEX node_members_renewal_due_idx ON control.node_members (renewal_next_attempt_at)
    WHERE state = 'approved';
