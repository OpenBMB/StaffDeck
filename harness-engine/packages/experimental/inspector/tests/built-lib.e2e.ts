import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execa } from 'execa'
import { describe, expect, it } from 'vitest'

const packageDirectory = fileURLToPath(new URL('..', import.meta.url))
const built = [
  'lib/index.js',
  'lib/worker.js',
  'node_modules/@deepseek-ai/schemastery/lib/index.mjs',
].every(file => existsSync(join(packageDirectory, file)))

describe.skipIf(!built)('experimental Inspector built artifact', () => {
  it('starts its sibling Worker and evaluates the Host through plain Node', async () => {
    const script = `
      const { startInspector } = await import('@deepseek-ai/dsh-experimental-inspector')
      const { default: WebSocket } = await import('ws')
      globalThis.__builtInspectorProbe = 42
      const inspector = await startInspector({ port: 0, captureFetch: false })
      const socket = new WebSocket(inspector.endpoint.webSocketDebuggerUrl)
      await new Promise((resolve, reject) => {
        socket.once('open', resolve)
        socket.once('error', reject)
      })
      const response = new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('CDP response timeout')), 5000)
        socket.on('message', data => {
          const message = JSON.parse(Buffer.from(data).toString('utf8'))
          if (message.id !== 1) return
          clearTimeout(timer)
          resolve(message)
        })
      })
      socket.send(JSON.stringify({
        id: 1,
        method: 'Runtime.evaluate',
        params: { expression: 'globalThis.__builtInspectorProbe', returnByValue: true },
      }))
      const message = await response
      socket.close()
      await inspector.close()
      console.log(JSON.stringify(message.result.result))
    `
    const result = await execa(process.execPath, ['--input-type=module', '-e', script], {
      cwd: packageDirectory,
      stdin: 'ignore',
      timeout: 20_000,
      killSignal: 'SIGKILL',
      reject: false,
    })

    expect(result.exitCode, `stderr:\n${result.stderr}`).toBe(0)
    expect(JSON.parse(result.stdout.trim()) as unknown).toEqual({ type: 'number', value: 42, description: '42' })
  })
})
