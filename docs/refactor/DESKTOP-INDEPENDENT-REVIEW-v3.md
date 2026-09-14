# Desktop / shared client independent review — 2026-09-13

Current result: **PASS for the bounded reviewed slice — F1 and F2 are fixed and
independently rechecked.** This verdict does not cover the ongoing Vue/Wiki product
integration, full P3, packaging rebuilds or a repository-wide commit acceptance.
The initial review found two reproducible blocking findings, reported to the
implementation owner. This is an independent, bounded review of the uncommitted
`apps/desktop/**` and `packages/client-runtime/**` implementation, not P3 completion
or acceptance of the entire repository/commit. No implementation file was changed
by this reviewer.

Baseline HEAD: `c09783d3555fae2ce86f229f18f37418b036f276`. Both reviewed directories
were new/untracked, so review covered their actual source and tests rather than
relying on `git diff --stat`, which omits untracked files. The parent was concurrently
editing the Vue workbench/query allowlist; those product changes are outside this
initial verdict. The plan requirements read were root v3 §3 and P3, T22 and T65–T68,
plus `packages/contracts/ddp/client-runtime-format.md`, `apps/desktop/HOST-CONTRACT.md`,
and both existing validation records.

## Reproduced findings

### F1 — P1: clearing a credential can resurrect it from an in-flight load

Location at initial review: `apps/desktop/src/credentials.mjs:62–94`, especially
the unconditional `#secrets.set(key, value)` at line 79 and `clear()` at line 88.
Source SHA256: `a83853be36cbf7089dd5e296bac205b210beaac37f4883da561fea8a3df5dd1e`.

`#load()` can open/read an old encrypted file while `clear()` deletes the memory
entry and file. After `clear()` has returned `{present:false}`, the pending load
puts the old credential back in the map. A later `withCredential()` can therefore
use a credential the UI has reported cleared. All identity checks still match;
the missing boundary is ordering/invalidation within the same identity. Concurrent
`set`/replacement should be covered by the same fix.

Actual reproduction: **90 of 100** attempts left `present:true` after successful
clear, with zero load errors. This uses real asynchronous filesystem operations
in a fresh temporary directory. The reversible safeStorage stub is used only to
exercise the approved-backend code path; all credential data are synthetic and
are removed afterwards. No user key, `.env`, or other conversation was read.

From the repository root:

```sh
node --input-type=module <<'EOF'
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {CredentialBroker} from './apps/desktop/src/credentials.mjs';
const directory = await mkdtemp(path.join(tmpdir(), 'ddp-cred-review-'));
const safeStorage = {
  getSelectedStorageBackend: () => 'gnome_libsecret',
  isEncryptionAvailable: () => true,
  encryptString: value => Buffer.from(value),
  decryptString: value => value.toString(),
};
const pair = {environmentId:'review-env', profileId:'review-profile'};
let resurrected = 0, errors = 0;
try {
  for (let i = 0; i < 100; i++) {
    const writer = new CredentialBroker({directory, safeStorage});
    await writer.set({...pair, secret:'synthetic-test-token-only', persist:true});
    const reader = new CredentialBroker({directory, safeStorage});
    const pending = reader.status(pair).catch(() => { errors++; });
    await reader.clear(pair);
    await pending;
    if ((await reader.status(pair)).present) resurrected++;
  }
  console.log({runs:100, resurrectedAfterClear:resurrected, readErrors:errors});
} finally { await rm(directory, {recursive:true, force:true}); }
EOF
```

Expected: zero resurrection; a completed clear invalidates older loads/writes.
Recommended correction: serialize broker operations per identity, or use an
identity generation fence that prevents old loads and writes from restoring a
cleared/replaced secret. Add a deterministic delayed-I/O regression in addition
to checking the real filesystem race.

**F1 recheck, 2026-09-13 00:52 CST: PASS.** The author added per-identity operation
serialization for `status`, `withCredential`, `set` and `clear`, plus a synchronous
session-generation fence for `clearSession`. The callback remains inside the
identity queue; a later clear cannot report success while an older use is active.
Failed operations release their queue and unrelated identities use independent
queues. This reviewer read the correction and its deterministic delayed-FileHandle
tests, reran all 15 credential/boundary tests successfully, and separately reran
the original script printed above: **100 runs, 0 resurrections, 0 read errors**.
The complete desktop/shared-client suite then passed **67 tests, zero skips**.
Corrected credential source SHA256:
`be8dcb8bfd1c63559bfe68c3a939c6ae5a1f9a460a0b131794f7176e77248a26`.
The author's serialization mutation result was inspected in
`apps/desktop/CREDENTIAL-RACE-FIX.md`; this reviewer did not repeat the mutation
or modify implementation files. Actual GNOME/KWallet behavior remains outside
this test-double-based concurrency verification.

### F2 — P2: disposed command/receipt calls still return old-generation data

Location at initial review: `packages/client-runtime/src/index.ts:267–302`,
especially returns after `await session.command()` / `await session.receipt()`;
`apps/desktop/src/client-host.mjs:206–207` directly forwards them.
Initial source SHA256: `10873ac1a58f3175d6e8779cf88aebdbf6c7196e05deb39c0780410644229fd4`.

