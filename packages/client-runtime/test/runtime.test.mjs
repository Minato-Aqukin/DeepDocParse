import { test } from 'node:test'
import assert from 'node:assert/strict'
import { Connection, ConnectionFault, ConnectionRegistry, MemoryProjectionStore, scopeKey } from '../src/index.ts'

const environment = { environmentId:'env', workspaceId:'ws', authorityNodeId:'node', endpoint:'https://center.invalid' }
const profile = { profileId:'profile', issuer:'issuer', subject:'subject' }
const projection = (sequence, state = { value: sequence }) => ({ sequence, cursor: `cursor-${sequence}`, state })
const tick = () => new Promise(resolve => setImmediate(resolve))
async function until(condition) { for (let i=0; i<200; i++) { if(condition()) return; await tick() } assert.fail('condition did not settle') }
function deferred() { let resolve, reject; const promise = new Promise((yes,no)=>{resolve=yes;reject=no}); return {promise,resolve,reject} }
function fixture(overrides={}) {
  const calls = { inspect:0, credential:0, authenticate:0, snapshot:0, events:0, command:0, close:0 }
  const session = {
    actor:{issuer:profile.issuer,subject:profile.subject},
    async snapshot() { calls.snapshot++; return projection(0) },
    async *events(cursor, signal) {
      calls.events++
      await new Promise(resolve => { if(signal.aborted) resolve(); else signal.addEventListener('abort',resolve,{once:true}) })
    },
    async command() { calls.command++; return {task:'one'} },
    async receipt() { return {task:'one'} },
    async close() { calls.close++ }, ...overrides.session,
  }
  const provider = {
    async inspect() { calls.inspect++; return {...environment,capabilities:['parse','search']} },
    async credential() { calls.credential++; return 'secret-never-persist' },
    async authenticate() { calls.authenticate++; return session }, ...overrides.provider,
  }
  return {calls,session,provider}
}
const options = {maxRetries:2,wait:async()=>{await tick()}}

test('read-only queries share the session, do not enter the write ledger, and fence late results', async()=>{
  const pending=deferred(), store=new MemoryProjectionStore(); let queries=0
  const f=fixture({session:{async query(){queries++;return pending.promise}}})
  const c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.query('arbitrary.fetch',{}),{code:'protocol_incompatible'})
  const query=c.query('corpus.search',{query:'evidence'})
  const rejected=assert.rejects(query,{code:'disposed'})
  await c.stop();pending.resolve({hits:[{text:'old identity'}]});await rejected
  assert.equal(queries,1);assert.equal(f.calls.command,0)
})

for (const operation of ['execute','receipt']) test(`late ${operation} settles the old identity ledger without returning into a disposed generation`, async()=>{
  const pending=deferred(),store=new MemoryProjectionStore();let requested=false
  const f=fixture({session:{
    async command(){requested=true;return pending.promise},
    async receipt(){requested=true;return pending.promise},
  }})
  const c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready')
  if(operation==='receipt')await store.intent(c.scope,'command-key','existing-digest')
  const running=operation==='execute'?c.execute('answer.generate',{},'command-key'):c.receipt('command-key')
  const rejected=assert.rejects(running,{code:'disposed'})
  await until(()=>requested);await c.stop();pending.resolve({task:'accepted-old-task'});await rejected
  const saved=await store.readIntent(c.scope,'command-key')
  assert.equal(saved.status,'confirmed');assert.deepEqual(saved.receipt,{task:'accepted-old-task'})
})

test('cached command receipt also fences a generation closed during ledger lookup',async()=>{
  const pending=deferred();let pauseLookup=false,entered=false
  class PausedStore extends MemoryProjectionStore{async intent(...args){const value=await super.intent(...args);if(pauseLookup){entered=true;await pending.promise}return value}}
  const store=new PausedStore(),f=fixture(),c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready')
  await c.execute('answer.generate',{},'command-key');pauseLookup=true
  const rejected=assert.rejects(c.execute('answer.generate',{},'command-key'),{code:'disposed'})
  await until(()=>entered);await c.stop();pending.resolve();await rejected
  assert.equal(f.calls.command,1);assert.equal((await store.readIntent(c.scope,'command-key')).status,'confirmed')
})

