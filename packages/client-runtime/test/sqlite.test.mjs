import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, statSync, symlinkSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawn } from 'node:child_process'
import { DatabaseSync } from 'node:sqlite'
import { SqliteProjectionStore } from '../src/sqlite-store.ts'

const projection = sequence => ({ sequence, cursor: `c${sequence}`, state: { text: '机密草稿', sequence } })
function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'ddp-projection-')), path = join(dir, 'cache.sqlite')
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  return { dir, path }
}

test('reopening the real database retains identity, state and cursor, and fences an old process', async t => {
  const {path} = fixture(t), first = new SqliteProjectionStore(path), second = new SqliteProjectionStore(path)
  t.after(() => {first.close(); second.close()})
  const a = await first.claim('env/profile', 'authority/user')
  assert.equal(await first.commit('env/profile', a.epoch, null, projection(1)), true)
  const b = await second.claim('env/profile', 'authority/user')
  assert.deepEqual(b.projection, projection(1))
  assert.equal(await first.commit('env/profile', a.epoch, 'c1', projection(2)), false)
  assert.equal(await second.commit('env/profile', b.epoch, 'c0', projection(2)), false)
  await assert.rejects(second.claim('env/profile', 'impostor/user'), /identity_mismatch/)
  assert.equal(statSync(path).mode & 0o777, 0o600)
})

test('a crash between dispatch and receipt never turns the recorded intent into a fresh command', async t => {
  const {path} = fixture(t)
  const before = new SqliteProjectionStore(path)
  await before.claim('s','b'); assert.equal((await before.intent('s','operation','digest')).status,'pending')
  before.close()
  const after = new SqliteProjectionStore(path); t.after(()=>after.close())
  assert.equal((await after.intent('s','operation','digest')).status,'unknown')
  await assert.rejects(after.intent('s','operation','different'), /idempotency_conflict/)
  await after.settle('s','operation','digest',{task_id:'durable'})
  await after.settle('s','operation','digest',null)
  assert.deepEqual((await after.readIntent('s','operation')).receipt,{task_id:'durable'})
  await after.forget('s')
  assert.equal((await after.intent('s','operation','digest')).status,'confirmed')
})

test('process termination rolls back an uncommitted projection and cursor together', async t => {
  const {path} = fixture(t), store = new SqliteProjectionStore(path)
  await store.claim('s','b'); await store.commit('s',1,null,projection(1)); store.close()
  const child = spawn(process.execPath, ['--input-type=module','-e', `
    import { DatabaseSync } from 'node:sqlite';
    const db = new DatabaseSync(process.argv[1]); db.exec('BEGIN IMMEDIATE');
    db.prepare('UPDATE connection_scope SET cursor=?,projection=? WHERE scope=?').run('c2',JSON.stringify({sequence:2,cursor:'c2',state:'not committed'}),'s');
    process.stdout.write('transaction-open'); setInterval(()=>{},1000);
  `,path], {stdio:['ignore','pipe','pipe']})
  t.after(()=>child.kill('SIGKILL'))
  await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject);child.once('exit',()=>reject(Error('child exited before transaction')))})
  const exited = new Promise(resolve=>child.once('exit',resolve)); child.kill('SIGKILL'); await exited
  const recovered = new SqliteProjectionStore(path); t.after(()=>recovered.close())
  assert.deepEqual((await recovered.claim('s','b')).projection, projection(1))
})

test('storage failure cannot advance the durable cursor', async t => {
  const {path} = fixture(t), store = new SqliteProjectionStore(path); t.after(()=>store.close())
  await store.claim('s','b'); await store.commit('s',1,null,projection(1))
  const injector = new DatabaseSync(path)
  injector.exec("CREATE TRIGGER simulated_full BEFORE UPDATE OF projection ON connection_scope BEGIN SELECT RAISE(ABORT,'disk full'); END;")
  injector.close()
  await assert.rejects(store.commit('s',1,'c1',projection(2)), /disk full/)
  assert.deepEqual((await store.claim('s','b')).projection, projection(1))
})