`query()` fences its response after awaiting the provider. `execute()` and
`receipt()` do not. A pending operation resolves successfully to its caller after
`stop()` has disposed the connection. This violates the old-generation response
boundary and leaves the host dependent on each UI caller correctly rejecting
late private results. Transport cancellation alone is insufficient: the shared
runtime explicitly supports fencing transports that ignore cancellation, and
durable settlement introduces another asynchronous boundary.

Actual: both operations returned `{privateResult:'old-connection-response'}` while
transport was `disconnected`. `execute` correctly retained a confirmed receipt in
its original scope; keeping that ledger record is desirable and must not be lost
when rejecting the stale UI response.

```sh
node --input-type=module <<'EOF'
import {Connection, MemoryProjectionStore} from './packages/client-runtime/src/index.ts';
const tick = () => new Promise(resolve => setImmediate(resolve));
const environment = {environmentId:'env', workspaceId:'workspace',
  authorityNodeId:'node', endpoint:'https://example.invalid'};
const profile = {profileId:'profile', issuer:'node', subject:'review'};
for (const operation of ['execute', 'receipt']) {
  let resolve, requested = false;
  const pending = new Promise(done => { resolve = done; });
  const session = {
    actor:profile,
    snapshot:async () => ({sequence:0, cursor:'c0', state:{}}),
    async *events(cursor, signal) {
      await new Promise(done => signal.addEventListener('abort', done, {once:true}));
    },
    close:async () => {},
    command:async () => { requested = true; return pending; },
    receipt:async () => { requested = true; return pending; },
  };
  const provider = {
    inspect:async () => ({...environment, capabilities:[]}),
    credential:async () => 'synthetic-token-only',
    authenticate:async () => session,
  };
  const connection = new Connection(environment, profile, provider, new MemoryProjectionStore());
  connection.start();
  while (connection.state.transport !== 'ready') await tick();
  const result = operation === 'execute'
    ? connection.execute('wiki.build', {}, 'review-key')
    : connection.receipt('review-key');
  while (!requested) await tick();
  await connection.stop();
  resolve({privateResult:'old-connection-response'});
  console.log({operation, transport:connection.state.transport, result:await result});
}
EOF
```

Expected: reject the caller response with `disposed`; retain any successfully
obtained receipt under the original scope, without replaying the operation.
Include the already-confirmed intent fast path and the asynchronous settlement
boundary in the generation check.

**F2 recheck, 2026-09-13 00:27 CST: PASS.** The parent added active-controller and
session checks after settlement/lookup, and moved the cached-confirmed branch
behind the fence. This reviewer reran both delayed-provider reproductions with
`assert.rejects(result, {code:'disposed'})`: both now reject; the old execute
intent remains `confirmed`. The shared client suite independently reran with
**42 passed, 0 failed/skipped**, including three new regressions for late execute,
late receipt and closure during a cached receipt lookup. F1 was still open at that
recheck and was subsequently closed by the separate recheck above.

## Check matrix

These verdicts describe the bounded source checks, not whole-scenario completion.

| Boundary | Verdict | Evidence |
| --- | --- | --- |
| Trusted current main-frame IPC only | PASS | Forged sender/window/frame and wrong origin negative tests; main handlers validate sender and exact argument schema |
| Renderer privilege / network surface | PASS (source + existing GUI evidence) | Sandboxed/context-isolated window, Node disabled, no generic IPC/shell/path API; CSP and host request filter deny remote content requests |
| Native workspace/path handles | PASS | Unknown handles, replaced/symlink workspace, forged token PID/mode/URL and static UI traversal tests |
| Credential fallback / ciphertext binding | PASS | `basic_text` and unavailable backend never persist; copied ciphertext cannot change environment/profile; sequential replacement removes old persistence |
| Concurrent credential clear / replacement | PASS after F1 fix | Original real filesystem race now 0/100; deterministic clear/use/replacement/session-generation tests pass |
| Remote identity before credentials | PASS | Fresh Ed25519 challenge, signed endpoint and node-key digest; copied/relayed proof, different node, redirects and chat-only service negative tests |
| Environment/profile cache and drafts | PASS | Real SQLite reopening, binding mismatch rejection, scoped draft CAS; clearing projection preserves drafts/receipts/assets |
| One finite reconnect owner | PASS | Multiple panels share one connection, bounded authentication/network retry and stable-window retry reset tests |
| Projection/cursor durability | PASS | Atomic commit, real process rollback, disk write failure, corrupt projection recovery, gap/expired cursor recovery and stale generation tests |
| Read query fencing | PASS | Fixed query names/endpoints, no write ledger entry, late query rejected |
| Command/receipt response fencing | PASS after F2 fix | Both initially resolved after disposal; independent rerun now rejects `disposed` and retains the original receipt |
| Unknown write replay prevention | PASS | Persisted intent before dispatch, repeated unknown key blocked, explicit receipt reconciliation, confirmed-receipt tombstones and restart tests |
| Native PDF/bundle file operations | PASS | Real CPU import, Chinese filename, single FD snapshot/hash, fixed-version original bytes, full bundle verifier before atomic export; lost upload response does not send bytes twice |
| Owned process lifecycle | PASS (bounded) | Real Python runtime, startup/stop race, refresh/unsubscribe/disconnect retain runtime, controlled suspend/resume, unrelated externally started child unaffected |
| Remote business approval | PASS (fail-closed boundary only) | Remote command/file dispatch reports `approved_plan_required`; approved-plan business execution is not implemented by this bridge |

