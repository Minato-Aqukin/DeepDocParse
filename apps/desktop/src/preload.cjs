const { contextBridge, ipcRenderer } = require('electron')

// No generic invoke/send function or Electron object crosses the isolated bridge.
contextBridge.exposeInMainWorld('ddpDesktop', Object.freeze({
  clientList: () => ipcRenderer.invoke('ddp:clientList'),
  clientCommand: input => ipcRenderer.invoke('ddp:clientCommand', input),
  clientQuery: input => ipcRenderer.invoke('ddp:clientQuery', input),
  clientReceipt: input => ipcRenderer.invoke('ddp:clientReceipt', input),
  clientReadDraft: input => ipcRenderer.invoke('ddp:clientReadDraft', input),
  clientSaveDraft: input => ipcRenderer.invoke('ddp:clientSaveDraft', input),
  clientPlanPropose: input => ipcRenderer.invoke('ddp:clientPlanPropose', input),
  clientPlanProposeFile: input => ipcRenderer.invoke('ddp:clientPlanProposeFile', input),
  clientPlanList: input => ipcRenderer.invoke('ddp:clientPlanList', input),
  clientPlanGet: input => ipcRenderer.invoke('ddp:clientPlanGet', input),
  clientPlanReviewCenter: input => ipcRenderer.invoke('ddp:clientPlanReviewCenter', input),
  clientPlanApprove: input => ipcRenderer.invoke('ddp:clientPlanApprove', input),
  clientPlanRevoke: input => ipcRenderer.invoke('ddp:clientPlanRevoke', input),
  clientPlanCancel: input => ipcRenderer.invoke('ddp:clientPlanCancel', input),
  clientPlanDispatch: input => ipcRenderer.invoke('ddp:clientPlanDispatch', input),
  clientPlanResume: input => ipcRenderer.invoke('ddp:clientPlanResume', input),
  clientPlanReconcile: input => ipcRenderer.invoke('ddp:clientPlanReconcile', input),
  clientPlanFetchDelivery: input => ipcRenderer.invoke('ddp:clientPlanFetchDelivery', input),
  clientPlanConfirmDelivery: input => ipcRenderer.invoke('ddp:clientPlanConfirmDelivery', input),
  sourceList: () => ipcRenderer.invoke('ddp:source-list'),
  sourceActivate: input => ipcRenderer.invoke('ddp:source-activate', input),
  sourceReconnect: input => ipcRenderer.invoke('ddp:source-reconnect', input),
  sourceRemove: input => ipcRenderer.invoke('ddp:source-remove', input),
  workspaceOpen: () => ipcRenderer.invoke('ddp:workspace-open'),
  centerConnect: input => ipcRenderer.invoke('ddp:center-connect', input),
  onSourceChange: listener => {
    if (typeof listener !== 'function') throw new TypeError('listener_required')
    const receive = (_event, message) => listener(message)
    ipcRenderer.on('ddp:source-change', receive)
    void ipcRenderer.invoke('ddp:source-change-subscribe').catch(() => {
      ipcRenderer.removeListener('ddp:source-change', receive)
    })
    return () => ipcRenderer.removeListener('ddp:source-change', receive)
  },
  hostStatus: () => ipcRenderer.invoke('ddp:host-status'),
  selectWorkspace: () => ipcRenderer.invoke('ddp:select-workspace'),
  startLocal: input => ipcRenderer.invoke('ddp:start-local', input),
  stopLocal: input => ipcRenderer.invoke('ddp:stop-local', input),
  runtimeStatus: input => ipcRenderer.invoke('ddp:runtime-status', input),
  setCredential: input => ipcRenderer.invoke('ddp:set-credential', input),
  credentialStatus: input => ipcRenderer.invoke('ddp:credential-status', input),
  clearCredential: input => ipcRenderer.invoke('ddp:clear-credential', input),
}))
