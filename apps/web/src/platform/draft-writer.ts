import type { Json } from './desktop'

/** The trusted host processes draft CAS synchronously in IPC arrival order.
 * Send each edit now; waiting for an earlier acknowledgment leaves unsent text
 * inside the renderer and loses it on reload. Predicted revisions preserve CAS:
 * any failed write fences this writer until a fresh read, never rebasing blindly.
 */
export class DraftWriter {
  private next: number
  private pending = new Set<Promise<void>>()
  private failure: unknown = null
  constructor(revision: number, private save: (expectedRevision: number, value: Json) => Promise<number>,
    private changed: (state: { pending: boolean; error: unknown }) => void) { this.next = revision }

  write(value: Json): Promise<void> {
    if (this.failure) return this.flush()
    if (this.pending.size >= 128) { this.failure = new Error('cache_failure'); this.notify(); return this.flush() }
    const expected = this.next++
    let request: Promise<number>
    try { request = this.save(expected, value) }
    catch (error) { this.failure = error; this.notify(); return this.flush() }
    const finished = request.then(actual => {
      if (actual !== expected + 1) throw new Error('draft_conflict')
    }).catch(error => { this.failure ??= error }).finally(() => { this.pending.delete(finished); this.notify() })
    this.pending.add(finished); this.notify()
    return this.flush()
  }
  private notify() { this.changed({ pending: this.pending.size > 0, error: this.failure }) }
  async flush(): Promise<void> { await Promise.all([...this.pending]) }
  get durable() { return this.pending.size === 0 && this.failure === null }
}
