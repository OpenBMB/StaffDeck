import { createServer } from 'node:http'
import type { IncomingMessage, Server, ServerResponse } from 'node:http'

/** One scripted behavior for the next request the mock server receives. */
export type Behavior =
  | { kind: 'sse'; events: string[]; delayMs?: number }
  | { kind: 'http-error'; status: number; body: string; contentType?: string; headers?: Record<string, string> }
  | { kind: 'close-early'; events: string[] }

export interface MockServer {
  url: string
  /** Bodies of received requests, in order. */
  requests: unknown[]
  /** Header bags of received requests, in order (parallel to `requests`). */
  headers: IncomingMessage['headers'][]
  /** Parsed Files API operations, excluded from chat request ordering. */
  fileRequests: Array<{ method: string; path: string; filename?: string; bytes?: number }>
  script: Behavior[]
  close(): Promise<void>
}

const servers: Server[] = []

/** Close every server opened since the last call; run from each spec's afterEach. */
export async function closeMockServers(): Promise<void> {
  await Promise.all(servers.splice(0).map(server => new Promise(resolve => server.close(resolve))))
}

/** A minimal complete text generation, reused by request-shape assertions. */
export const textEvents = [
  '{"choices":[{"delta":{"role":"assistant","content":null,"reasoning_content":""}}]}',
  '{"choices":[{"delta":{"content":"hello"}}]}',
  '{"choices":[{"delta":{"content":""},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
  '[DONE]',
]

/** Local chat-completions stand-in: replays scripted behaviors per request. */
export async function mockServer(script: Behavior[]): Promise<MockServer> {
  const requests: unknown[] = []
  const headers: IncomingMessage['headers'][] = []
  const fileRequests: MockServer['fileRequests'] = []
  const files = new Map<string, { id: string; object: 'file'; bytes: number; created_at: number; filename: string; purpose: 'user_data'; expires_at: number }>()
  let nextFile = 1
  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    const chunks: Buffer[] = []
    request.on('data', (chunk: Buffer) => { chunks.push(chunk) })
    request.on('end', () => {
      void (async () => {
        const url = new URL(request.url ?? '/', 'http://localhost')
        const body = Buffer.concat(chunks)
        if (url.pathname === '/files' && request.method === 'POST') {
          const headers = new Headers()
          for (const [name, value] of Object.entries(request.headers)) {
            if (value !== undefined) headers.set(name, Array.isArray(value) ? value.join(', ') : value)
          }
          const form = await new Request('http://localhost/files', {
            method: 'POST',
            headers,
            body,
          }).formData()
          const blob = form.get('file')
          if (!(blob instanceof Blob)) throw new Error('mock upload omitted file')
          const name = 'name' in blob && typeof blob.name === 'string' ? blob.name : 'uploaded_file'
          const id = `file-api-${nextFile}`
          const createdAt = Math.floor(Date.now() / 1_000)
          nextFile += 1
          const expiresSeconds = Number(form.get('expires_after[seconds]'))
          const file = {
            id,
            object: 'file' as const,
            bytes: blob.size,
            created_at: createdAt,
            filename: name,
            purpose: 'user_data' as const,
            expires_at: createdAt + expiresSeconds,
          }
          files.set(id, file)
          fileRequests.push({ method: 'POST', path: url.pathname, filename: name, bytes: blob.size })
          response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(file))
          return
        }
        if (url.pathname === '/files' && request.method === 'GET') {
          fileRequests.push({ method: 'GET', path: `${url.pathname}${url.search}` })
          const data = [...files.values()]
          response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({
            object: 'list',
            data,
            first_id: data[0]?.id,
            last_id: data.at(-1)?.id,
            has_more: false,
          }))
          return
        }
        if (url.pathname.startsWith('/files/') && request.method === 'DELETE') {
          const id = decodeURIComponent(url.pathname.slice('/files/'.length))
          files.delete(id)
          fileRequests.push({ method: 'DELETE', path: url.pathname })
          response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({
            id, object: 'file', deleted: true,
          }))
          return
        }
        if (url.pathname.startsWith('/files/') && request.method === 'GET') {
          const id = decodeURIComponent(url.pathname.slice('/files/'.length))
          fileRequests.push({ method: 'GET', path: url.pathname })
          const file = files.get(id)
          if (file === undefined) {
            response.writeHead(404, { 'content-type': 'application/json' }).end(JSON.stringify({
              error: { message: 'file not found', code: 'file_not_found' },
            }))
          } else {
            response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify(file))
          }
          return
        }

        requests.push(JSON.parse(body.toString('utf8')))
        headers.push(request.headers)
        const behavior = script.shift()
        if (!behavior) {
          response.writeHead(500).end('mock script exhausted')
          return
        }
        if (behavior.kind === 'http-error') {
          response.writeHead(behavior.status, {
            'content-type': behavior.contentType ?? 'application/json',
            ...behavior.headers,
          })
          response.end(behavior.body)
          return
        }
        response.writeHead(200, { 'content-type': 'text/event-stream' })
        const write = (index: number): void => {
          if (index >= behavior.events.length) {
            if (behavior.kind === 'sse') response.end()
            else response.destroy() // close-early: drop the socket mid-stream
            return
          }
          response.write(`data: ${behavior.events[index]}\n\n`)
          setTimeout(() => { write(index + 1) }, behavior.kind === 'sse' ? behavior.delayMs ?? 0 : 5)
        }
        write(0)
      })().catch((error: unknown) => {
        response.writeHead(500, { 'content-type': 'text/plain' }).end(String(error))
      })
    })
  })
  servers.push(server)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('no port')
  return {
    url: `http://127.0.0.1:${address.port}`,
    requests,
    headers,
    fileRequests,
    script,
    close: () => new Promise(resolve => server.close(() => { resolve() })),
  }
}
