const { contextBridge, ipcRenderer } = require('electron')

// No generic invoke/send function or Electron object crosses the isolated bridge.
contextBridge.exposeInMainWorld('ddpDesktop', Object.freeze({
  clientList: () => ipcRenderer.invoke('ddp:clientList'),
  clientConnectLocal: input => ipcRenderer.invoke('ddp:clientConnectLocal', input),
  clientPairRemote: input => ipcRenderer.invoke('ddp:clientPairRemote', input),
  clientWake: input => ipcRenderer.invoke('ddp:clientWake', input),
  clientDisconnect: input => ipcRenderer.invoke('ddp:clientDisconnect', input),
  clientSubscribe: input => ipcRenderer.invoke('ddp:clientSubscribe', input),
  clientUnsubscribe: input => ipcRenderer.invoke('ddp:clientUnsubscribe', input),
  clientCommand: input => ipcRenderer.invoke('ddp:clientCommand', input),
  clientQuery: input => ipcRenderer.invoke('ddp:clientQuery', input),
  clientReceipt: input => ipcRenderer.invoke('ddp:clientReceipt', input),
  clientReadDraft: input => ipcRenderer.invoke('ddp:clientReadDraft', input),
  clientSaveDraft: input => ipcRenderer.invoke('ddp:clientSaveDraft', input),
  clientImportFile: input => ipcRenderer.invoke('ddp:clientImportFile', input),
  clientExportBundle: input => ipcRenderer.invoke('ddp:clientExportBundle', input),
  clientReadOriginal: input => ipcRenderer.invoke('ddp:clientReadOriginal', input),
  onClientView: listener => {
    if (typeof listener !== 'function') throw new TypeError('listener_required')
    const receive = (_event, message) => listener(message)
    ipcRenderer.on('ddp:client-view', receive)
    return () => ipcRenderer.removeListener('ddp:client-view', receive)
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