test('several panels share one connection; one bad observer cannot disconnect all panels', async()=>{
  const store = new MemoryProjectionStore(), registry = new ConnectionRegistry(store), f=fixture()
  const a=registry.acquire(environment,profile,f.provider,options)
  a.connection.subscribe(()=>{throw Error('one panel')})
  const b=registry.acquire(environment,profile,f.provider,options)
  assert.equal(a.connection,b.connection)
  await until(()=>a.connection.state.transport==='ready')
  assert.equal(f.calls.authenticate,1)
  await a.release(); assert.equal(b.connection.state.transport,'ready')
  await b.release(); assert.equal(b.connection.state.transport,'disconnected')
})

test('identity is checked before any credential leaves the secret broker', async()=>{
  const f=fixture({provider:{async inspect(){return {...environment,workspaceId:'wrong',capabilities:[]}}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start(); await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'identity_mismatch'); assert.equal(f.calls.credential,0); assert.equal(f.calls.authenticate,0)
  await c.stop()
})

test('authentication failure is bounded and acquiring another panel does not retry it', async()=>{
  const f=fixture({provider:{async authenticate(){f.calls.authenticate++;throw new ConnectionFault('authentication_required')}}})
  const registry=new ConnectionRegistry(new MemoryProjectionStore())
  const a=registry.acquire(environment,profile,f.provider,options)
  await until(()=>a.connection.state.transport==='blocked')
  const b=registry.acquire(environment,profile,f.provider,options)
  await tick(); assert.equal(f.calls.authenticate,1)
  await b.release(); await a.release()
})

test('network retry has one finite owner', async()=>{
  const f=fixture({provider:{async inspect(){f.calls.inspect++;throw Error('network')}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start(); c.start(); await until(()=>c.state.transport==='blocked')
  assert.equal(f.calls.inspect,3); assert.equal(c.state.reason,'connection_failed'); await c.stop()
})

test('projection and cursor commit together; failed persistence does not advance either', async()=>{
  class FailingStore extends MemoryProjectionStore { async commit(...args){if(args[3].sequence===1) throw Error('disk full');return super.commit(...args)} }
  const store=new FailingStore(), f=fixture({session:{async *events(){yield projection(1)}}})
  const c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'cache_failure'); assert.deepEqual(c.state.projection,projection(0))
  const persisted=await store.claim(scopeKey(environment,profile),JSON.stringify(['ws','node','issuer','subject']))
  assert.deepEqual(persisted.projection,projection(0));await c.stop()
})

test('sequence gap recovers a fresh authoritative snapshot instead of stitching events', async()=>{
  let snapshots=0, streams=0
  const f=fixture({session:{async snapshot(){return projection(snapshots++ ? 10:0)},
    async *events(cursor,signal){if(streams++===0){yield projection(4);return} assert.equal(cursor,'cursor-10');await new Promise(r=>signal.addEventListener('abort',r,{once:true}))}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start();await until(()=>c.state.transport==='ready' && snapshots===2)
  assert.equal(c.state.projection.sequence,10);await c.stop()
})

test('expired log cursor starts a new snapshot and duplicate acknowledgments do not reapply', async()=>{
  let streams=0,snapshots=0
  const f=fixture({session:{async snapshot(){return projection(snapshots++ ? 20:0)},
    async *events(cursor,signal){if(streams++===0)throw new ConnectionFault('cursor_expired');yield projection(20);await new Promise(r=>signal.addEventListener('abort',r,{once:true}))}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start();await until(()=>c.state.projection?.sequence===20 && c.state.transport==='ready')
  assert.equal(snapshots,2);await c.stop()
})

test('late snapshot from a disposed generation cannot replace newer state', async()=>{
  const old=deferred();let attempt=0
  const f=fixture({session:{async snapshot(){return ++attempt===1 ? old.promise:projection(9)}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start();await until(()=>attempt===1);await c.wake();await until(()=>c.state.projection?.sequence===9)
  old.resolve(projection(1));await tick();await tick();assert.equal(c.state.projection.sequence,9);await c.stop()
})

test('profile identity is also verified after authentication', async()=>{
  const f=fixture({session:{actor:{issuer:'other',subject:profile.subject}}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start();await until(()=>c.state.transport==='blocked');assert.equal(c.state.reason,'profile_mismatch')
  assert.equal(f.calls.snapshot,0);await c.stop()
})

test('profile cache binding persists across registry replacement', async()=>{
  const store=new MemoryProjectionStore(), f=fixture(), c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready');await c.stop()
  const wrong=new Connection(environment,{...profile,subject:'different-user'},f.provider,store,options)
  wrong.start();await until(()=>wrong.state.transport==='blocked')
  assert.equal(wrong.state.projection,null);assert.equal(f.calls.credential,1);await wrong.stop()
})

test('intent precedes command; uncertain writes never replay on wake or execute; receipt repairs', async()=>{
  let store=new MemoryProjectionStore(), f=fixture({session:{async command(){f.calls.command++;throw Error('response lost')}}})
  const c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',{name:'test'},'command-key'))
  const intent=await store.readIntent(scopeKey(environment,profile),'command-key')
  assert.equal(intent.status,'unknown');assert.ok(intent.digest)
  await c.wake();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',{name:'test'},'command-key'));assert.equal(f.calls.command,1)
  assert.deepEqual(await c.receipt('command-key'),{task:'one'})
  assert.deepEqual(await c.execute('wiki.build',{name:'test'},'command-key'),{task:'one'})
  assert.equal(f.calls.command,1)
  await assert.rejects(c.execute('wiki.build',{name:'different'},'command-key'),/idempotency_conflict/)
  await c.stop()
})

test('forgetting projection fences late writers and does not reuse epochs', async()=>{
  const store=new MemoryProjectionStore(), a=await store.claim('s','identity')
  await store.forget('s');const b=await store.claim('s','identity')
  assert.ok(b.epoch>a.epoch);assert.equal(await store.commit('s',a.epoch,null,projection(1)),false)
})

test('old panel release cannot remove the replacement connection', async()=>{
  const registry=new ConnectionRegistry(new MemoryProjectionStore()), f=fixture()
  const old=registry.acquire(environment,profile,f.provider,options)
  await until(()=>old.connection.state.transport==='ready')
  await registry.remove(environment,profile)
  const fresh=registry.acquire(environment,profile,f.provider,options)
  await until(()=>fresh.connection.state.transport==='ready');await old.release()
  const another=registry.acquire(environment,profile,f.provider,options)
  assert.equal(fresh.connection,another.connection);await another.release();await fresh.release()
})

test('disposing while persisting an intent prevents a late write from being dispatched', async()=>{
  const barrier=deferred()
  class PausedStore extends MemoryProjectionStore { async intent(...args) {const intent=await super.intent(...args);await barrier.promise;return intent} }
  const store=new PausedStore(), f=fixture(), c=new Connection(environment,profile,f.provider,store,options)
  c.start();await until(()=>c.state.transport==='ready')
  const write=c.execute('wiki.build',{},'command-key')
  await until(()=>store.rows.get(c.scope)?.intents.size===1)
  await c.stop();barrier.resolve();await assert.rejects(write,/disposed/)
  assert.equal(f.calls.command,0);assert.equal(await store.readIntent(c.scope,'command-key'),null)
  await c.wake();await until(()=>c.state.transport==='ready')
  assert.deepEqual(await c.execute('wiki.build',{},'command-key'),{task:'one'});assert.equal(f.calls.command,1);await c.stop()
})

test('a transport whose close never settles cannot block a replacement connection', async()=>{
  const f=fixture({session:{async close(){await new Promise(()=>{})}}}), c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options)
  c.start();await until(()=>c.state.transport==='ready');await c.wake()
  await until(()=>c.state.transport==='ready');assert.equal(f.calls.authenticate,2);await c.stop()
})

test('invalid cyclic command JSON is rejected before an intent or network write', async()=>{
  const f=fixture(),c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),options),payload={}
  payload.self=payload;c.start();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',payload,'command-key'),/invalid_event/)
  assert.equal(f.calls.command,0);await c.stop()
})

test('unrelated outages separated by a healthy interval have independent retry budgets',async()=>{
  let clock=0,streams=0
  const f=fixture({session:{async *events(cursor,signal){
    streams++;clock+=10000;yield projection(0)
    if(streams<8)throw Error('new outage')
    await new Promise(resolve=>signal.addEventListener('abort',resolve,{once:true}))
  }}})
  const c=new Connection(environment,profile,f.provider,new MemoryProjectionStore(),{...options,now:()=>clock})
  c.start();await until(()=>streams===8);assert.equal(c.state.transport,'ready');await c.stop()
})

test('relocating the same authority preserves cached state but verifies identity before credentials',async()=>{
  const store=new MemoryProjectionStore(),registry=new ConnectionRegistry(store),f=fixture()
  const first=registry.acquire(environment,profile,f.provider,options)
  await until(()=>first.connection.state.transport==='ready')
  const moved={...environment,endpoint:'https://new-address.invalid'}
  const next=await registry.relocate(moved,profile,f.provider,options)
  await until(()=>next.connection.state.transport==='ready')
  assert.deepEqual(next.connection.state.projection,projection(0));assert.equal(f.calls.snapshot,1)
  assert.equal(f.calls.credential,2);await first.release();await next.release()
})