test('drafts have independent profile keys and a CAS revision; cache eviction retains the write ledger', async t => {
  const {path} = fixture(t), store = new SqliteProjectionStore(path); t.after(()=>store.close())
  await store.claim('s/a','a'); await store.claim('s/b','b')
  await store.saveDraft('s/a','wiki',0,{text:'Alice'}); await store.saveDraft('s/b','wiki',0,{text:'Bob'})
  await assert.rejects(store.saveDraft('s/a','wiki',0,{text:'stale tab'}),/draft_conflict/)
  assert.deepEqual((await store.readDraft('s/a','wiki')).value,{text:'Alice'})
  await store.intent('s/a','pending','digest'); await store.forget('s/a')
  assert.deepEqual((await store.readDraft('s/a','wiki')).value,{text:'Alice'})
  assert.equal((await store.intent('s/a','pending','digest')).status,'unknown')
  assert.deepEqual((await store.readDraft('s/b','wiki')).value,{text:'Bob'})
  assert.equal((await store.claim('s/a','a')).epoch,3)
})

test('oversize cache writes and symlink paths are rejected without overwriting existing data', async t => {
  const {dir,path} = fixture(t), store = new SqliteProjectionStore(path); t.after(()=>store.close())
  await store.claim('s','b'); await store.commit('s',1,null,projection(1))
  await assert.rejects(store.commit('s',1,'c1',{...projection(2),state:'x'.repeat(5*1024*1024)}), /cache_failure/)
  assert.deepEqual((await store.claim('s','b')).projection,projection(1))
  const link = join(dir,'link');symlinkSync(path,link)
  const before = readFileSync(path); assert.throws(()=>new SqliteProjectionStore(link))
  assert.deepEqual(readFileSync(path),before)
})

test('confirmed receipt bodies are bounded; tombstones prevent replay without blocking later new writes',async t=>{
  const {path}=fixture(t),store=new SqliteProjectionStore(path);t.after(()=>store.close())
  await store.claim('s','b')
  const seed=new DatabaseSync(path);seed.exec('BEGIN')
  const insert=seed.prepare("INSERT INTO command_intent VALUES (?, ?, ?, 'confirmed', ?)")
  for(let i=0;i<10001;i++)insert.run('s','key'+i,'digest'+i,JSON.stringify({task_id:'task'+i}))
  seed.exec('COMMIT');seed.close()
  assert.equal((await store.intent('s','new-key','new-digest')).status,'pending')
  await store.settle('s','new-key','new-digest',{task_id:'new'})
  assert.equal((await store.intent('s','key0','digest0')).status,'retired')
  await assert.rejects(store.intent('s','key0','changed'),/idempotency_conflict/)
  await store.settle('s','key0','digest0',{task_id:'task0'})
  assert.equal((await store.intent('s','key0','digest0')).status,'confirmed')
  const inspect=new DatabaseSync(path)
  assert.equal(inspect.prepare("SELECT count(*) n FROM command_intent WHERE status='confirmed'").get().n,128)
  inspect.close()
})

test('corrupt cache resync preserves identity, drafts and unresolved operations',async t=>{
  const {path}=fixture(t),store=new SqliteProjectionStore(path);t.after(()=>store.close())
  await store.claim('s','b');await store.intent('s','key','digest');await store.saveDraft('s','draft',0,{text:'keep'})
  const damage=new DatabaseSync(path)
  damage.prepare('UPDATE connection_scope SET cursor=?,projection=? WHERE scope=?').run('c99','broken-json','s');damage.close()
  assert.equal((await store.claim('s','b')).projection,null)
  assert.deepEqual((await store.readDraft('s','draft')).value,{text:'keep'})
  assert.equal((await store.intent('s','key','digest')).status,'unknown')
  await assert.rejects(store.claim('s','other'),/identity_mismatch/)
})
