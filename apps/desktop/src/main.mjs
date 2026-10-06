import { app, BrowserWindow, Menu, dialog, ipcMain, protocol, session, safeStorage, powerMonitor } from 'electron'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { readdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay, setImmediate as nextTurn } from 'node:timers/promises'
import { CHANNELS, HostError, validate, uiLocation, authorizeSender, isUI, smokeLocalRuntimeFailure,
  allowRequest, contentSecurityPolicy, safeFailure } from './policy.mjs'
import { platformName, secureDirectory } from './platform.mjs'
import { CredentialBroker } from './credentials.mjs'
import { WorkspaceHandles } from './workspaces.mjs'
import { OwnedRuntimeManager } from './runtime.mjs'
import { createRuntimeBackend } from './runtime-backends.mjs'
import { staticUI } from './static-ui.mjs'
import { ClientHost, clientFailure } from './client-host.mjs'
import { CLIENT_CHANNELS, clientArguments } from './client-policy.mjs'

const source = path.dirname(fileURLToPath(import.meta.url))
const repository = path.resolve(source, '../../..')
// These configuration values are host startup inputs, never renderer inputs.
const smoke = process.env.DDP_DESKTOP_SMOKE === '1' && (!app.isPackaged || process.argv.includes('--smoke'))
if (smoke && process.env.DDP_DESKTOP_SMOKE_DIRECTORY) app.setPath('userData', process.env.DDP_DESKTOP_SMOKE_DIRECTORY)
const ui = uiLocation(app.isPackaged ? undefined : process.env.DDP_DESKTOP_DEV_URL)
protocol.registerSchemesAsPrivileged([{ scheme: 'ddp', privileges: {
  standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true,
} }])
app.enableSandbox()
app.commandLine.appendSwitch('disable-background-networking')
app.commandLine.appendSwitch('disable-component-update')
// BrowserContext seeds a dictionary before any renderer exists. Disable it for
// every session at creation, including Electron's otherwise unused default one.
app.on('session-created', created => {
  created.setSpellCheckerLanguages([])
  created.setSpellCheckerEnabled(false)
})
app.setName('DeepDocParse')
let window, runtime, credentials, clients, quitting = false, quitInProgress = false, smokeResult
let localRuntimeKind = 'native'
const WSL_WORKSPACE_DIRECTORY = '~/.deepdocparse/workspaces/default'
const workspaces = new WorkspaceHandles()
if (!app.requestSingleInstanceLock()) app.exit(0)
app.on('second-instance', () => {
  if (window && !window.isDestroyed()) { if (window.isMinimized()) window.restore(); window.show(); window.focus() }
})

async function runtimeConfiguration() {
  // Windows never runs a native Python: local mode is the bundled Linux runtime
  // inside the user's WSL2 distribution. Remote mode needs none of this.
  if (process.platform === 'win32') {
    const archive = `deepdocparse-wsl-runtime-${app.getVersion()}-linux-x64.tar.gz`
    return { kind: 'wsl', distro: process.env.DDP_WSL_DISTRO || null,
      runtimeRoot: '~/.deepdocparse/runtime',
      runtimeArchive: path.join(app.isPackaged ? process.resourcesPath : path.join(repository, 'dist/wsl'), archive),
      runtimeManifest: app.isPackaged ? path.join(process.resourcesPath, 'runtime/wsl-runtime.json')
        : path.join(repository, 'dist/wsl/wsl-runtime.json') }
  }
  if (app.isPackaged) return { python: '/usr/bin/python3',
    pythonPaths: [path.join(process.resourcesPath, 'runtime/site-packages')], cwd: process.resourcesPath }
  const library = path.join(repository, '.venv/lib')
  const versions = (await readdir(library)).filter(name => /^python\d+\.\d+$/.test(name))
  if (versions.length !== 1) throw new HostError('development_python_ambiguous')
  return { python: path.join(repository, '.venv/bin/python'), cwd: repository,
    pythonPaths: ['ddp_local', 'ddp_core', 'ddp_contracts'].map(name => path.join(repository, 'python', name))
      .concat(path.join(library, versions[0], 'site-packages')) }
}

async function chooseWorkspace() {
  if (localRuntimeKind === 'wsl') return workspaces.selectedWsl({ directory: WSL_WORKSPACE_DIRECTORY })
  const selected = await dialog.showOpenDialog(window, { title: '选择本地工作区',
    properties: ['openDirectory', 'createDirectory'], buttonLabel: '选择工作区' })
  if (selected.canceled || selected.filePaths.length !== 1) return null
  return workspaces.selectedByNativeDialog(selected.filePaths[0])
}

