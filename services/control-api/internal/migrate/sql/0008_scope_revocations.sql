-- Once a source withdraws authorization for a frozen catalog, expiry/restart must
-- not erase that observation. The frozen denominator and its digest remain intact.
CREATE TABLE control.scope_catalog_revocations (
    scope_id TEXT PRIMARY KEY REFERENCES control.scope_manifests(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
