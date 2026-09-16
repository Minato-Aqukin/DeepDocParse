import { app, BrowserWindow, Menu, dialog, ipcMain, protocol, session, safeStorage, powerMonitor } from 'electron'
import { fileURLToPath } from 'node:url'
import { readdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
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
    if (!smoke && runtime?.activeCount()) {
      const answer = await dialog.showMessageBox(window, { type: 'question', title: '退出 DeepDocParse',
        message: '退出会停止本机任务', detail: '重新打开后将核对任务状态；未完成任务可能需要重新执行。远端服务会继续运行。',
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
    selectInput: async kind => {
      const selected = await dialog.showOpenDialog(window, { title: kind === 'pdf' ? '导入 PDF' : '导入证据包',
        properties: ['openFile'], filters: [{ name: kind === 'pdf' ? 'PDF' : 'DDP bundle', extensions: kind === 'pdf' ? ['pdf'] : ['zip'] }] })
      return selected.canceled || selected.filePaths.length !== 1 ? null : selected.filePaths[0]
    },
    selectOutput: async () => {
      const selected = await dialog.showSaveDialog(window, { title: '导出证据包', defaultPath: 'document.ddp.zip',
        filters: [{ name: 'DDP bundle', extensions: ['zip'] }] })
      return selected.canceled ? null : selected.filePath
    },
    // A grant needs a user action the renderer cannot script: a native dialog that
    // restates the stored scope (recipient, endpoint, exact payload bytes, expiry).
    confirmApproval: async summary => {
      const payloads = summary.payloads.map(item => `  ${item.kind} → ${item.recipient}（${item.bytes} 字节，${String(item.digest).slice(0, 19)}…）`)
      const transports = summary.transports.map(item => `  ${item.recipient} · ${item.endpoint}`)
      const answer = await dialog.showMessageBox(window, { type: 'warning', title: '批准外发',
        message: summary.phase === 'exploration' ? '批准探索：把以下内容发给中心，用于探测与规划' : '批准执行：按已审阅计划提交给中心',
        detail: [`计划 ${summary.planId}`, `范围摘要 ${String(summary.scopeDigest).slice(0, 23)}…`, '外发内容：', ...payloads,
          '接收方：', ...transports, `本地输入 ${summary.inputs} 项（只锁定摘要，不上传原件）`,
          `保留策略 ${summary.retention} · 输出位置 ${summary.outputLocations.join('、')}`, `有效期至 ${summary.validUntil}`].join('\n'),
        buttons: ['取消', '批准'], defaultId: 0, cancelId: 0, noLink: true })
      return answer.response === 1
    },
  }).initialize()
  const hostStatus = () => {
    const storage = credentials.policy()
    return { platform: process.platform, electron: process.versions.electron,
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
    : path.join(repository, 'apps/web/dist'), ui))
  window = new BrowserWindow({ width: 1280, height: 860, minWidth: 860, minHeight: 640,
    title: 'DeepDocParse', show: false, webPreferences: { session: isolated,
      preload: path.join(source, 'preload.cjs'), sandbox: true, contextIsolation: true,
      nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false,
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
    clientList: () => clients.list(), clientConnectLocal: input => clients.connectLocal(input),
    clientPairRemote: input => clients.pairRemote(input), clientWake: input => clients.wake(input),
    clientDisconnect: input => clients.disconnect(input), clientCommand: input => clients.command(input),
    clientQuery: input => clients.query(input), clientReceipt: input => clients.receipt(input),
    clientReadDraft: input => clients.readDraft(input), clientSaveDraft: input => clients.saveDraft(input),
    clientUnsubscribe: input => clients.unsubscribe(input), clientImportFile: input => clients.importFile(input),
    clientExportBundle: input => clients.exportBundle(input), clientReadOriginal: input => clients.readOriginal(input),
    clientPlanPropose: input => clients.planPropose(input), clientPlanList: input => clients.planList(input),
    clientPlanGet: input => clients.planGet(input), clientPlanApprove: input => clients.planApprove(input),
    clientPlanRevoke: input => clients.planRevoke(input), clientPlanDispatch: input => clients.planDispatch(input),
    clientPlanReconcile: input => clients.planReconcile(input), clientPlanFetchDelivery: input => clients.planFetchDelivery(input),
    clientPlanConfirmDelivery: input => clients.planConfirmDelivery(input),
  }
  for (const [method, channel] of Object.entries(CLIENT_CHANNELS)) ipcMain.handle(channel, async (event, input) => {
    try {
      authorizeSender(event, window.webContents, ui)
      const argument = clientArguments(method, input)
      const value = method === 'clientSubscribe' ? clients.subscribe(argument, message => {
        authorizeSender(event, window.webContents, ui)
        event.senderFrame.send('ddp:client-view', message)
      }) : await clientOperations[method](argument)
      return { ok: true, value }
    } catch (error) { return clientFailure(error) }
  })
  // Reload/unmount disposes view subscriptions only; host-owned connections keep running.
  window.webContents.on('did-start-navigation', (_event, _url, _inPlace, isMainFrame) => {
    if (isMainFrame) clients.clearSubscriptions()
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
      { label: '启动已选工作区', click: action(async () => {
        if (!selectedWorkspace) selectedWorkspace = await chooseWorkspace()
        if (selectedWorkspace) { await clients.connectLocal({ workspaceId: selectedWorkspace.workspaceId }); window.setTitle(`DeepDocParse · ${selectedWorkspace.name}`) }
      }) },
      { label: '停止已选工作区', click: action(async () => { if (selectedWorkspace) { await clients.stopWorkspace(selectedWorkspace.workspaceId); await runtime.stop(selectedWorkspace.workspaceId) } }) },
      { type: 'separator' }, { label: '退出', accelerator: 'CmdOrCtrl+Q', click: () => { void requestQuit() } },
    ] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '视图', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }] },
  ]))
  let suspension = Promise.resolve()
  powerMonitor.on('suspend', () => { suspension = clients.suspend().then(() => runtime.suspend()).catch(() => {}) })
  powerMonitor.on('resume', () => { void suspension.then(() => runtime.resume()).then(() => clients.resume()) })
  await window.loadURL(ui.href)
  window.show()
  // Test instrumentation is reachable only from this trusted development startup, never IPC.
  if (smoke) {
    const directory = process.env.DDP_DESKTOP_SMOKE_DIRECTORY
    await secureDirectory(directory, { code: 'unsafe_smoke_directory' })
    const renderer = await window.webContents.executeJavaScript(`(async () => ({
      nodeRequire: typeof require, nodeProcess: typeof process,
      methods: Object.keys(window.ddpDesktop ?? {}), host: await window.ddpDesktop.hostStatus(),
      rejected: await window.ddpDesktop.startLocal({ workspaceId: 'not-a-handle', shell: 'id' }),
      title: document.title, rendered: document.querySelector('#app')?.children.length ?? 0,
    }))()`)
    let selected
    if (localRuntimeKind === 'wsl') {
      selected = workspaces.selectedWsl({ directory: WSL_WORKSPACE_DIRECTORY })
    } else {
      const workspace = path.join(directory, 'workspace')
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
    let connectionId = null, pdfRendered = false
    try {
    const connected = await clients.connectLocal({ workspaceId: selected.workspaceId })
    connectionId = connected.connectionId
    ready = runtime.status(selected.workspaceId)
    const waitCurrent = async () => {
      for (let attempt = 0; attempt < 100; attempt++) {
        const current = clients.list().find(item => item.connectionId === connected.connectionId)
        if (current.view.transport === 'ready' && current.view.snapshot === 'current') return current
        await delay(50)
      }
      throw new HostError('smoke_client_not_current')
    }
    await waitCurrent()
    clients.selectInput = async () => path.join(repository, 'tests/fixtures/sample.pdf')
    clients.selectOutput = async () => path.join(directory, 'smoke.ddp.zip')
    const imported = await window.webContents.executeJavaScript(`window.ddpDesktop.clientImportFile(${JSON.stringify({
      connectionId: connected.connectionId, kind: 'pdf', idempotencyKey: 'gui-smoke-import-0001',
    })})`)
    if (!imported.ok) throw new HostError('smoke_import_failed')
    for (let attempt = 0; attempt < 100; attempt++) {
      const tasks = clients.list().find(item => item.connectionId === connected.connectionId).view.projection.state.tasks
      if (tasks.some(task => task.id === imported.value.id && task.status === 'succeeded')) break
      if (attempt === 99) throw new HostError('smoke_parse_failed')
      await delay(100)
    }
    fileResults = await window.webContents.executeJavaScript(`(async () => {
      const original = await window.ddpDesktop.clientReadOriginal(${JSON.stringify({ connectionId: connected.connectionId, versionId: imported.value.version_id })})
      const exported = await window.ddpDesktop.clientExportBundle(${JSON.stringify({ connectionId: connected.connectionId, versionId: imported.value.version_id })})
      return { originalBytes: original.ok ? original.value.byteLength : 0, exported }
    })()`)
    if (!fileResults.originalBytes || !fileResults.exported.ok) throw new HostError('smoke_file_failed')
    await window.loadURL(ui.href)
    await delay(300)
    await window.webContents.executeJavaScript(`document.querySelector('.resource-row')?.click()`)
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
    smokeResult = { directory, workspaceId: selected.workspaceId, report: { renderer, localRuntime, ready, suspended, resumed,
      sharedClient: localRuntime.state === 'ready' ? { connectionId, fileResults, pdfRendered } : null,
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
