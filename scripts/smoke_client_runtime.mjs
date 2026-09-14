/** Real Node provider -> private Python HTTP -> CPU parse -> durable projection/receipt. */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile, stat } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { Connection } from '../packages/client-runtime/src/index.ts'
import { HttpProvider } from '../packages/client-runtime/src/http-provider.ts'
import { SqliteProjectionStore } from '../packages/client-runtime/src/sqlite-store.ts'

const root=resolve(fileURLToPath(new URL('..',import.meta.url)))
const dir=await mkdtemp(join(tmpdir(),'ddp-client-real-')),children=[],connections=[],stores=[]
const report={platform:process.platform,arch:process.arch,node:process.version,protocol:'ddp-client/1',checks:[]}
const input=await readFile(join(root,'tests/fixtures/sample.pdf'))
report.input_sha256=createHash('sha256').update(input).digest('hex')
async function until(fn) {
  const deadline=Date.now()+20000
  while(Date.now()<deadline){if(await fn())return;await new Promise(resolve=>setTimeout(resolve,40))}
  throw new Error('expected_state_timeout')
}
async function launch() {
  const child=spawn(process.env.DDP_TEST_PYTHON || join(root,'.venv/bin/python'),
    ['-m','ddp_local.cli','--workspace',join(dir,'workspace'),'serve'],{cwd:root,stdio:['ignore','pipe','pipe']})
  children.push(child)
  let line='',publicInfo
  child.stdout.on('data',chunk=>{
    line+=chunk.toString()
    if(line.includes('\n')) {try {publicInfo=JSON.parse(line.split('\n')[0])}catch{/* fail below */}}
  })
  // Drain diagnostics, but never put a credential-bearing process response in the report.
  child.stderr.on('data',()=>{})
  await until(()=>{if(child.exitCode!==null||child.signalCode)throw Error('runtime_exited');return publicInfo})
  assert.equal((await stat(publicInfo.token_file)).mode & 0o777,0o600)
  const privateInfo=JSON.parse(await readFile(publicInfo.token_file,'utf8'))
  assert.equal(privateInfo.pid,child.pid);assert.equal(privateInfo.url,publicInfo.url)
  return {child,publicInfo,privateInfo}
}
function connect(info) {
  const id=info.publicInfo.identity
  const environment={environmentId:id.environment_id,authorityNodeId:id.authority_node_id,
    workspaceId:id.workspace_id,endpoint:info.publicInfo.url}
  const profile={profileId:'local',...info.publicInfo.profile}
  // A new listener port must reuse the same identity-scoped durable cache.
  const store=new SqliteProjectionStore(join(dir,'cache.sqlite'));stores.push(store)
  const provider=new HttpProvider({ownedLoopback:true,localCommands:true,pollMs:50,
    inspect:async()=>({...environment,capabilities:info.publicInfo.capabilities}),
    credential:async()=>info.privateInfo.token})
  const connection=new Connection(environment,profile,provider,store,{maxRetries:2})
  connections.push(connection);connection.start();return connection
}
async function terminate(child) {
  if(child.exitCode!==null||child.signalCode)return
  const stopped=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGTERM');await stopped
}
try {
  const first=await launch(),connection=connect(first)
  await until(()=>connection.state.transport==='ready');report.checks.push('owned_bootstrap_authenticated')
  const response=await fetch(first.publicInfo.url+'/api/v1/resources/upload',{method:'POST',body:input,
    headers:{Authorization:'Bearer '+first.privateInfo.token,'X-Filename':'sample.pdf','Idempotency-Key':'real-client-upload'}})
  assert.equal(response.status,202)
  const accepted=await response.json()
  await until(()=>connection.state.projection?.state.tasks.some(task=>task.status==='succeeded'))
  const snapshot=connection.state.projection
  assert.equal(snapshot.state.resources.length,1);assert.ok(snapshot.sequence>0)
  assert.equal(snapshot.state.tasks.length,1);report.checks.push('real_cpu_task_projected')
  const receipt=await connection.receipt('real-client-upload')
  assert.equal(receipt.id,snapshot.state.tasks[0].id);report.checks.push('accepted_operation_reconciled')
  await connection.stop();assert.equal(first.child.exitCode,null);report.checks.push('disconnect_does_not_terminate_task_owner')
  await terminate(first.child)
  await assert.rejects(stat(first.publicInfo.token_file),{code:'ENOENT'});report.checks.push('shutdown_removed_session_credential')
  const second=await launch();assert.deepEqual(second.publicInfo.identity,first.publicInfo.identity)
  const reconnected=connect(second);await until(()=>reconnected.state.transport==='ready' &&
    reconnected.state.snapshot==='current' && reconnected.state.projection.sequence>snapshot.sequence)
  assert.equal((await reconnected.receipt('real-client-upload')).id,receipt.id)
  assert.equal(reconnected.state.projection.state.tasks.length,1)
  assert.ok(reconnected.state.projection.sequence>snapshot.sequence)
  report.checks.push('restart_preserves_identity_and_receipt_without_replay')
  report.resource_id=accepted.resource_id ?? snapshot.state.resources[0].resource_id
  report.status='passed';report.generation='not_run_model_unavailable'
} catch(error) {
  report.status='failed';report.error=error instanceof assert.AssertionError?'assertion_failed':error.message
  process.exitCode=1
} finally {
  for(const connection of connections)await connection.stop()
  for(const child of children)await terminate(child)
  for(const store of stores)store.close()
  await rm(dir,{recursive:true,force:true})
  await writeFile(process.env.DDP_CLIENT_REPORT || '/tmp/ddp-v3-client-real-report.json',JSON.stringify(report,null,2)+'\n')
  console.log(JSON.stringify(report))
}
