import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { createHash, generateKeyPairSync, sign } from 'node:crypto'
import { Connection, MemoryProjectionStore } from '../src/index.ts'
import { HttpProvider } from '../src/http-provider.ts'

const keys=generateKeyPairSync('ed25519'),keyBytes=Buffer.from(keys.publicKey.export({format:'jwk'}).x,'base64url')
const node = 'node-'+createHash('sha256').update(keyBytes).digest('hex').slice(0,48)
const profile = {profileId:'owner',issuer:node,subject:'user'}
const shape = {environment_id:node,authority_node_id:node,workspace_id:'workspace'}
const handshake = {protocol_version:'ddp-client/1',identity:shape,profile:{issuer:node,subject:'user'},
  capabilities:['client.snapshot','client.events','client.receipt']}
const p = sequence => ({sequence,cursor:`cursor-${sequence}`,state:{tasks:[]}})
const secret = 'test-only-secret-long-enough'
const tick = () => new Promise(resolve=>setTimeout(resolve,5))
async function until(fn) {for(let i=0;i<300;i++){if(fn())return;await tick()}assert.fail('not settled')}
async function server(t, handler) {
  const seen=[]
  const http=createServer(async(req,res)=>{
    seen.push({url:req.url,authorization:req.headers.authorization,cookie:req.headers.cookie})
    try {
      const custom=await handler?.(req,res)
      if (custom || res.writableEnded) return
      res.setHeader('Content-Type','application/json')
      const url=new URL(req.url,'http://local')
      if(url.pathname==='/api/v1/federation/node') {
        const issued=Date.now()
        const proof={schema:'ddp-node-proof/1',nonce:url.searchParams.get('challenge'),node_id:node,
          endpoint:`http://127.0.0.1:${http.address().port}`,
          issued_at:new Date(issued).toISOString(),expires_at:new Date(issued+60000).toISOString()}
        const signed=JSON.stringify([proof.schema,proof.nonce,proof.node_id,proof.endpoint,proof.issued_at,proof.expires_at])
        proof.signature=sign(null,Buffer.from(signed),keys.privateKey).toString('base64url')
        res.end(JSON.stringify({authority_node_id:node,public_key:keyBytes.toString('base64'),proof}))
      }
      else if(url.pathname==='/api/v1/client/handshake')res.end(JSON.stringify(handshake))
      else if(url.pathname==='/api/v1/client/snapshot')res.end(JSON.stringify(p(0)))
      else if(url.pathname==='/api/v1/client/events')res.end(JSON.stringify({events:[{...p(0),previous_sequence:0}]}))
      else if(url.pathname.startsWith('/api/v1/client/receipts/')){res.statusCode=404;res.end('{}')}
      else {res.statusCode=404;res.end(JSON.stringify({error:{code:'not_found'}}))}
    } catch {res.statusCode=500;res.end('{}')}
  })
  await new Promise(resolve=>http.listen(0,'127.0.0.1',resolve))
  t.after(()=>{http.closeAllConnections();return new Promise(resolve=>http.close(resolve))})
  return {seen,environment:{environmentId:node,authorityNodeId:node,workspaceId:'workspace',endpoint:`http://127.0.0.1:${http.address().port}`}}
}
function connection(t, env, extra={}) {
  const provider=new HttpProvider({ownedLoopback:true,credential:async()=>secret,pollMs:50,...extra})
  const c=new Connection(env,profile,provider,new MemoryProjectionStore(),{maxRetries:0})
  t.after(()=>c.stop());return {c,provider}
}

