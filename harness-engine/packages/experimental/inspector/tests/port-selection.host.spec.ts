/** Host Worker port-selection behavior. */

import { createServer, type Server } from 'node:http'
import { afterEach, describe, expect, it } from 'vitest'
import { startInspector, type InspectorHandle } from '../src/host/bridge/controller.ts'

describe('Inspector endpoint port selection', () => {
  let blocker: Server | undefined
  let inspector: InspectorHandle | undefined

  afterEach(async () => {
    await inspector?.close()
    inspector = undefined
    if (blocker?.listening === true) {
      await new Promise<void>((resolve) => { blocker!.close(() => { resolve() }) })
    }
    blocker = undefined
  })

  it('advances from an occupied starting port and publishes the selected port', async () => {
    blocker = createServer()
    await new Promise<void>((resolve, reject) => {
      blocker!.once('error', reject)
      blocker!.listen(0, '127.0.0.1', () => {
        blocker!.off('error', reject)
        resolve()
      })
    })
    const occupiedAddress = blocker.address()
    if (occupiedAddress === null || typeof occupiedAddress === 'string') {
      throw new Error('test server did not bind a TCP port')
    }

    inspector = await startInspector({ port: occupiedAddress.port, captureFetch: false })
    const selectedPort = Number(new URL(inspector.endpoint.httpUrl).port)

    expect(selectedPort).toBeGreaterThan(occupiedAddress.port)
    expect(new URL(inspector.endpoint.webSocketDebuggerUrl).port).toBe(String(selectedPort))
    expect(new URL(inspector.endpoint.client.endpoint).port).toBe(String(selectedPort))
    await expect(fetch(new URL('json', inspector.endpoint.httpUrl)).then(response => response.status)).resolves.toBe(200)
  })
})
