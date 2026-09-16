/** Renderer boundary; importing these types grants no Node, HTTP or filesystem capability. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
export interface Environment { environmentId: string; workspaceId: string; authorityNodeId: string; endpoint: string }
export interface Profile { profileId: string; issuer: string; subject: string }
export interface ClientView {
  transport: 'disconnected' | 'connecting' | 'authenticating' | 'ready' | 'backoff' | 'blocked'
  snapshot: 'loading' | 'current' | 'stale' | 'failed'
  reason: string | null
  projection: { cursor: string; sequence: number; state: Json } | null
}
export interface ConnectionSummary {
  connectionId: string; kind: 'local' | 'remote'; label: string
  environment: Omit<Environment, 'endpoint'>; profile: Profile
  workspaceId: string | null // Opaque native workspace handle, not authoritative workspace identity.
  revision: number; view: ClientView
}
export type Result<T> = { ok: true; value: T } | { ok: false; error: { code: string } }
export interface ClientViewEvent { subscriptionId: string; connectionId: string; revision: number; view: ClientView }
export interface DesktopClientBridge {
  clientList(): Promise<Result<ConnectionSummary[]>>
  clientConnectLocal(input: { workspaceId: string }): Promise<Result<ConnectionSummary>>
  clientPairRemote(input: { environment: Environment; profile: Profile; label: string }): Promise<Result<ConnectionSummary>>
  clientWake(input: { connectionId: string }): Promise<Result<ConnectionSummary>>
  clientDisconnect(input: { connectionId: string }): Promise<Result<ConnectionSummary>>
  clientSubscribe(input: { connectionId: string; subscriptionId: string }): Promise<Result<ConnectionSummary>>
  clientUnsubscribe(input: { subscriptionId: string }): Promise<Result<null>>
  /** Register before subscribe; discard older revisions and events for inactive subscriptions. */
  onClientView(listener: (event: ClientViewEvent) => void): () => void
  clientCommand(input: { connectionId: string; name: 'task.cancel' | 'answer.generate' | 'wiki.build' | 'models.install' | 'models.start' | 'models.stop' | 'wiki.create' | 'wiki.rebuild' | 'wiki.edit';
    payload: Json; idempotencyKey: string }): Promise<Result<Json>>
  clientReceipt(input: { connectionId: string; idempotencyKey: string }): Promise<Result<Json | null>>
  clientReadDraft(input: { connectionId: string; key: string }): Promise<Result<{ revision: number; value: Json } | null>>
  clientSaveDraft(input: { connectionId: string; key: string; expectedRevision: number; value: Json }): Promise<Result<{ revision: number }>>
}
export interface DesktopClientBridge {
  clientQuery(input: { connectionId: string; name: 'corpus.search' | 'evidence.get' | 'models.list' | 'resource.page' | 'task.page' | 'wiki.list' | 'wiki.get' | 'wiki.revisions'; payload: Json }): Promise<Result<Json>>
  clientImportFile(input: { connectionId: string; kind: 'pdf' | 'bundle'; idempotencyKey: string }): Promise<Result<Json | null>>
  clientExportBundle(input: { connectionId: string; versionId: string }): Promise<Result<{ saved: boolean }>>
  clientReadOriginal(input: { connectionId: string; versionId: string }): Promise<Result<Uint8Array>>
}
export type PlanPhase = 'exploration' | 'execution'
/** Plan view plus the runtime's persisted federation mirror and a host-side rehash of the local result. */
export interface PlanDetail {
  plan: Json
  federation: Json | null
  verification: { state: 'passed' | 'failed' | 'unavailable'; expected: string | null; actual: string | null }
}
/**
 * Remote plan flow through the owned local runtime's consent ledger. Connection IDs name a
 * local workspace connection and a paired center; no TaskSpec, plan, node, endpoint, path or
 * credential crosses this boundary. Approval additionally requires a native host dialog.
 */
export interface DesktopClientBridge {
  clientPlanPropose(input: { connectionId: string; centerConnectionId: string; query: string;
    inputs: { ref: string; digest: string; sizeBytes: number }[]; retention: 'temporary' | 'task_pinned';
    validMinutes: number; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanList(input: { connectionId: string }): Promise<Result<Json>>
  clientPlanGet(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanApprove(input: { connectionId: string; planId: string; phase: PlanPhase; scopeDigest: string;
    userConfirmed: true; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanRevoke(input: { connectionId: string; planId: string; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanDispatch(input: { connectionId: string; planId: string; phase: PlanPhase; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanReconcile(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanFetchDelivery(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanConfirmDelivery(input: { connectionId: string; planId: string; deliveryId: string;
    resultManifestDigest: string; idempotencyKey: string }): Promise<Result<Json>>
}
