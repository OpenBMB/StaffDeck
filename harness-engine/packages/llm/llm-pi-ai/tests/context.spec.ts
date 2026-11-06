import { describe, expect, it, vi } from 'vitest'
import { AttachmentId, ImageVariantId } from '@deepseek-ai/dsh-attachment'
import type {
  AttachmentStore,
  ImageAttachmentRef,
  ImageRequestPolicy,
  RequestImageAttachment,
} from '@deepseek-ai/dsh-attachment'
import { ToolCallId, createMessage, createUserMessage, offloadedImageText } from '@deepseek-ai/dsh-llm'
import type { ContentBlock, GenerateOptions, Message } from '@deepseek-ai/dsh-llm'
import { toPiContext } from '../src/context.ts'
import type { PiImageRequestContext } from '../src/context.ts'
import { toPiAssistant } from '../src/replay.ts'

const ref: ImageAttachmentRef = {
  attachmentId: AttachmentId(`sha256:${'a'.repeat(64)}`),
  mediaType: 'image/png',
  bytes: 1,
  width: 1,
  height: 1,
}

function requestImage(value: ImageAttachmentRef, data: Uint8Array): RequestImageAttachment {
  return {
    variantId: ImageVariantId(`sha256:${'b'.repeat(64)}`),
    attachment: value,
    data,
    mediaType: value.mediaType,
    bytes: data.byteLength,
    width: value.width,
    height: value.height,
    depth: 'uchar',
    space: 'srgb',
    hasAlpha: value.mediaType === 'image/png',
  }
}

function projectionStore(
  readImageRequest: (
    value: ImageAttachmentRef,
    policy: ImageRequestPolicy,
    signal?: AbortSignal,
  ) => Promise<RequestImageAttachment> = vi.fn((value: ImageAttachmentRef) => (
    Promise.resolve(requestImage(value, Uint8Array.of(1)))
  )),
): AttachmentStore {
  return { readImageRequest, imageHostPath: () => undefined } as unknown as AttachmentStore
}

const attachments = projectionStore()

function imageContext(
  store: AttachmentStore,
  overrides: Partial<Omit<PiImageRequestContext, 'attachments'>> = {},
): PiImageRequestContext {
  return { attachments: store, resolveImageAccess: () => undefined, ...overrides }
}

function request(messages: GenerateOptions['messages']): GenerateOptions {
  return {
    provider: 'openai',
    model: 'gpt-4.1',
    system: 'system prompt',
    tools: [{ name: 'lookup', description: 'look up', parameters: { type: 'object' } }],
    messages,
  }
}

function user(content: ContentBlock[]): Message {
  return createUserMessage({ content, source: { kind: 'plugin', plugin: 'test' } })
}

function history(role: 'system' | 'assistant', content: ContentBlock[]): Message {
  return createMessage({ role, content, source: { kind: 'plugin', plugin: 'test' } })
}

