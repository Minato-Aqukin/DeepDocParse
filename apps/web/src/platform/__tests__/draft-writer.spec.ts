import { describe, expect, it } from 'vitest'
import { DraftWriter } from '../draft-writer'

function deferred<T>() { let resolve!: (value: T) => void, reject!: (error: Error) => void; const promise = new Promise<T>((a,b) => {resolve=a;reject=b}); return { promise, resolve, reject } }
describe('renderer draft delivery', () => {
  it('delivers newer edits before earlier acknowledgments, so reload cannot discard a renderer queue', async () => {
    const replies = [deferred<number>(),deferred<number>()], sent: unknown[] = [], states: boolean[] = []
    const writer = new DraftWriter(4, (revision,value) => {sent.push([revision,value]);return replies[sent.length-1]!.promise}, state=>states.push(!state.pending && !state.error))
    const first=writer.write('old'), second=writer.write('latest')
    expect(sent).toEqual([[4,'old'],[5,'latest']]);expect(writer.durable).toBe(false)
    replies[1]!.resolve(6);replies[0]!.resolve(5);await Promise.all([first,second])
    expect(writer.durable).toBe(true);expect(states.at(-1)).toBe(true)
  })
  it('does not silently rebase after a conflict or uncertain persistence failure', async () => {
    const pending=deferred<number>(),sent: unknown[]=[]
    const writer=new DraftWriter(2, (revision,value)=>{sent.push([revision,value]);return pending.promise},()=>{})
    const write=writer.write('mine');pending.reject(new Error('draft_conflict'));await write
    await writer.write('newer mine');expect(sent).toEqual([[2,'mine']]);expect(writer.durable).toBe(false)
  })
  it('rejects an unexpected acknowledgment revision and fences further writes', async()=>{
    let calls=0;const writer=new DraftWriter(2,async()=>{calls++;return 100},()=>{})
    await writer.write('mine');await writer.write('next');expect(calls).toBe(1);expect(writer.durable).toBe(false)
  })
})