test('typed local reads use fixed endpoints without command keys or automatic query replay',async t=>{
  const requests=[]
  const f=await server(t,async(req,res)=>{
    if(['/api/v1/search','/api/v1/evidence/abc123','/api/v1/models'].includes(req.url)) {
      requests.push({url:req.url,key:req.headers['idempotency-key'],method:req.method})
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify({items:[]}));return true
    }
  })
  const {c}=connection(t,f.environment,{localCommands:true});c.start();await until(()=>c.state.transport==='ready')
  await c.query('corpus.search',{query:'specific fact'})
  await c.query('evidence.get',{evidence_id:'abc123'})
  await c.query('models.list',{})
  await assert.rejects(c.query('evidence.get',{evidence_id:'https://other',url:'untrusted'}),{code:'unsupported_operation'})
  await c.wake();await until(()=>c.state.transport==='ready')
  assert.deepEqual(requests,[{url:'/api/v1/search',key:undefined,method:'POST'},
    {url:'/api/v1/evidence/abc123',key:undefined,method:'GET'},
    {url:'/api/v1/models',key:undefined,method:'GET'}])
})

test('Wiki metadata, immutable revisions and CAS edits use fixed routes and preserve body and write keys',async t=>{
  const calls=[]
  const f=await server(t,async(req,res)=>{
    if(req.url.startsWith('/api/v1/wikis')) {
      const chunks=[];for await(const chunk of req)chunks.push(chunk)
      calls.push({url:req.url,method:req.method,key:req.headers['idempotency-key'],body:chunks.length?JSON.parse(Buffer.concat(chunks)):null})
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify({task_id:'wiki-task',items:[]}));return true
    }
  })
  const {c}=connection(t,f.environment,{localCommands:true});c.start();await until(()=>c.state.transport==='ready')
  await c.query('wiki.list',{cursor:'frozen&scope=other',limit:2})
  await c.query('wiki.get',{wiki_id:'wiki-1',revision_id:'revision-1'})
  await c.query('wiki.revisions',{wiki_id:'wiki-1',limit:2})
  const body={title:'Manual',sources:[{resource_id:'r',source_version_id:'v'}],execution_policy:'local_only',allow_remote:false}
  await c.execute('wiki.create',{body},'create-key')
  await c.execute('wiki.rebuild',{wiki_id:'wiki-1',body:{...body,base_revision_id:'revision-1'}},'rebuild-key')
  const edited={base_revision_id:'revision-1',paragraphs:[{id:'human-1',text:'Human correction'}]}
  await c.execute('wiki.edit',{wiki_id:'wiki-1',page_key:'page-1',body:edited},'edit-key')
  assert.deepEqual(calls.map(v=>[v.method,v.url,v.key]),[
    ['GET','/api/v1/wikis?cursor=frozen%26scope%3Dother&limit=2',undefined],
    ['GET','/api/v1/wikis/wiki-1/revisions/revision-1',undefined],
    ['GET','/api/v1/wikis/wiki-1/revisions?limit=2',undefined],
    ['POST','/api/v1/wikis','create-key'],['POST','/api/v1/wikis/wiki-1/revisions','rebuild-key'],
    ['PATCH','/api/v1/wikis/wiki-1/pages/page-1','edit-key']])
  assert.deepEqual(calls[5].body,edited)
  await assert.rejects(c.query('wiki.get',{wiki_id:'../private'}),{code:'unsupported_operation'})
  const remote=connection(t,f.environment).c;remote.start();await until(()=>remote.state.transport==='ready')
  await assert.rejects(remote.execute('wiki.create',{body},'remote-key'),{code:'approved_plan_required'})
  assert.equal(calls.length,6)
})

test('model installation and startup require explicit commands with stable keys and fixed catalog IDs',async t=>{
  const calls=[]
  const f=await server(t,async(req,res)=>{
    if(req.url.startsWith('/api/v1/models/')) {
      calls.push([req.url,req.headers['idempotency-key']]);res.setHeader('Content-Type','application/json')
      res.end(JSON.stringify({task_id:'model-task',status:'accepted'}));return true
    }
  })
  const {c}=connection(t,f.environment,{localCommands:true});c.start();await until(()=>c.state.transport==='ready')
  assert.deepEqual(calls,[])
  await c.execute('models.install',{model_id:'qwen3-1.7b-q8_0'},'install-0001')
  await c.execute('models.start',{model_id:'qwen3-1.7b-q8_0'},'startup-0001')
  await c.wake();await until(()=>c.state.transport==='ready')
  assert.deepEqual(calls,[['/api/v1/models/qwen3-1.7b-q8_0/install','install-0001'],['/api/v1/models/qwen3-1.7b-q8_0/start','startup-0001']])
  await assert.rejects(c.execute('models.install',{model_id:'../../arbitrary',url:'https://untrusted'},'invalid-0001'),{code:'unsupported_operation'})
  assert.equal(calls.length,2)
})

