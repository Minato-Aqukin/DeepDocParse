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
  /** Named local read: LOCAL connections only (content reads, and every center read, go through the /api proxy). */
  clientQuery(input: { connectionId: string; name: 'models.list'; payload: Json }): Promise<Result<Json>>
  /** Local model management only; content writes go through the /api proxy. */
  clientCommand(input: { connectionId: string; name: 'models.install' | 'models.start' | 'models.stop';
    payload: Json; idempotencyKey: string }): Promise<Result<Json>>
  clientReceipt(input: { connectionId: string; idempotencyKey: string }): Promise<Result<Json | null>>
  clientReadDraft(input: { connectionId: string; key: string }): Promise<Result<{ revision: number; value: Json } | null>>
  clientSaveDraft(input: { connectionId: string; key: string; expectedRevision: number; value: Json }): Promise<Result<{ revision: number }>>
}
export type PlanPhase = 'exploration' | 'execution'
/** Plan view plus the runtime's persisted federation mirror and a host-side rehash of the local result. */
export interface PlanDetail {
  plan: Json
  federation: Json | null
  verification: { state: 'passed' | 'failed' | 'unavailable'; expected: string | null; actual: string | null }
  transfer?: { state: string; uploadedBytes: number; totalBytes: number }
}
/**
 * Remote plan flow through the owned local runtime's consent ledger. Connection IDs name a
 * local workspace connection and a paired center; no TaskSpec, plan, node, endpoint, path or
 * credential crosses this boundary. Approval additionally requires a native host dialog.
 */
export interface DesktopClientBridge {
  clientPlanPropose(input: { connectionId: string; centerConnectionId: string; query: string;
    purpose?: 'answer' | 'wiki'; wiki?: { title: string; max_pages?: number };
    template?: 'center_only' | 'trusted_federation'; participantConnectionIds?: string[];
    inputs: { ref: string; digest: string; sizeBytes: number }[]; retention: 'temporary' | 'task_pinned';
    validMinutes: number; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanProposeFile(input: { connectionId: string; centerConnectionId: string; filename: string;
    inputs: [{ ref: string; digest: string; sizeBytes: number }]; retention: 'temporary' | 'task_pinned';
    validMinutes: number; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanList(input: { connectionId: string }): Promise<Result<Json>>
  clientPlanGet(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanReviewCenter(input: { connectionId: string; planId: string; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanApprove(input: { connectionId: string; planId: string; phase: PlanPhase; scopeDigest: string;
    userConfirmed: true; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanRevoke(input: { connectionId: string; planId: string; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanCancel(input: { connectionId: string; planId: string; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanDispatch(input: { connectionId: string; planId: string; phase: PlanPhase; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanResume(input: { connectionId: string; planId: string; idempotencyKey: string }): Promise<Result<Json>>
  clientPlanReconcile(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanFetchDelivery(input: { connectionId: string; planId: string }): Promise<Result<PlanDetail>>
  clientPlanConfirmDelivery(input: { connectionId: string; planId: string; deliveryId: string;
    resultManifestDigest: string; idempotencyKey: string }): Promise<Result<Json>>
}
/**
 * Single active data source for the host /api proxy (DESKTOP-APPSHELL-PLAN wave 1).
 * sourceId IS the ConnectionSummary.connectionId verbatim. After a successful
 * sourceActivate/workspaceOpen/centerConnect the RENDERER calls location.reload();
 * the host only records the active source (persisted in userData).
 */
export type SourceKind = 'local' | 'center'
export type SourceState = 'ready' | 'connecting' | 'signed_out' | 'unavailable'
export type SourceFeature = 'resources' | 'documents' | 'search' | 'wiki' | 'federation_tasks'
export interface SourceSummary {
  sourceId: string        // = existing connectionId
  kind: SourceKind
  label: string           // local: directory name; center: endpoint host
  state: SourceState
  readOnly: boolean       // center: true, local: false
  features: SourceFeature[]
  active: boolean
  reason: string | null   // contract error code when state != ready
  /** Delivery-center matching against plan transport_bindings (six-field rule); label never a match key. */
  environment: { environmentId: string; workspaceId: string; authorityNodeId: string }
  profile: Profile
}
export interface DesktopSourceBridge {
  sourceList(): Promise<Result<SourceSummary[]>>
  sourceActivate(input: { sourceId: string }): Promise<Result<SourceSummary>>
  sourceReconnect(input: { sourceId: string }): Promise<Result<SourceSummary>> // centers only; active source unchanged
  sourceRemove(input: { sourceId: string }): Promise<Result<null>>          // never deletes local workspace data (T65)
  workspaceOpen(): Promise<Result<SourceSummary | null>>                     // native directory dialog → start runtime → connect → activate; null = cancelled
  centerConnect(input: { endpoint: string; username: string; password: string; persist: boolean; storageOrigin?: string }): Promise<Result<SourceSummary>>
  onSourceChange(listener: (sources: SourceSummary[]) => void): () => void
}
