import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const source = path.dirname(fileURLToPath(import.meta.url))
const packaged = path.resolve(source, '../../client-runtime')
const directory = existsSync(path.join(packaged, 'index.ts')) ? packaged
  : path.resolve(source, '../../../packages/client-runtime/src')
// Electron 44 embeds Node 24 with native type stripping; these files never enter the renderer.
export const { ConnectionRegistry, ConnectionFault } = await import(pathToFileURL(path.join(directory, 'index.ts')))
export const { SqliteProjectionStore } = await import(pathToFileURL(path.join(directory, 'sqlite-store.ts')))
export const { HttpProvider, OperationFault } = await import(pathToFileURL(path.join(directory, 'http-provider.ts')))