describe('pi-ai request context conversion', () => {
  it('omits absent and empty request-level optional fields', () => {
    const base = { provider: 'openai', model: 'gpt-4.1', messages: [] }
    expect(toPiContext(base)).toEqual({ messages: [] })
    expect(toPiContext({ ...base, tools: [] })).toEqual({ messages: [] })
  })

  it('converts complete text-only history and rejects nested images without storage', () => {
    const callId = ToolCallId('call-1')
    expect(toPiContext(request([
      history('system', [{ type: 'text', text: 'history system' }]),
      history('assistant', [{ type: 'tool-call', id: callId, name: 'lookup', arguments: '{}' }]),
      user([
        { type: 'text', text: 'after tool' },
        {
          type: 'tool-result',
          toolCallId: callId,
          content: [{ type: 'text', text: '' }],
        },
      ]),
    ]))).toMatchObject({
      systemPrompt: 'system prompt',
      tools: [{ name: 'lookup' }],
      messages: [
        { role: 'user', content: 'history system' },
        { role: 'assistant' },
        { role: 'user', content: 'after tool' },
        {
          role: 'toolResult',
          toolCallId: 'call-1',
          toolName: 'lookup',
          content: [{ type: 'text', text: '(no output)' }],
          isError: false,
        },
      ],
    })

    expect(() => toPiContext(request([user([{
      type: 'tool-result',
      toolCallId: callId,
      content: [{ type: 'image', attachment: ref }],
    }])]))).toThrow(/durable attachment service/)
  })

  it('resolves user and tool-result images while preserving explicit fallbacks', async () => {
    const callId = ToolCallId('missing-call')
    const knownCallId = ToolCallId('known-call')
    const context = await toPiContext(request([
      user([{ type: 'text', text: '' }]),
      history('assistant', [
        { type: 'text', text: 'calling' },
        { type: 'tool-call', id: knownCallId, name: 'lookup', arguments: '{}' },
      ]),
      user([
        { type: 'image', attachment: ref },
        { type: 'text', text: 'caption' },
        { type: 'reasoning', text: 'ignored' },
      ]),
      user([{
        type: 'tool-result',
        toolCallId: knownCallId,
        content: [{ type: 'text', text: '' }],
      }]),
      user([{
        type: 'tool-result',
        toolCallId: callId,
        isError: true,
        content: [
          { type: 'tool-result', toolCallId: callId, content: [] },
          { type: 'image', attachment: ref },
        ],
      }]),
    ]), imageContext(attachments))

    expect(context.messages).toEqual([
      { role: 'user', content: '', timestamp: 0 },
      expect.objectContaining({ role: 'assistant' }),
      {
        role: 'user',
        content: [
          { type: 'text', text: expect.stringContaining(`Image ${ref.attachmentId}`) as string },
          { type: 'image', data: 'AQ==', mimeType: 'image/png' },
          { type: 'text', text: 'caption' },
        ],
        timestamp: 0,
      },
      {
        role: 'toolResult',
        toolCallId: 'known-call',
        toolName: 'lookup',
        content: [{ type: 'text', text: '(no output)' }],
        isError: false,
        timestamp: 0,
      },
      {
        role: 'toolResult',
        toolCallId: 'missing-call',
        toolName: 'unknown',
        content: [
          { type: 'text', text: expect.stringContaining(`Image ${ref.attachmentId}`) as string },
          { type: 'image', data: 'AQ==', mimeType: 'image/png' },
        ],
        isError: true,
        timestamp: 0,
      },
    ])
  })

  it('uses the shared normalized-path description for retained images', async () => {
    const named = { ...ref, name: 'chart.png', width: 2048, height: 1024 }
    const store = projectionStore(value => Promise.resolve({
      ...requestImage(value, Uint8Array.of(1)),
      width: 1130,
      height: 565,
    }))
    const context = await toPiContext(request([user([{ type: 'image', attachment: named }])]), imageContext(store, {
      resolveImageAccess: () => ({ readonlyPath: '/tmp/dsh/objects/aa/object' }),
    }))
    expect(context.messages[0]).toMatchObject({
      role: 'user',
      content: [
        { type: 'text', text: expect.stringContaining('Image "chart.png"') as string },
        { type: 'image' },
      ],
    })
    expect(JSON.stringify(context.messages[0])).toContain('/tmp/dsh/objects/aa/object')
    expect(JSON.stringify(context.messages[0])).toContain('request preview 1130x565px')
  })

  it('recursively converts nested tool-result text and images', async () => {
    const callId = ToolCallId('nested-call')
    const context = await toPiContext(request([user([{
      type: 'tool-result',
      toolCallId: callId,
      content: [
        {
          type: 'tool-result',
          toolCallId: callId,
          content: [{ type: 'text', text: 'nested text' }],
        },
        {
          type: 'tool-result',
          toolCallId: callId,
          content: [{ type: 'image', attachment: ref }],
        },
      ],
    }])]), imageContext(attachments))

    expect(context.messages).toEqual([{
      role: 'toolResult',
      toolCallId: 'nested-call',
      toolName: 'unknown',
      content: [
        { type: 'text', text: 'nested text' },
        { type: 'text', text: expect.stringContaining(`Image ${ref.attachmentId}`) as string },
        { type: 'image', data: 'AQ==', mimeType: 'image/png' },
      ],
      isError: false,
      timestamp: 0,
    }])
  })

  it('flattens nested text-only tool results and ignores other block types without storage', () => {
    const callId = ToolCallId('nested-text')
    expect(toPiContext(request([user([{
      type: 'tool-result',
      toolCallId: callId,
      content: [
        { type: 'chart', data: 'ignored' } as unknown as ContentBlock,
        {
          type: 'tool-result',
          toolCallId: callId,
          content: [{ type: 'text', text: 'nested' }],
        },
      ],
    }])]))).toMatchObject({
      messages: [{
        role: 'toolResult',
        content: [{ type: 'text', text: 'nested' }],
      }],
    })
  })

  it('replaces the oldest images with placeholders once the request payload bound is exceeded', async () => {
    const readImageRequest = vi.fn((value: ImageAttachmentRef) => (
      Promise.resolve(requestImage(value, Uint8Array.of(1, 2, 3)))
    ))
    const store = projectionStore(readImageRequest)
    const sized: ImageAttachmentRef = { ...ref, bytes: 3 }
    const callId = ToolCallId('shot-call')
    // Three 3-byte images cost 4 base64 characters each (12 total); a bound of
    // 8 forces exactly the oldest one out, including one nested in a tool result.
    const context = await toPiContext(request([
      user([{
        type: 'tool-result',
        toolCallId: callId,
        content: [{ type: 'image', attachment: sized }],
      }]),
      user([{ type: 'image', attachment: sized }, { type: 'text', text: 'newer' }]),
      user([{ type: 'image', attachment: sized }]),
    ]), imageContext(store, { maxRequestImageBytes: 8 }))

    expect(context.messages).toEqual([
      {
        role: 'toolResult',
        toolCallId: 'shot-call',
        toolName: 'unknown',
        content: [{ type: 'text', text: offloadedImageText(sized) }],
        isError: false,
        timestamp: 0,
      },
      {
        role: 'user',
        content: [
          { type: 'text', text: expect.stringContaining(`Image ${sized.attachmentId}`) as string },
          { type: 'image', data: 'AQID', mimeType: 'image/png' },
          { type: 'text', text: 'newer' },
        ],
        timestamp: 0,
      },
      {
        role: 'user',
        content: [
          { type: 'text', text: expect.stringContaining(`Image ${sized.attachmentId}`) as string },
          { type: 'image', data: 'AQID', mimeType: 'image/png' },
        ],
        timestamp: 0,
      },
    ])
    expect(readImageRequest).toHaveBeenCalledTimes(1)
  })

  it('does not prepare an old image removed by the conservative request projection', async () => {
    const old = { ...ref, attachmentId: AttachmentId(`sha256:${'c'.repeat(64)}`), bytes: 3 }
    const recent = { ...ref, attachmentId: AttachmentId(`sha256:${'d'.repeat(64)}`), bytes: 3 }
    const readImageRequest = vi.fn((value: ImageAttachmentRef) => {
      if (value.attachmentId === old.attachmentId) throw new Error('old image must not be read')
      return Promise.resolve(requestImage(value, Uint8Array.of(1, 2, 3)))
    })

    const context = await toPiContext(request([user([
      { type: 'image', attachment: old },
      { type: 'image', attachment: recent },
    ])]), imageContext(projectionStore(readImageRequest), { maxRequestImageBytes: 4 }))

    expect(context.messages[0]).toMatchObject({
      role: 'user',
      content: [
        { type: 'text', text: offloadedImageText(old) },
        { type: 'text', text: expect.stringContaining(String(recent.attachmentId)) as string },
        { type: 'image' },
      ],
    })
    expect(readImageRequest).toHaveBeenCalledTimes(1)
    expect(readImageRequest.mock.calls[0]?.[0]).toEqual(recent)
  })

  it('uses independently resolved access when exact encoded bytes require offload', async () => {
    const sized: ImageAttachmentRef = { ...ref, bytes: 3 }
    const access = { readonlyPath: '/tmp/dsh-normalized-image' }
    const readImageRequest = vi.fn((value: ImageAttachmentRef) => Promise.resolve({
      ...requestImage(value, Uint8Array.of(1, 2, 3, 4)),
    }))

    const context = await toPiContext(request([
      user([{ type: 'image', attachment: sized }]),
    ]), imageContext(projectionStore(readImageRequest), {
      maxRequestImageBytes: 4,
      resolveImageAccess: () => access,
    }))

    expect(context.messages).toEqual([{
      role: 'user',
      content: offloadedImageText(sized, access),
      timestamp: 0,
    }])
    expect(readImageRequest).toHaveBeenCalledTimes(1)
  })

  it('keeps every image at exactly the payload bound and drops all of them when even the newest cannot fit', async () => {
    const sized: ImageAttachmentRef = { ...ref, bytes: 3 }
    const exact = await toPiContext(request([
      user([{ type: 'image', attachment: sized }]),
      user([{ type: 'image', attachment: sized }]),
    ]), imageContext(attachments, { maxRequestImageBytes: 8 }))
    expect(exact.messages).toEqual([
      {
        role: 'user',
        content: [expect.objectContaining({ type: 'text' }), expect.objectContaining({ type: 'image' })],
        timestamp: 0,
      },
      {
        role: 'user',
        content: [expect.objectContaining({ type: 'text' }), expect.objectContaining({ type: 'image' })],
        timestamp: 0,
      },
    ])

    const readImageRequest = vi.fn((value: ImageAttachmentRef) => (
      Promise.resolve(requestImage(value, new Uint8Array(300)))
    ))
    const store = projectionStore(readImageRequest)
    const oversized = await toPiContext(request([
      user([{ type: 'image', attachment: { ...ref, bytes: 300 } }]),
    ]), imageContext(store, { maxRequestImageBytes: 8 }))
    // All-text content collapses to the string form; the placeholder still reaches the model.
    expect(oversized.messages).toEqual([
      { role: 'user', content: offloadedImageText({ ...ref, bytes: 300 }), timestamp: 0 },
    ])
    expect(readImageRequest).not.toHaveBeenCalled()
  })

  it('offloads repeated image-block occurrences by position rather than shared object identity', async () => {
    const sized: ImageAttachmentRef = { ...ref, bytes: 3 }
    const shared: ContentBlock = { type: 'image', attachment: sized }
    const readImageRequest = vi.fn((value: ImageAttachmentRef) => (
      Promise.resolve(requestImage(value, Uint8Array.of(1, 2, 3)))
    ))
    const store = projectionStore(readImageRequest)
    const aliased = await toPiContext(
      request([user([shared, shared])]),
      imageContext(store, { maxRequestImageBytes: 4 }),
    )
    const replayed = await toPiContext(request([user([
      { type: 'image', attachment: { ...sized } },
      { type: 'image', attachment: { ...sized } },
    ])]), imageContext(store, { maxRequestImageBytes: 4 }))

    const expected = [{
      role: 'user',
      content: [
        { type: 'text', text: offloadedImageText(sized) },
        { type: 'text', text: expect.stringContaining(`Image ${sized.attachmentId}`) as string },
        { type: 'image', data: 'AQID', mimeType: 'image/png' },
      ],
      timestamp: 0,
    }]
    expect(aliased.messages).toEqual(expected)
    expect(replayed.messages).toEqual(expected)
    expect(readImageRequest).toHaveBeenCalledTimes(2)
  })

  it('keeps empty text-only users while separating result-only messages', () => {
    const callId = ToolCallId('unknown-call')
    expect(toPiContext(request([
      user([]),
      history('assistant', [
        { type: 'text', text: 'answer' },
        { type: 'tool-call', id: ToolCallId('other-call'), name: 'lookup', arguments: '{}' },
      ]),
      user([{
        type: 'tool-result',
        toolCallId: callId,
        content: [{ type: 'text', text: 'result' }],
      }]),
    ]))).toMatchObject({
      messages: [
        { role: 'user', content: '' },
        { role: 'assistant' },
        { role: 'toolResult', toolName: 'unknown' },
      ],
    })
  })

  it('handles in-history system and assistant messages explicitly on the image path', async () => {
    for (const role of ['system', 'assistant'] as const) {
      const readImageRequest = vi.fn()
      const store = projectionStore(readImageRequest)
      await expect(toPiContext(request([
        history(role, [{ type: 'image', attachment: ref }]),
      ]), imageContext(store, { maxRequestImageBytes: 1 }))).rejects.toMatchObject({ code: 'UNSUPPORTED_CONTENT' })
      expect(readImageRequest).not.toHaveBeenCalled()
    }

    await expect(toPiContext(request([
      history('system', [{ type: 'text', text: 'history system' }]),
      history('assistant', [{ type: 'text', text: 'answer' }]),
      user([{ type: 'text', text: 'plain' }]),
    ]), imageContext(attachments))).resolves.toMatchObject({
      messages: [
        { role: 'user', content: 'history system' },
        { role: 'assistant' },
        { role: 'user', content: 'plain' },
      ],
    })

    expect(() => toPiAssistant(
      history('assistant', [{ type: 'image', attachment: ref }]),
    )).toThrow(/assistant image output/)
  })

})
