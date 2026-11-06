import { describe, expect, it, vi } from 'vitest'
import { userAgent } from '@deepseek-ai/dsh-llm'
import { DeepSeekFileId } from '../src/file-id.ts'
import {
  DeepSeekFilesClient,
  DeepSeekFilesError,
  isFilesQuotaError,
  MAX_FILE_UPLOAD_BYTES,
} from '../src/files-api.ts'

function requestUrl(input: string | URL | Request): string {
  if (typeof input === 'string') return input
  return input instanceof URL ? input.href : input.url
}

function file(overrides: Record<string, unknown> = {}) {
  return {
    id: 'file-api-one',
    object: 'file',
    bytes: 3,
    created_at: 1_700_000_000,
    filename: 'image.png',
    purpose: 'user_data',
    expires_at: 1_700_604_800,
    ...overrides,
  }
}

describe('DeepSeekFilesClient', () => {
  it('uploads multipart bytes with the required purpose and explicit expiry', async () => {
    const fetchImpl = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      expect(requestUrl(url)).toBe('https://api.deepseek.com/files')
      expect(init?.method).toBe('POST')
      const headers = new Headers(init?.headers)
      expect(headers.get('authorization')).toBe('Bearer key')
      expect(headers.get('user-agent')).toBe(userAgent())
      const form = init?.body
      expect(form).toBeInstanceOf(FormData)
      if (!(form instanceof FormData)) throw new Error('expected multipart body')
      expect(form.get('purpose')).toBe('user_data')
      expect(form.get('expires_after[anchor]')).toBe('created_at')
      expect(form.get('expires_after[seconds]')).toBe('604800')
      const blob = form.get('file')
      expect(blob).toBeInstanceOf(Blob)
      expect((blob as Blob).size).toBe(3)
      return new Response(JSON.stringify(file()), { status: 200 })
    }) as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com/', apiKey: 'key', fetch: fetchImpl })

    await expect(client.upload({
      data: Uint8Array.of(1, 2, 3),
      mediaType: 'image/png',
      filename: 'image.png',
      expiresAfterSeconds: 604_800,
    })).resolves.toEqual({
      id: DeepSeekFileId('file-api-one'),
      bytes: 3,
      createdAt: 1_700_000_000,
      filename: 'image.png',
      purpose: 'user_data',
      expiresAt: 1_700_604_800,
    })
  })

  it('validates list, retrieve, and delete responses', async () => {
    const fetchImpl = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const target = requestUrl(url)
      if (target.includes('?')) {
        return new Response(JSON.stringify({
          object: 'list', data: [file()], first_id: 'file-api-one', last_id: 'file-api-one', has_more: false,
        }), { status: 200 })
      }
      if (init?.method === 'DELETE') {
        return new Response(JSON.stringify({ id: 'file-api-one', object: 'file', deleted: true }), { status: 200 })
      }
      return new Response(JSON.stringify(file()), { status: 200 })
    }) as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com', apiKey: 'key', fetch: fetchImpl })

    await expect(client.list({ after: DeepSeekFileId('file-api-before'), limit: 20, order: 'desc' })).resolves.toMatchObject({
      data: [{ id: 'file-api-one' }], firstId: 'file-api-one', lastId: 'file-api-one', hasMore: false,
    })
    await expect(client.retrieve(DeepSeekFileId('file-api-one'))).resolves.toMatchObject({ id: 'file-api-one' })
    await expect(client.delete(DeepSeekFileId('file-api-one'))).resolves.toBeUndefined()
  })

  it('refuses an upload response that omits the requested expiry', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response(
      JSON.stringify(file({ expires_at: undefined })),
      { status: 200 },
    ))) as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com', apiKey: 'key', fetch: fetchImpl })

    await expect(client.upload({
      data: Uint8Array.of(1), mediaType: 'image/png', filename: 'image.png', expiresAfterSeconds: 3_600,
    })).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('retains quota error detail for the one cleanup retry policy', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      error: { message: 'user storage quota exceeded', type: 'invalid_request_error', code: 'file_quota' },
    }), { status: 400 }))) as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com', apiKey: 'key', fetch: fetchImpl })

    const error = await client.upload({
      data: Uint8Array.of(1), mediaType: 'image/png', filename: 'image.png', expiresAfterSeconds: 3_600,
    }).catch((caught: unknown) => caught)
    expect(isFilesQuotaError(error)).toBe(true)
    expect(isFilesQuotaError(new Error('storage quota'))).toBe(false)
  })

  it.each([
    [401, 'AUTH'],
    [403, 'AUTH'],
    [429, 'RATE_LIMIT'],
    [500, 'SERVER'],
    [400, 'FILES_API'],
  ] as const)('classifies HTTP %i Files failures as %s', async (status, code) => {
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.resolve(new Response('not-json', { status }))),
    })
    await expect(client.retrieve(DeepSeekFileId('missing'))).rejects.toMatchObject({
      name: 'DeepSeekFilesError',
      code,
      detail: '',
    })
  })

  it.each([
    null,
    [],
    {},
    { error: null },
    { error: [] },
    { error: { message: 1, type: 2, code: 3 } },
  ])('falls back to the HTTP status for an unstructured provider error %#', async (body) => {
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 400 }))),
    })
    const error = await client.retrieve(DeepSeekFileId('missing')).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(DeepSeekFilesError)
    expect(error).toMatchObject({ message: 'DeepSeek Files API error (HTTP 400)', detail: '' })
  })

  it('wraps transport failures but preserves an aborted request reason', async () => {
    const transport = new Error('socket closed')
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.reject(transport)),
    })
    await expect(client.retrieve(DeepSeekFileId('one'))).rejects.toMatchObject({
      code: 'TRANSPORT',
      cause: transport,
    })

    const controller = new AbortController()
    const reason = new Error('cancelled')
    controller.abort(reason)
    await expect(client.retrieve(DeepSeekFileId('one'), controller.signal)).rejects.toBe(transport)
  })

  it.each([
    null,
    [],
    file({ id: 1 }),
    file({ id: '' }),
    file({ object: 'wrong' }),
    file({ bytes: 1.5 }),
    file({ bytes: -1 }),
    file({ created_at: 1.5 }),
    file({ created_at: -1 }),
    file({ filename: 1 }),
    file({ filename: '' }),
    file({ purpose: 'assistants' }),
    file({ expires_at: 1.5 }),
    file({ expires_at: -1 }),
  ])('rejects an invalid file object %#', async (body) => {
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    })
    await expect(client.retrieve(DeepSeekFileId('one'))).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it.each([
    3_599,
    2_592_001,
    3_600.5,
  ])('refuses invalid file expiry %s before transport', async (expiresAfterSeconds) => {
    const fetchImpl = vi.fn() as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com', apiKey: 'key', fetch: fetchImpl })
    await expect(client.upload({
      data: Uint8Array.of(1), mediaType: 'image/png', filename: 'image.png', expiresAfterSeconds,
    })).rejects.toMatchObject({ code: 'INVALID_REQUEST' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('refuses a file larger than the upload limit before transport', async () => {
    const fetchImpl = vi.fn() as typeof fetch
    const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com', apiKey: 'key', fetch: fetchImpl })
    const data = { byteLength: MAX_FILE_UPLOAD_BYTES + 1 } as Uint8Array
    await expect(client.upload({
      data, mediaType: 'image/png', filename: 'image.png', expiresAfterSeconds: 3_600,
    })).rejects.toMatchObject({ code: 'INVALID_REQUEST' })
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it.each([
    null,
    [],
    {},
    { object: 'wrong', data: [], has_more: false },
    { object: 'list', data: null, has_more: false },
    { object: 'list', data: [], has_more: 0 },
    { object: 'list', data: [], has_more: false, first_id: 1 },
    { object: 'list', data: [], has_more: false, last_id: 1 },
  ])('rejects an invalid list response %#', async (body) => {
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    })
    await expect(client.list()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('accepts a list without cursors and uses the global fetch default', async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response(JSON.stringify({
      object: 'list', data: [], has_more: false,
    }), { status: 200 })))
    vi.stubGlobal('fetch', fetchImpl)
    try {
      const client = new DeepSeekFilesClient({ baseURL: 'https://api.deepseek.com///', apiKey: 'key' })
      await expect(client.list()).resolves.toEqual({ data: [], hasMore: false })
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it.each([
    null,
    [],
    {},
    { id: 'wrong', object: 'file', deleted: true },
    { id: 'file-api-one', object: 'wrong', deleted: true },
    { id: 'file-api-one', object: 'file', deleted: false },
  ])('rejects an invalid delete response %#', async (body) => {
    const client = new DeepSeekFilesClient({
      baseURL: 'https://api.deepseek.com',
      apiKey: 'key',
      fetch: vi.fn(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
    })
    await expect(client.delete(DeepSeekFileId('file-api-one'))).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
})