test('center queries require advertised interfaces and preserve empty version scope without dispatching model operations',async t=>{
  const dispatched=[]
  const f=await server(t,async(req,res)=>{
    if(req.url==='/api/v1/client/handshake') {
      res.setHeader('Content-Type','application/json')
      res.end(JSON.stringify({...handshake,capabilities:[...handshake.capabilities,'client.query','client.windows']}));return true
    }
    if(req.url==='/api/v1/client/query') {
      let body='';for await(const chunk of req)body+=chunk
      dispatched.push(JSON.parse(body));res.setHeader('Content-Type','application/json');res.end(JSON.stringify({hits:[]}));return true
    }
  })
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='ready')
  await c.query('corpus.search',{query:'only selected sources',version_ids:[]})
  await c.query('resource.page',{snapshot_id:'fixed-snapshot',cursor:'opaque-cursor'})
  await assert.rejects(c.query('models.list',{}),{code:'unsupported_operation'})
  await assert.rejects(c.execute('models.start',{model_id:'qwen3-1.7b-q8_0'},'remote-model-001'),{code:'approved_plan_required'})
  assert.deepEqual(dispatched,[{name:'corpus.search',payload:{query:'only selected sources',version_ids:[]}},
    {name:'resource.page',payload:{snapshot_id:'fixed-snapshot',cursor:'opaque-cursor'}}])
})

test('real HTTP handshake checks public node first, then authenticated workspace and profile',async t=>{
  const f=await server(t),{c}=connection(t,f.environment)
  c.start();await until(()=>c.state.transport==='ready')
  assert.match(f.seen[0].url,/^\/api\/v1\/federation\/node\?challenge=[A-Za-z0-9_-]{32}$/);assert.equal(f.seen[0].authorization,undefined)
  assert.equal(f.seen[1].authorization,`Bearer ${secret}`);assert.equal(f.seen[1].cookie,undefined)
  assert.equal(c.state.projection.cursor,'cursor-0')
  assert.equal(await c.receipt('unknown-key'),null)
})

test('a different node at the saved address never receives credentials',async t=>{
  const f=await server(t,(req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({authority_node_id:'impostor'}));return true})
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'identity_mismatch');assert.equal(f.seen.length,1)
  assert.equal(f.seen[0].authorization,undefined)
})

test('authenticated redirect cannot forward a token to another listener',async t=>{
  const stolen=[]
  const target=await server(t,(req,res)=>{stolen.push(req.headers.authorization);res.end('{}');return true})
  const f=await server(t,(req,res)=>{if(req.url.endsWith('/handshake')){res.writeHead(302,{Location:target.environment.endpoint});res.end();return true}})
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.deepEqual(stolen,[]);assert.equal(c.state.reason,'connection_failed')
})

test('a chat-only service cannot impersonate the complete corpus API',async t=>{
  const f=await server(t,(req,res)=>{if(req.url.endsWith('/handshake')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({choices:[{message:{content:'hello'}}]}));return true}})
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'protocol_incompatible');assert.equal(f.seen.length,2)
})

test('an unknown handshake protocol version is rejected before authoritative data is applied',async t=>{
  const f=await server(t,(req,res)=>{
    if(req.url==='/api/v1/client/handshake'){
      res.setHeader('Content-Type','application/json')
      res.end(JSON.stringify({...handshake,protocol_version:'ddp-client/99'}));return true
    }
  })
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'protocol_incompatible')
  assert.equal(c.state.projection,null)
  assert.equal(f.seen.some(item=>item.url.startsWith('/api/v1/client/snapshot')),false,
    'an incompatible node must not receive the authoritative snapshot request')
})