async function requestQuit() {
  if (quitting || quitInProgress) return
  quitInProgress = true
  try {
    if (!smoke && (runtime?.activeCount() || clients?.activeTransferCount())) {
      const answer = await dialog.showMessageBox(window, { type: 'question', title: '退出 DeepDocParse',
        message: '退出会停止本机任务和当前传输', detail: '已受理的远端任务会继续运行。重新打开后可对账并显式续传；未知结果不会自动重发。',
        buttons: ['继续使用', '停止本机任务并退出'], defaultId: 0, cancelId: 0, noLink: true })
      if (answer.response !== 1) return
    }
    await clients?.close()
    await runtime?.shutdown()
    credentials?.clearSession()
    if (smokeResult) {
      smokeResult.report.stopped = runtime.status(smokeResult.workspaceId)
      await writeFile(path.join(smokeResult.directory, 'report.json'), JSON.stringify(smokeResult.report, null, 2))
    }
    // Promise continuations can run before Electron's native close callback
    // unwinds. Re-entering quit there closes the window but cancels app shutdown.
    await nextTurn()
    quitting = true
    app.quit()
  } finally { quitInProgress = false }
}

app.on('before-quit', event => { if (!quitting) { event.preventDefault(); void requestQuit() } })
app.on('window-all-closed', () => { void requestQuit() })
app.whenReady().then(async () => {
  const privateRoot = path.join(app.getPath('userData'), 'host')
  credentials = new CredentialBroker({ directory: path.join(privateRoot, 'credentials'), safeStorage })
  const configuration = await runtimeConfiguration()
  const runtimeDirectory = path.join(privateRoot, 'runtime')
  const launcher = path.join(source, 'runtime-launcher.py')
  const runtimeKind = configuration.kind ?? 'native'
  localRuntimeKind = runtimeKind
  let backend = null, localRuntimeFailure = null, orphanCleanupFailure = null
  try {
    backend = await createRuntimeBackend({ ...configuration, kind: runtimeKind,
      directory: runtimeDirectory, launcher })
  } catch (error) {
    // A missing WSL backend must leave this host usable: it is reported as an
    // unavailable local runtime, never as a startup crash or a fake ready state.
    localRuntimeFailure = error instanceof HostError ? error
      : new HostError(runtimeKind === 'wsl' ? 'wsl_backend_unavailable' : 'local_runtime_unavailable')
  }
  if (backend && typeof backend.cleanupOrphans === 'function') {
    try { await backend.cleanupOrphans() } catch (error) {
      orphanCleanupFailure = error instanceof HostError ? error : new HostError('wsl_backend_unavailable')
    }
  }
  runtime = new OwnedRuntimeManager({ workspaces, directory: runtimeDirectory, launcher,
    ...configuration, ...(backend ? { backend } : {}) })
  clients = await new ClientHost({ workspaces, runtime, credentials, directory: path.join(privateRoot, 'client'),
    localRuntimeFailure,
    // Unpackaged (dev) builds accept http://127.0.0.1 centers, matching source-policy.
    loopbackCenters: !app.isPackaged,
    // A grant needs a user action the renderer cannot script: a native dialog that
    // restates the stored scope (recipient, endpoint, exact payload bytes, expiry).
    confirmApproval: async summary => {
      const payloads = summary.payloads.map(item => `  ${item.kind} → ${item.recipient}（${item.bytes} 字节，${String(item.digest).slice(0, 19)}…）`)
      const transports = summary.transports.map(item => `  ${item.recipient} · ${item.endpoint}`)
      const sendsOriginal = summary.payloads.some(item => item.kind === 'source_files')
      const exploration = summary.exploration ? [`探索允许接收方：${summary.exploration.recipients.join('、')}`,
        `探索允许载荷：${summary.exploration.payloads.join('、')}`, `探索预算 ${JSON.stringify(summary.exploration.budget)}`] : []
      const center = summary.centerExecution
      const graph = center ? [`中心任务 ${center.rootTaskId} · 父计划 ${center.parentPlanId}`,
        `中心计划摘要 ${center.planDigest}`, '执行步骤：',
        ...center.steps.map(step => `  ${step.step_id}: ${step.operation} → ${step.executor_node_id}（依赖 ${step.depends_on.join('、') || '无'}）`),
        '数据边：', ...center.dataEdges.map(edge => `  ${edge.edge_id}: ${edge.from_node_id} → ${edge.to_node_id} · ${edge.payload_kind} · 中继 ${(edge.relay_via ?? []).join('、') || '无'} · ${edge.retention}`),
        `中心总预算 ${JSON.stringify(center.budget)}`, `最终结果写入方 ${center.finalResultWriter}`] : []
      const answer = await dialog.showMessageBox(window, { type: 'warning', title: '批准外发',
        message: summary.phase === 'exploration' ? '批准探索：把以下内容发给中心，用于探测与规划' : '批准执行：按已审阅计划提交给中心',
        detail: [`计划 ${summary.planId}`, `内容：${summary.description}`, `范围摘要 ${summary.scopeDigest}`,
          '外发内容：', ...payloads, '接收方与实际存储地址：', ...transports,
          `本机输入 ${summary.inputs} 项（${sendsOriginal ? '此次执行将上传锁定的本机原件' : '当前阶段不上传本机原件'}）`,
          `保留策略 ${summary.retention} · 输出位置 ${summary.outputLocations.join('、')}`, `有效期至 ${summary.validUntil}`, ...exploration, ...graph].join('\n'),
        buttons: ['取消', '批准'], defaultId: 0, cancelId: 0, noLink: true })
      return answer.response === 1
    },
  }).initialize()
  // A restart restores the last active source: reconnect it in the background (the
  // renderer sees `connecting`, then `ready` or `unavailable`) instead of stranding it.
  if (!smoke) void clients.restoreActiveSource()
  const hostStatus = () => {
    const storage = credentials.policy()
    return { platform: process.platform, electron: process.versions.electron,
      version: app.getVersion(),
      secrets: storage, credentialStorage: storage,
      runtimeBackend: runtimeKind, runtimeAvailable: Boolean(backend),
      runtimeReason: localRuntimeFailure?.code ?? null,
      orphanCleanup: orphanCleanupFailure?.code ?? null,
      isolation: runtimeKind === 'wsl' && backend ? 'wsl_vm'
        : process.platform === 'win32' ? 'ntfs_acl' : 'posix_mode',
      lifecycle: 'close_stops_owned_local_tasks',
      connectionIntegration: 'shared_client_runtime' }
  }
  const operations = {
    hostStatus, selectWorkspace: chooseWorkspace,
    startLocal: ({ workspaceId }) => { if (localRuntimeFailure) throw localRuntimeFailure; return runtime.start(workspaceId) },
    stopLocal: async ({ workspaceId }) => { await clients.stopWorkspace(workspaceId); return runtime.stop(workspaceId) },
    runtimeStatus: ({ workspaceId }) => runtime.status(workspaceId),
    setCredential: args => credentials.set(args), credentialStatus: args => credentials.status(args),
    clearCredential: args => credentials.clear(args),
  }
  const isolated = session.fromPartition('ddp-workbench') // Memory partition: no web credentials/cookies persisted.
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  isolated.setPermissionCheckHandler(() => false)
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !allowRequest(details.url, ui) }))
  isolated.webRequest.onHeadersReceived((details, callback) => callback({ responseHeaders: {
    ...details.responseHeaders, 'Content-Security-Policy': [contentSecurityPolicy(ui)],
  } }))
  await isolated.protocol.handle('ddp', staticUI(app.isPackaged ? path.join(app.getAppPath(), 'ui')
    : path.join(repository, 'apps/web/dist'), ui, { clients: () => clients }))
  window = new BrowserWindow({ width: 1280, height: 860, minWidth: 860, minHeight: 640,
    title: 'DeepDocParse', show: false, webPreferences: { session: isolated,
      preload: path.join(source, 'preload.cjs'), sandbox: true, contextIsolation: true,
      nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
      spellcheck: false,
    } })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  window.webContents.on('will-navigate', (event, destination) => { if (!isUI(destination, ui)) event.preventDefault() })
  window.webContents.on('will-redirect', (event, destination) => { if (!isUI(destination, ui)) event.preventDefault() })
  window.on('close', event => { if (!quitting) { event.preventDefault(); void requestQuit() } })
  for (const [method, channel] of Object.entries(CHANNELS)) ipcMain.handle(channel, async (event, input) => {
    try {
      authorizeSender(event, window.webContents, ui)
      const argument = validate(method, input)
      return { ok: true, value: await operations[method](argument) }
    } catch (error) { return safeFailure(error) }
  })
  const clientOperations = {
    clientList: () => clients.list(), clientCommand: input => clients.command(input),
    clientQuery: input => clients.query(input), clientReceipt: input => clients.receipt(input),
    clientReadDraft: input => clients.readDraft(input), clientSaveDraft: input => clients.saveDraft(input),
    clientPlanPropose: input => clients.planPropose(input), clientPlanList: input => clients.planList(input),
    clientPlanProposeFile: input => clients.planProposeFile(input),
    clientPlanGet: input => clients.planGet(input), clientPlanApprove: input => clients.planApprove(input),
    clientPlanReviewCenter: input => clients.planReviewCenter(input),
    clientPlanRevoke: input => clients.planRevoke(input), clientPlanDispatch: input => clients.planDispatch(input),
    clientPlanCancel: input => clients.planCancel(input),
    clientPlanResume: input => clients.planResume(input),
    clientPlanReconcile: input => clients.planReconcile(input), clientPlanFetchDelivery: input => clients.planFetchDelivery(input),
    clientPlanConfirmDelivery: input => clients.planConfirmDelivery(input),
  }
  for (const [method, channel] of Object.entries(CLIENT_CHANNELS)) ipcMain.handle(channel, async (event, input) => {
    try {
      authorizeSender(event, window.webContents, ui)
      const argument = clientArguments(method, input)
      const value = await clientOperations[method](argument)
      return { ok: true, value }
    } catch (error) { return clientFailure(error) }
  })
  const { SOURCE_CHANNELS, sourceArguments } = await import('./source-policy.mjs')
  const sourceOperations = {
    sourceList: () => clients.sourceList(),
    sourceActivate: input => clients.sourceActivate(input),
    sourceReconnect: input => clients.sourceReconnect(input),
    sourceRemove: input => clients.sourceRemove(input),
    workspaceOpen: async () => {
      clients.selectWorkspace = chooseWorkspace
      return clients.workspaceOpen()
    },
    centerConnect: input => clients.centerConnect(input, { packaged: app.isPackaged }),
  }
  let sourceForwarder = null
  for (const [method, channel] of Object.entries(SOURCE_CHANNELS)) {
    if (method === 'onSourceChange') continue
    ipcMain.handle(channel, async (event, input) => {
      try {
        authorizeSender(event, window.webContents, ui)
        const argument = sourceArguments(method, input, { packaged: app.isPackaged })
        // workspaceOpen takes no renderer input; the native dialog runs host-side.
        const value = method === 'workspaceOpen' ? await sourceOperations.workspaceOpen()
          : await sourceOperations[method](argument)
        return { ok: true, value }
      } catch (error) { return clientFailure(error) }
    })
  }
  ipcMain.handle(SOURCE_CHANNELS.onSourceChange, async event => {
    authorizeSender(event, window.webContents, ui)
    if (!sourceForwarder) {
      sourceForwarder = clients.onSourceChange(summaries => {
        if (!window || window.isDestroyed()) return
        window.webContents.send('ddp:source-change', summaries)
      })
    }
    return { ok: true, value: null }
  })
  let selectedWorkspace
  const action = operation => async () => {
    try { await operation() } catch { await dialog.showMessageBox(window, {
      type: 'error', message: '操作未完成', detail: '请检查工作区及本地运行时状态后重试。',
    }) }
  }
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: '工作区', submenu: [
      { label: '选择本地工作区…', click: action(async () => { selectedWorkspace = await chooseWorkspace() }) },
      { label: '打开已选工作区', click: action(async () => {
        if (!selectedWorkspace) selectedWorkspace = await chooseWorkspace()
        if (selectedWorkspace) {
          clients.selectWorkspace = async () => selectedWorkspace
          const opened = await clients.workspaceOpen()
          if (opened) window.setTitle(`DeepDocParse · ${opened.label}`)
        }
      }) },
      { label: '停止已选工作区', click: action(async () => { if (selectedWorkspace) { await clients.stopWorkspace(selectedWorkspace.workspaceId); await runtime.stop(selectedWorkspace.workspaceId) } }) },
      { type: 'separator' }, { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => { void requestQuit() } },
    ] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '视图', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }] },
  ]))
  // logind sleeps arrive as two `suspend` and two `resume` events on Linux (T28 drill); each
  // transition starts after the previous one finished, so a repeat finds nothing left to stop.
  let power = Promise.resolve()
  const transition = step => {
    power = power.then(step).catch(error => { process.stderr.write(`DeepDocParse: power_transition_failed ${error?.code ?? ''}\n`) })
  }
  powerMonitor.on('suspend', () => transition(() => clients.suspend().then(() => runtime.suspend())))
  powerMonitor.on('resume', () => transition(() => runtime.resume().then(() => clients.resume())))
  await window.loadURL(ui.href)
  window.show()
  // Test instrumentation is reachable only from this trusted development startup, never IPC.
  if (smoke) {
    const directory = process.env.DDP_DESKTOP_SMOKE_DIRECTORY
    await secureDirectory(directory, { code: 'unsafe_smoke_directory' })
    const fonts = await window.webContents.executeJavaScript(`(async () => {
      const checks = [['16px Inter', 'Offline'], ['16px "IBM Plex Mono"', '0123'], ['16px "Noto Sans SC"', '离线证据']]
      return Promise.all(checks.map(async ([font, text]) => {
        const faces = await document.fonts.load(font, text)
        return { font, loaded: faces.length > 0 && faces.every(face => face.status === 'loaded') }
      }))
    })()`)
    if (fonts.some(font => !font.loaded)) throw new HostError('smoke_offline_font_failed')
    const renderer = await window.webContents.executeJavaScript(`(async () => ({
      nodeRequire: typeof require, nodeProcess: typeof process,
      methods: Object.keys(window.ddpDesktop ?? {}), host: await window.ddpDesktop.hostStatus(),
      rejected: await window.ddpDesktop.sourceActivate({ sourceId: 'not-a-source', shell: 'id' }),
      title: document.title, rendered: document.querySelector('#app')?.children.length ?? 0,
    }))()`)
    let selected
    if (localRuntimeKind === 'wsl') {
      selected = workspaces.selectedWsl({ directory: WSL_WORKSPACE_DIRECTORY })
    } else {
      const workspace = process.env.DDP_DESKTOP_SMOKE_WORKSPACE || path.join(directory, 'workspace')
      await secureDirectory(workspace, { code: 'unsafe_smoke_directory' })
      selected = await workspaces.selectedByNativeDialog(workspace)
    }
    // Tier A smoke: a Windows host without a WSL2 distribution cannot start
    // local mode, and that is a product state, not a host failure. Record the
    // honest capability reason and keep asserting the renderer/host boundary;
    // machines with WSL run the full local flow below. Package/runtime defects
    // and anything after a successful connect still fail (smokeLocalRuntimeFailure).
    let localRuntime = { state: 'unavailable', reason: null }
    let ready = null, suspended = null, resumed = null, fileResults = null
    let connectionId = null, pdfRendered = false, modelResults = null, tokenAudit = null
    try {
    // AppShell flow: connect the local workspace through the source registry
    // and activate it, then drive everything else through the real
    // ddp://app/api proxy exactly as the renderer does.
    clients.selectWorkspace = async () => selected
    const opened = await clients.workspaceOpen()
    if (!opened) throw new HostError('smoke_workspace_open_cancelled')
    connectionId = opened.sourceId
    ready = runtime.status(selected.workspaceId)
    const waitCurrent = async () => {
      for (let attempt = 0; attempt < 100; attempt++) {
        const current = clients.list().find(item => item.connectionId === connectionId)
        if (current.view.transport === 'ready' && current.view.snapshot === 'current') return current
        await delay(50)
      }
      throw new HostError('smoke_client_not_current')
    }
    await waitCurrent()
    const activated = (await clients.sourceList()).find(item => item.sourceId === connectionId)
    if (!activated?.active) throw new HostError('smoke_source_not_active')
    // The renderer reloads after activation (the location.reload() contract) and
    // boots against the new active source; it must not park on the sources page.
    await window.webContents.executeJavaScript(`location.hash = '#/resources'`)
    await window.webContents.reload()
    let booted = null
    for (let attempt = 0; attempt < 100 && !booted?.egress; attempt++) {
      await delay(100)
      booted = await window.webContents.executeJavaScript(`(() => ({ hash: location.hash,
        egress: document.querySelector('.egress')?.textContent ?? null,
        sources: !!document.querySelector('.sources') }))()`).catch(() => null)
    }
    if (!booted?.egress || booted.sources || booted.hash.includes('/sources')) {
      throw new HostError('smoke_boot_timeout')
    }
    const pdfPath = process.env.DDP_DESKTOP_SMOKE_PDF || path.join(repository, 'tests/fixtures/sample.pdf')
    const { readFile: readPdf } = await import('node:fs/promises')
    const pdfBytes = await readPdf(pdfPath)
    const proxy = async (method, apiPath, { headers = {}, body } = {}) => {
      const proxied = await clients.apiProxy({ method, path: apiPath, headers, body })
      if (proxied.headers['X-DDP-Source'] !== connectionId) throw new HostError('smoke_source_header_missing')
      return proxied
    }
    const proxyJson = async (method, apiPath, value, extraHeaders = {}) => {
      const proxied = await proxy(method, apiPath, { headers: { 'Content-Type': 'application/json', ...extraHeaders },
        body: value === undefined ? undefined : Buffer.from(JSON.stringify(value)) })
      const text = await new Response(proxied.body).text()
      let data = null
      try { data = text ? JSON.parse(text) : null } catch { throw new HostError('smoke_proxy_not_json') }
      return { status: proxied.status, data }
    }
    const uploadOne = async ({ bytes, filename, targetResourceId, key }) => {
      const created = await proxyJson('POST', '/api/uploads',
        { filename, size: bytes.length, mime: 'application/pdf',
          ...(targetResourceId ? { target_resource_id: targetResourceId } : {}) },
        { 'Idempotency-Key': key })
      if (created.status !== 201 && created.status !== 200) throw new HostError('smoke_import_failed')
      const session = created.data
      const put = await proxy('PUT', `/api/uploads/${session.id}/parts/1`,
        { headers: { 'Content-Type': 'application/pdf' }, body: Buffer.from(bytes) })
      if (put.status !== 200) throw new HostError('smoke_import_failed')
      await new Response(put.body).arrayBuffer()
      const finalized = await proxyJson('POST', `/api/uploads/${session.id}/finalize`,
        { engine: 'borndigital', options: {} }, { 'Idempotency-Key': session.id })
      if (!finalized.data?.version_id || !finalized.data?.resource_id) throw new HostError('smoke_import_failed')
      return finalized.data
    }
    const waitDocument = async versionId => {
      for (let attempt = 0; attempt < 100; attempt++) {
        const got = await proxyJson('GET', `/api/documents/${versionId}`)
        if (got.data?.status === 'succeeded') return got.data
        if (got.data?.status === 'failed') throw new HostError('smoke_parse_failed')
        await delay(300)
      }
      throw new HostError('smoke_parse_failed')
    }
    const first = await uploadOne({ bytes: pdfBytes, filename: 'sample.pdf', key: 'gui-smoke-import-0001' })
    const document = await waitDocument(first.version_id)
    fileResults = { resourceId: first.resource_id, versionId: first.version_id,
      status: document.status, pageCount: document.page_count }
    // Token audit: /api/auth/me and /api/v1/capabilities must carry no token-like
    // field, and the renderer bridge must expose no credential getter.
    const probe = await proxyJson('GET', '/api/auth/me')
    if (probe.status !== 200 || probe.data?.role !== 'admin') throw new HostError('smoke_token_audit_failed')
    // The renderer must see the same active source the host recorded.
    const bootProbe = await window.webContents.executeJavaScript(`(async () => {
      try {
        const listed = await window.ddpDesktop.sourceList()
        return { ok: listed?.ok, count: listed?.value?.length ?? -1,
          active: (listed?.value ?? []).filter(s => s.active).map(s => s.sourceId),
          error: listed?.error ?? null }
      } catch (error) { return { threw: String(error) } }
    })()`)
    if (!bootProbe.ok || !bootProbe.active?.length) throw new HostError('smoke_boot_source_missing')
    const audited = await window.webContents.executeJavaScript(`(async () => {
      const me = await fetch('ddp://app/api/auth/me').then(response => response.json()).catch(error => ({ error: String(error) }))
      const capabilities = await fetch('ddp://app/api/v1/capabilities').then(response => response.json()).catch(error => ({ error: String(error) }))
      const keys = value => value && typeof value === 'object' ? Object.keys(value) : []
      return { me, capabilities, meKeys: keys(me), capabilitiesKeys: keys(capabilities),
        methods: Object.keys(window.ddpDesktop ?? {}) }
    })()`)
    tokenAudit = { meKeys: audited.meKeys, capabilitiesKeys: audited.capabilitiesKeys, methods: audited.methods }
    const tokenLike = /(token|secret|password|credential|authorization|jwt|presign|signature|session)/i
    for (const key of [...audited.meKeys, ...audited.capabilitiesKeys]) {
      if (tokenLike.test(key)) throw new HostError('smoke_token_leaked')
    }
    if (JSON.stringify([audited.me, audited.capabilities]).match(/eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/)) {
      throw new HostError('smoke_token_leaked')
    }
    for (const name of audited.methods) {
      if (/credential|token|secret|password/i.test(name) && name !== 'setCredential' && name !== 'credentialStatus' && name !== 'clearCredential') {
        throw new HostError('smoke_token_leaked')
      }
    }
    if (audited.methods.includes('getCredential') || audited.methods.includes('clientReadOriginal')
        || audited.methods.includes('clientImportFile') || audited.methods.includes('clientConnectLocal')) {
      throw new HostError('smoke_removed_bridge_present')
    }
    // Append a version through the same proxy chain (target_resource_id).
    const appended = await uploadOne({ bytes: pdfBytes, filename: 'sample.pdf',
      targetResourceId: first.resource_id, key: 'gui-smoke-append-0001' })
    if (appended.version_id === first.version_id) throw new HostError('smoke_version_append_failed')
    await waitDocument(appended.version_id)
    fileResults.appendedVersion = appended.version_id
    if (process.env.DDP_DESKTOP_SMOKE_MODEL) {
      const modelId = process.env.DDP_DESKTOP_SMOKE_MODEL
      const runtimeId = process.env.DDP_DESKTOP_SMOKE_RUNTIME
      const command = async (name, payload, key = 'gui-smoke-' + randomUUID()) => {
        const result = await window.webContents.executeJavaScript(`window.ddpDesktop.clientCommand(${JSON.stringify({
          connectionId, name, payload, idempotencyKey: key,
        })})`)
        if (!result.ok) throw new HostError(result.error?.code || 'smoke_command_failed')
        return result.value
      }
      const started = await command('models.start', { model_id: modelId, ...(runtimeId ? { runtime_id: runtimeId } : {}) })
      modelResults = { model_id: modelId, started, cases: [] }
      // Same evidence-binding assertions as before, but through the content
      // conversation + SSE ask chain instead of the removed answer.generate IPC.
      const asked = await window.webContents.executeJavaScript(`(async () => {
        const conversation = await fetch('ddp://app/api/documents/${appended.version_id}/conversations', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
        }).then(response => response.json())
        const response = await fetch('ddp://app/api/conversations/' + conversation.id + '/ask', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: 'What is the answer in the contract? State the value with an original citation.' }),
        })
        const text = await response.text()
        return { conversation, events: text.split('\\n\\n').filter(Boolean) }
      })()`)
      const frames = asked.events.map(block => {
        const lines = block.split('\\n')
        return { event: lines[0].replace('event: ', ''), data: JSON.parse(lines[1].replace('data: ', '')) }
      })
      const kinds = frames.map(frame => frame.event)
      for (const required of ['meta', 'delta', 'citations', 'assertions', 'done']) {
        if (!kinds.includes(required)) throw new HostError('smoke_model_evidence_failed')
      }
      const citations = frames.find(frame => frame.event === 'citations').data.citations
      const assertions = frames.find(frame => frame.event === 'assertions').data.assertions
      const delta = frames.filter(frame => frame.event === 'delta').map(frame => frame.data.text ?? '').join('')
      const sourceBound = Array.isArray(citations) && citations.length > 0 &&
        citations.every(item => item.source_type === 'source' && item.source_version_id === appended.version_id)
      const cited = Array.isArray(assertions) && assertions.length > 0 &&
        assertions.every(claim => !claim.unsupported && claim.evidence_ids?.length > 0 &&
          claim.evidence_ids.every(id => citations.some(item => item.evidence_id === id || item.id === id)))
      if (!delta.includes('42') || !sourceBound || !cited) throw new HostError('smoke_model_evidence_failed')
      modelResults.cases.push({ operation: 'conversations.ask', input_version: appended.version_id, expected_value: '42',
        events: kinds, delta })
      const built = await proxyJson('POST', '/api/wikis',
        { title: 'Contract answer', sources: [{ resource_id: first.resource_id, source_version_id: appended.version_id }],
          max_pages: 1, max_output_tokens: 2048 }, { 'Idempotency-Key': 'gui-smoke-wiki-0001' })
      const wiki = built.data
      const dependencies = wiki.revision?.dependency_manifest || []
      const sentences = (wiki.revision?.pages || []).flatMap(page => page.generated_sections.flatMap(section => section.sentences))
      if (!wiki.wiki?.id || !wiki.revision?.id || !sentences.length || !dependencies.length ||
          !sentences.some(claim => claim.text.includes('42')) ||
          !sentences.every(claim => !claim.unsupported && claim.evidence_ids?.length &&
            claim.evidence_ids.every(id => dependencies.some(item => item.evidence_id === id))) ||
          !dependencies.every(item => item.original?.source_type === 'source' &&
            item.original?.source_version_id === appended.version_id && item.original?.locator?.bbox))
        throw new HostError('smoke_model_evidence_failed')
      modelResults.cases.push({ operation: 'wikis.create', input_version: appended.version_id, expected_value: '42',
        wiki_id: wiki.wiki.id })
      modelResults.stopped = await command('models.stop', {})
    }
    // Open the real AppShell workbench for the uploaded document and assert the
    // PDF canvas renders non-blank pixels. Drive through the vue-router (a bare
    // location.hash assignment does not reliably trigger the hash-history
    // router), and re-assert the active source right before: any renderer boot
    // that saw no_active_source would have parked on /sources instead.
    const stillActive = (await clients.sourceList()).find(item => item.sourceId === connectionId)
    if (!stillActive?.active) throw new HostError('smoke_source_not_active')
    const workbenchHash = '#/documents/' + appended.version_id
      + `?resource_id=${encodeURIComponent(first.resource_id)}&version_id=${encodeURIComponent(appended.version_id)}`
    await window.webContents.executeJavaScript(`location.hash = ${JSON.stringify(workbenchHash)}`)
    await window.webContents.reload()
    await delay(300)
    for (let attempt = 0; attempt < 100; attempt++) {
      pdfRendered = await window.webContents.executeJavaScript(`(() => {
        const canvas = document.querySelector('.pdf-canvas canvas')
        if (!canvas || canvas.width < 200 || canvas.height < 200) return false
        return canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data.some((v, i) => i % 4 === 3 && v > 0)
      })()`)
      if (pdfRendered) break
      await delay(100)
    }
    if (!pdfRendered) throw new HostError('smoke_pdf_not_rendered')
    await clients.suspend()
    await runtime.suspend()
    suspended = runtime.status(selected.workspaceId)
    await runtime.resume()
    await clients.resume()
    await waitCurrent()
    resumed = runtime.status(selected.workspaceId)
    localRuntime = { state: 'ready', reason: null }
    } catch (error) {
      const unavailable = smokeLocalRuntimeFailure(error,
        { kind: localRuntimeKind, connected: connectionId !== null })
      if (!unavailable) throw error
      localRuntime = unavailable
    }
    const screenshot = await window.webContents.capturePage()
    await writeFile(path.join(directory, 'desktop.png'), screenshot.toPNG())
    smokeResult = { directory, workspaceId: selected.workspaceId, report: { renderer, fonts, localRuntime, ready, suspended, resumed,
      application: { version: app.getVersion(), packaged: app.isPackaged },
      sharedClient: localRuntime.state === 'ready' ? { connectionId, fileResults, pdfRendered, modelResults, tokenAudit } : null,
      webPreferences: { sandbox: window.webContents.getLastWebPreferences().sandbox,
        contextIsolation: window.webContents.getLastWebPreferences().contextIsolation,
        nodeIntegration: window.webContents.getLastWebPreferences().nodeIntegration },
      display: platformName() === 'win32' ? { session: process.env.SESSIONNAME ?? null }
        : { wayland: process.env.WAYLAND_DISPLAY ?? null, x11: process.env.DISPLAY ?? null },
    } }
    window.close() // Exercise the real close -> controlled shutdown -> quit path.
  }
}).catch(async error => {
  if (smoke && window && !window.isDestroyed()) {
    const diagnostics = await window.webContents.executeJavaScript(`({
      pdfError: document.querySelector('.pdf-canvas')?.textContent ?? null,
      alert: document.querySelector('[role=alert]')?.textContent ?? null,
      source: document.querySelector('.source-panel')?.textContent ?? null,
    })`).catch(() => null)
    await writeFile(path.join(process.env.DDP_DESKTOP_SMOKE_DIRECTORY, 'failure.json'), JSON.stringify({
      code: error instanceof HostError ? error.code : 'host_operation_failed', diagnostics,
    }, null, 2)).catch(() => {})
  }
  // Do not log raw startup exceptions: paths, tokens and provider errors may be sensitive.
  process.stderr.write('DeepDocParse: host_start_failed\n')
  await clients?.close().catch(() => {})
  await runtime?.shutdown().catch(() => {})
  quitting = true; app.exit(1)
})
