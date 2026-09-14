# Credential F1 correction — 2026-09-13

Author verification, pending independent recheck of F1. Only the credential broker
and its regression tests changed for this correction; no package rebuild was run.

Broker operations are serialized per `(environmentId, profileId)`. An older
status/load/use must finish before a later clear or replacement completes. This
orders both the memory map and actual filesystem writes, including replacing a
persistent key with a session-only key. Unrelated identities have separate queues;
a failed operation releases its queue. The internal withCredential callback is
part of that ordered operation; clear does not report success while an earlier
callback for the same identity is still running.

A synchronous clearSession advances a generation fence. An older queued operation
cannot return a stale result or refill the cleared map, including when its cleanup
fails. This clears memory only; subsequent explicit reads may reload approved
persistent ciphertext, as before.

Tests use real temporary files and real FileHandles. A trusted constructor I/O
adapter pauses a read only after the encrypted file bytes have been read, allowing
the exact ordering to be asserted without sleeps. No renderer can inject this
adapter. SafeStorage is an approved-backend test double with synthetic values;
this is not a claim of actual GNOME/KWallet persistence testing.

Verification:

- `node --test --test-timeout=10000 apps/desktop/test/credentials-race.test.mjs apps/desktop/test/boundaries.test.mjs`: **15 passed**.
- Seven new cases cover clear/status, clear/use, persistent/session replacement,
  clearSession fencing, independent profiles/error recovery, and the review's real
  filesystem race (**0 resurrections in 100 attempts**).
- Temporarily bypassing serialization makes all four deterministic clear/update
  regressions fail. The mutation was restored and the credential suite reran green.