## Wiki IPC/HTTP increment — 2026-09-13 00:53 CST

At the parent's request, the second review also inspected the new `wiki.list`,
`wiki.get`, `wiki.revisions`, `wiki.create`, `wiki.rebuild` and `wiki.edit` entries
in `client-policy.mjs`, `bridge.d.ts`, the shared query allowlist and the fixed
HTTP provider mappings. **PASS for this boundary slice; UI/business integration
is not included.** The provider routes were cross-checked against the actual
local HTTP decorators in `python/ddp_local/ddp_local/http.py`.

An independent temporary loopback HTTP server returned a valid synthetic local
handshake and recorded actual method/URL/body/idempotency headers. Inputs were
first processed through the actual desktop `clientArguments` function, then sent
through the actual `HttpProvider`. This tests the complete parameter/mapping
boundary without a generator or a remote service. All seven mappings matched:

| Typed request input | Actual method/path (expected matched) |
| --- | --- |
| `wiki.list`, limit 3, cursor `a/b?scope=other&x=1` | `GET /api/v1/wikis?cursor=a%2Fb%3Fscope%3Dother%26x%3D1&limit=3` |
| `wiki.get`, wiki `wiki-1` | `GET /api/v1/wikis/wiki-1` |
| `wiki.get`, wiki `wiki-1`, revision `revision-1` | `GET /api/v1/wikis/wiki-1/revisions/revision-1` |
| `wiki.revisions`, wiki `wiki-1`, limit 2 | `GET /api/v1/wikis/wiki-1/revisions?limit=2` |
| `wiki.create`, valid local-only body | `POST /api/v1/wikis` |
| `wiki.rebuild`, wiki `wiki-1`, valid body/base revision | `POST /api/v1/wikis/wiki-1/revisions` |
| `wiki.edit`, wiki `wiki-1`, page `page-1`, base revision/human paragraph | `PATCH /api/v1/wikis/wiki-1/pages/page-1` |

Read requests carried no idempotency key; all three write keys were preserved.
Five injected inputs were rejected before dispatch: `wiki_id:'../escape'`, a list
`url` field, a create body with `remote_allowed`/`allow_remote:true`, edit
`page_key:'../source'`, and an extra rebuild `endpoint` field. Repeating the three
write names through a provider without `localCommands` returned
`approved_plan_required` each time, with **zero HTTP write dispatches**. Cursor
metacharacters stayed inside one encoded query parameter rather than changing
the path or adding a scope parameter.

Reviewed increment SHA256 values:

- `packages/client-runtime/src/index.ts`:
  `ff6f9f35fbe336a3300e67235447c2bd4d8ac18b11bb5bfc44680668e4512c7a`
- `packages/client-runtime/src/http-provider.ts`:
  `e81b5f1e147c1088f56aa6d4a5beb39b4f4ef17e17c44ea676e92685df78f762`
- `apps/desktop/src/client-policy.mjs`:
  `49d81faf197b9f6e7eb1e05d2d0ac3fdab18e6dc4004bd40ca8f39a69b833446`

## Actual verification and limits

- `node --test --test-timeout=30000 apps/desktop/test/*.test.mjs packages/client-runtime/test/*.test.mjs`:
  **57 passed, 0 failed, 0 skipped**, 6.17 seconds. This includes real local CPU
  runtime and SQLite tests; it does not make the two independent counterexamples pass.
- `apps/web/node_modules/.bin/tsc -p packages/client-runtime/tsconfig.json`: passed.
- After both fixes and the Wiki mapping increment, the same complete Node command
  independently reran: **67 passed, 0 failed, 0 skipped**, 6.14 seconds. The shared
  TypeScript check was rerun and passed. No open confirmed finding remains in this
  reviewed slice.
- No GPU or model download was performed. No installation, publication, commit or
  push was performed. The existing real Wayland/package reports were inspected;
  this initial review did not repeat their GUI or package build runs.
- Secure storage concurrency used the explicit approved-backend test double;
  actual GNOME/KWallet persistence, physical sleep, X11/other platforms, installer
  upgrade/rollback and full center approved-plan execution remain unproved.
- Vue workbench changes, complete resource/Wiki product recovery, center business
  authorization and the entire P3/T65–T68 exits require their own integration review.

OpenCode `deepseek/deepseek-v4-flash` completed a bounded read-only second opinion
of the IPC policy, credentials, preload, workspace handles and minimal consumers.
It reported no confirmed flaw, and correctly limited an unsubscribe-ID collision
to the single trusted renderer rather than calling it a cross-principal bypass.
It ran no tests and missed F1's concurrency boundary; its static result does not
override the concrete filesystem reproduction. No additional independently
confirmed security finding came from this second opinion.
