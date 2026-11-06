import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { Context } from '@deepseek-ai/cordis'
import type { IndexInjection, WebServer } from '@deepseek-ai/dsh-host-webserver'
import WebSocket, { type RawData } from 'ws'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apply, Config, inject, name, startInspector } from '../src/index.ts'
import { isPlainObject } from '../src/shared/json.ts'

interface CdpResponse {
  readonly id: number
  readonly result?: Record<string, unknown>
}

describe('experimental Inspector Host plugin', () => {
  let context: Context | undefined

  afterEach(async () => {
    await context?.fiber.dispose()
    context = undefined
    vi.restoreAllMocks()
  })

  it('starts the Worker, provides ctx.inspector, injects Client bootstrap, and disposes', async () => {
    context = new Context()
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    context.provide('webServer', {} as WebServer)
    const fiber = context.plugin(
      { name, inject: [...inject], Config, apply },
      { port: 0, captureFetch: false },
    )
    await fiber.await()

    const rows: IndexInjection[] = []
    context.emit('webserver/index-inject', rows)
    const bootstrap = rows.find(row => row.kind === 'global' && row.name === '__DSH_INSPECTOR__')
    expect(bootstrap).toMatchObject({ kind: 'global', name: '__DSH_INSPECTOR__' })
    expect(log).toHaveBeenCalledWith(expect.stringMatching(/^dsh inspector: devtools:\/\//u))
    expect(context.inspector).toBeDefined()
    await vi.waitFor(async () => {
      const tree = await context!.inspector.cordis.getTree()
      expect(tree.host?.source.kind).toBe('host')
    })
    expect(() => { context!.inspector.publish('', {}) }).toThrow('topic must contain 1 to 128 characters')
    expect(() => { context!.inspector.publish('host/invalid-time', {}, Number.NaN) }).toThrow('monotonicMs must be finite')
    context.inspector.publish('host/plugin-probe', { ready: true })

    const value = bootstrap?.kind === 'global' ? bootstrap.value : undefined
    const endpoint = value as { endpoint: string; protocol: string }
    const authority = new URL(endpoint.endpoint)
    const targets: unknown = await fetch(`http://${authority.host}/json`).then(response => response.json())
    if (!Array.isArray(targets) || !isPlainObject(targets[0]) || typeof targets[0].webSocketDebuggerUrl !== 'string') {
      throw new Error('Inspector discovery did not return a target')
    }
    const socket = new WebSocket(targets[0].webSocketDebuggerUrl)
    await new Promise<void>((resolve, reject) => {
      socket.once('open', () => { resolve() })
      socket.once('error', reject)
    })
    const response = new Promise<CdpResponse>((resolve) => {
      socket.on('message', (data) => {
        const message = JSON.parse(rawText(data)) as CdpResponse
        if (message.id === 1) resolve(message)
      })
    })
    socket.send(JSON.stringify({ id: 1, method: 'DSHInspector.getSources' }))
    await vi.waitFor(async () => {
      const sources = (await response).result?.sources as Array<{ topics: Record<string, number> }>
      expect(sources.some(source => source.topics['host/plugin-probe'] === 1)).toBe(true)
    })
    socket.close()
    await new Promise<void>((resolve) => { socket.once('close', () => { resolve() }) })

    await fiber.dispose()
    expect(rows).toHaveLength(1)
    const afterDispose: IndexInjection[] = []
    context.emit('webserver/index-inject', afterDispose)
    expect(afterDispose).toEqual([])
  })

  it('closes the started Worker when a later plugin registration fails', async () => {
    const port = await availablePort()
    context = new Context()
    context.provide('webServer', {} as WebServer)
    context.provide('inspector', {
      publish: () => undefined,
      cordis: { getTree: () => Promise.reject(new Error('unused test service')) },
    })

    const fiber = context.plugin(
      { name, inject: [...inject], Config, apply },
      { port, captureFetch: false },
    )
    await expect(fiber.await()).rejects.toThrow('service "inspector" has been registered')

    const replacement = await startInspector({ port, captureFetch: false })
    expect(new URL(replacement.endpoint.httpUrl).port).toBe(String(port))
    await replacement.close()
  })

  it('closes the Worker when fetch capture installation fails', async () => {
    const port = await availablePort()
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
    const nativeFetch = globalThis.fetch
    Object.defineProperty(globalThis, 'fetch', {
      configurable: true,
      get: () => nativeFetch,
    })
    try {
      await expect(startInspector({ port })).rejects.toThrow('globalThis.fetch is an accessor')
    } finally {
      if (descriptor === undefined) Reflect.deleteProperty(globalThis, 'fetch')
      else Object.defineProperty(globalThis, 'fetch', descriptor)
    }

    const replacement = await startInspector({ port, captureFetch: false })
    expect(new URL(replacement.endpoint.httpUrl).port).toBe(String(port))
    await replacement.close()
  })
})

async function availablePort(): Promise<number> {
  const server = createServer()
  await new Promise<void>((resolve) => { server.listen(0, '127.0.0.1', resolve) })
  const port = (server.address() as AddressInfo).port
  await new Promise<void>((resolve, reject) => {
    server.close((error) => { if (error === undefined) resolve(); else reject(error) })
  })
  return port
}

function rawText(data: RawData): string {
  if (Array.isArray(data)) return Buffer.concat(data).toString('utf8')
  if (data instanceof ArrayBuffer) return Buffer.from(data).toString('utf8')
  return Buffer.from(data).toString('utf8')
}
