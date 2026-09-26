-- One-use signed directory credentials survive restarts and concurrent receivers.
CREATE TABLE control.federation_credential_nonces (
    jti TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    operation TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_federation_credential_nonces_expiry
    ON control.federation_credential_nonces (expires_at);