test('an old service missing a required client capability is rejected before snapshot',async t=>{
  for(const missing of ['client.snapshot','client.events','client.receipt']){
    const f=await server(t,(req,res)=>{
      if(req.url==='/api/v1/client/handshake'){
        res.setHeader('Content-Type','application/json')
        res.end(JSON.stringify({...handshake,
          capabilities:handshake.capabilities.filter(value=>value!==missing)}));return true
      }
    })
    const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
    assert.equal(c.state.reason,'protocol_incompatible',missing)
    assert.equal(c.state.projection,null)
    assert.equal(f.seen.some(item=>item.url.startsWith('/api/v1/client/snapshot')),false,
      `missing ${missing} must block before the snapshot request`)
  }
})

test('a scoped batch advances all covered events without treating the skipped sequence numbers as missing',async t=>{
  const f=await server(t,(req,res)=>{if(req.url.includes('/events?')){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({events:[{...p(5),previous_sequence:req.url.endsWith('cursor-0')?0:5}]}));return true}})
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.projection?.sequence===5)
  assert.equal(c.state.transport,'ready');assert.equal(c.state.snapshot,'current')
})

test('a write with a lost response stays unknown until receipt reconciliation, across reconnect',async t=>{
  let writes=0
  const f=await server(t,(req,res)=>{
    if(req.url==='/api/v1/wiki'){writes++;assert.equal(req.headers['idempotency-key'],'stable-key');req.socket.destroy();return true}
    if(req.url==='/api/v1/client/receipts/stable-key'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({task_id:'accepted-before-response-loss'}));return true}
  })
  const {c}=connection(t,f.environment,{localCommands:true});c.start();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',{query:'q'},'stable-key'))
  await c.wake();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',{query:'q'},'stable-key'));assert.equal(writes,1)
  assert.deepEqual(await c.receipt('stable-key'),{task_id:'accepted-before-response-loss'})
  assert.deepEqual(await c.execute('wiki.build',{query:'q'},'stable-key'),{task_id:'accepted-before-response-loss'})
  assert.equal(writes,1)
})

test('remote command dispatch requires an approved plan and cannot use the local convenience API',async t=>{
  const f=await server(t),{c}=connection(t,f.environment)
  c.start();await until(()=>c.state.transport==='ready')
  await assert.rejects(c.execute('wiki.build',{query:'q'},'stable-key'),/approved_plan_required/)
  assert.equal(f.seen.some(item=>item.url==='/api/v1/wiki'),false)
})

test('copied public node metadata without the private signing key never releases a credential',async t=>{
  const f=await server(t,(req,res)=>{
    const url=new URL(req.url,'http://local')
    const proof={schema:'ddp-node-proof/1',nonce:url.searchParams.get('challenge'),node_id:node,
      endpoint:f.environment.endpoint,issued_at:new Date().toISOString(),expires_at:new Date(Date.now()+60000).toISOString(),
      signature:Buffer.alloc(64).toString('base64url')}
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify({authority_node_id:node,public_key:keyBytes.toString('base64'),proof}));return true
  })
  const {c}=connection(t,f.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'identity_mismatch');assert.equal(f.seen.length,1);assert.equal(f.seen[0].authorization,undefined)
})

test('relaying a legitimate proof for another endpoint cannot authorize this recipient',async t=>{
  const honest=await server(t)
  const relay=await server(t,async(req,res)=>{
    const data=await(await fetch(honest.environment.endpoint+req.url)).json()
    res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));return true
  })
  const {c}=connection(t,relay.environment);c.start();await until(()=>c.state.transport==='blocked')
  assert.equal(c.state.reason,'identity_mismatch');assert.equal(relay.seen.length,1)
  assert.equal(relay.seen[0].authorization,undefined)
})
