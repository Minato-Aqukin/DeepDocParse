/** Desktop main/utility process only. This module must never enter the renderer bundle. */
import { DatabaseSync } from 'node:sqlite'
import { openSync, closeSync, fstatSync, fchmodSync, constants } from 'node:fs'
import { isAbsolute } from 'node:path'
import { ConnectionFault } from './index.ts'
import type { Intent, Json, Projection, ProjectionStore } from './index.ts'

type ScopeRow = { binding: string | null; epoch: number; cursor: string | null; projection: string | null }
type IntentRow = { digest: string; status: Intent['status']; receipt: string | null }
const MAX_JSON_BYTES = 4 * 1024 * 1024
function encode(value: unknown, max = MAX_JSON_BYTES): string {
  const encoded = JSON.stringify(value)
  if (encoded === undefined || Buffer.byteLength(encoded) > max) throw new ConnectionFault('cache_failure')
  return encoded
}
function intentOut(row: IntentRow): Intent {
  return { digest: row.digest, status: row.status, receipt: row.receipt === null ? null : JSON.parse(row.receipt) }
}

/** Epoch fencing and projection/cursor CAS are one SQLite transaction, across processes. */
export class SqliteProjectionStore implements ProjectionStore {
  private db: DatabaseSync
  constructor(path: string) {
    if (path !== ':memory:') {
      if (!isAbsolute(path)) throw new ConnectionFault('cache_failure')
      // The host supplies a private, approved directory, never a renderer path.
      const fd = openSync(path, constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW, 0o600)
      try {
        if (!fstatSync(fd).isFile()) throw new ConnectionFault('cache_failure')
        fchmodSync(fd, 0o600)
      } finally { closeSync(fd) }
    }
    this.db = new DatabaseSync(path, { timeout: 3000, allowExtension: false })
    try {
      this.db.exec('PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL; PRAGMA journal_mode=WAL; PRAGMA journal_size_limit=8388608; PRAGMA max_page_count=16384;')
      this.transaction(() => {
        const version = this.db.prepare('PRAGMA user_version').get()!.user_version
        if (version !== 0 && version !== 1 && version !== 2) throw new ConnectionFault('cache_failure')
        this.db.exec(`
          CREATE TABLE IF NOT EXISTS connection_scope (
            scope TEXT PRIMARY KEY, binding TEXT, epoch INTEGER NOT NULL DEFAULT 0,
            cursor TEXT, projection TEXT,
            CHECK ((cursor IS NULL) = (projection IS NULL))
          ) STRICT;
          CREATE TABLE IF NOT EXISTS command_intent (
            scope TEXT NOT NULL REFERENCES connection_scope(scope), key TEXT NOT NULL,
            digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','unknown','confirmed','retired')),
            receipt TEXT, PRIMARY KEY(scope,key)
          ) STRICT;
          CREATE TABLE IF NOT EXISTS view_draft (
            scope TEXT NOT NULL REFERENCES connection_scope(scope), key TEXT NOT NULL,
            revision INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY(scope,key)
          ) STRICT;
        `)
        if (version === 1) this.db.exec(`
          ALTER TABLE command_intent RENAME TO command_intent_v1;
          CREATE TABLE command_intent (
            scope TEXT NOT NULL REFERENCES connection_scope(scope), key TEXT NOT NULL,
            digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','unknown','confirmed','retired')),
            receipt TEXT, PRIMARY KEY(scope,key)
          ) STRICT;
          INSERT INTO command_intent SELECT * FROM command_intent_v1;
          DROP TABLE command_intent_v1;
        `)
        this.db.exec('PRAGMA user_version=2')
      })
    } catch (error) { this.db.close(); throw error }
  }
  close(): void { this.db.close() }
  private transaction<T>(work: () => T): T {
    this.db.exec('BEGIN IMMEDIATE')
    try { const value = work(); this.db.exec('COMMIT'); return value }
    catch (error) { this.db.exec('ROLLBACK'); throw error }
  }
  private row(scope: string): ScopeRow {
    if (!scope || scope.length > 4096) throw new ConnectionFault('cache_failure')
    this.db.prepare('INSERT INTO connection_scope(scope) VALUES (?) ON CONFLICT DO NOTHING').run(scope)
    return this.db.prepare('SELECT binding, epoch, cursor, projection FROM connection_scope WHERE scope=?').get(scope) as ScopeRow
  }
  async claim(scope: string, expectedBinding: string) {
    return this.transaction(() => {
      const row = this.row(scope)
      if (row.binding !== null && row.binding !== expectedBinding) throw new ConnectionFault('identity_mismatch')
      const epoch = row.epoch + 1
      this.db.prepare('UPDATE connection_scope SET binding=?, epoch=? WHERE scope=?').run(expectedBinding, epoch, scope)
      let projection: Projection | null = null
      if (row.projection !== null) {
        try {
          projection = JSON.parse(row.projection)
          if (!projection || projection.cursor !== row.cursor || !Number.isSafeInteger(projection.sequence)
              || projection.sequence < 0 || !('state' in projection)) throw new Error('invalid_projection')
        } catch {
          // This is a disposable projection, not an authoritative task or draft.
          this.db.prepare('UPDATE connection_scope SET cursor=NULL,projection=NULL WHERE scope=?').run(scope)
          projection = null
        }
      }
      return { epoch, projection }
    })
  }
  async commit(scope: string, epoch: number, expectedCursor: string | null, projection: Projection) {
    const encoded = encode(projection)
    return this.transaction(() => {
      const row = this.row(scope)
      if (row.epoch !== epoch || row.cursor !== expectedCursor) return false
      this.db.prepare('UPDATE connection_scope SET cursor=?, projection=? WHERE scope=?').run(projection.cursor, encoded, scope)
      return true
    })
  }
  async invalidate(scope: string, epoch: number) {
    this.db.prepare('UPDATE connection_scope SET epoch=epoch+1 WHERE scope=? AND epoch=?').run(scope, epoch)
  }
  async intent(scope: string, key: string, digest: string): Promise<Intent> {
    return this.transaction(() => {
      this.row(scope)
      const found = this.db.prepare('SELECT digest,status,receipt FROM command_intent WHERE scope=? AND key=?').get(scope, key) as IntentRow | undefined
      if (found) {
        if (found.digest !== digest) throw new Error('idempotency_conflict')
        return { ...intentOut(found), status: found.status === 'pending' ? 'unknown' : found.status }
      }
      const count = this.db.prepare("SELECT count(*) AS n FROM command_intent WHERE scope=? AND status IN ('pending','unknown')").get(scope)!.n as number
      // Never silently evict unresolved commands to make room for another write.
      if (count >= 10000) throw new ConnectionFault('cache_failure')
      this.db.prepare("INSERT INTO command_intent VALUES (?,?,?,'pending',NULL)").run(scope, key, digest)
      return { digest, status: 'pending', receipt: null }
    })
  }
  async readIntent(scope: string, key: string) {
    const row = this.db.prepare('SELECT digest,status,receipt FROM command_intent WHERE scope=? AND key=?').get(scope, key) as IntentRow | undefined
    return row ? intentOut(row) : null
  }
  async settle(scope: string, key: string, digest: string, receipt: Json | null) {
    this.transaction(() => {
      const row = this.db.prepare('SELECT digest,status FROM command_intent WHERE scope=? AND key=?').get(scope, key)
      if (!row || row.digest !== digest) throw new Error('idempotency_conflict')
      if (row.status === 'confirmed' || (row.status === 'retired' && receipt === null)) return
      this.db.prepare('UPDATE command_intent SET status=?,receipt=? WHERE scope=? AND key=?')
        .run(receipt === null ? 'unknown' : 'confirmed', receipt === null ? null : encode(receipt), scope, key)
      // Keep compact digest tombstones for replay prevention, but bound cached
      // receipt bodies. An old key requires lookup, never a fresh dispatch.
      if (receipt !== null) this.db.prepare(`UPDATE command_intent SET status='retired',receipt=NULL WHERE rowid IN
        (SELECT rowid FROM command_intent WHERE scope=? AND key<>? AND status='confirmed' ORDER BY rowid DESC LIMIT -1 OFFSET 127)`).run(scope,key)
    })
  }
  async discardUndispatched(scope: string, key: string, digest: string) {
    this.db.prepare("DELETE FROM command_intent WHERE scope=? AND key=? AND digest=? AND status='pending'").run(scope,key,digest)
  }
  async forget(scope: string) {
    this.transaction(() => {
      this.row(scope)
      this.db.prepare('UPDATE connection_scope SET epoch=epoch+1,cursor=NULL,projection=NULL WHERE scope=?').run(scope)
      // Drafts, bindings and receipts survive cache eviction. Draft deletion is a
      // separate explicit edit, never a side effect of removing a connection.
    })
  }
  async readDraft(scope: string, key: string): Promise<{ revision: number; value: Json } | null> {
    const row = this.db.prepare('SELECT revision,value FROM view_draft WHERE scope=? AND key=?').get(scope, key)
    return row ? { revision: row.revision as number, value: JSON.parse(row.value as string) } : null
  }
  async saveDraft(scope: string, key: string, expectedRevision: number, value: Json): Promise<number> {
    const encoded = encode(value, 1024 * 1024)
    return this.transaction(() => {
      this.row(scope)
      const row = this.db.prepare('SELECT revision FROM view_draft WHERE scope=? AND key=?').get(scope, key)
      if ((row?.revision ?? 0) !== expectedRevision) throw new Error('draft_conflict')
      const revision = expectedRevision + 1
      this.db.prepare(`INSERT INTO view_draft VALUES (?,?,?,?) ON CONFLICT(scope,key)
        DO UPDATE SET revision=excluded.revision,value=excluded.value`).run(scope, key, revision, encoded)
      return revision
    })
  }
}
