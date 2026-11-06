// The browser-facing control surface: catalog assembly against the live Agent
// registry, prompt admission, and the stable failure codes each answers with.
// The durable listing, continuation, and interrupt primitives they wrap have
// their own specs, so each case scripts them.

import { describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import type { MessageId } from '@deepseek-ai/dsh-llm'
import { SessionId } from '@deepseek-ai/dsh-session'
import SubagentRuntime, {
  SubagentError,
  type SubagentListEntry,
  type SubagentPromptRequestId,
} from '@deepseek-ai/dsh-subagent'

const PARENT = SessionId('parent')
const CHILD = SessionId('child')
const OTHER = SessionId('other')
const BROKEN = SessionId('broken')
const REQUEST_ID = 'req-1' as SubagentPromptRequestId
const signal = new AbortController().signal

/** The runtime plus a programmable live-Agent registry, omitted to compose none. */
async function bench(live?: Record<string, { status: 'running' | 'idle' }>) {
  const ctx = new Context()
  await ctx.plugin(SubagentRuntime)
  if (live !== undefined) {
    ctx.provide('agents', { get: (id: SessionId) => live[id] } as never)
  }
  return { ctx, subagents: ctx.subagents }
}

function childRow(id: SessionId, activity: 'running' | 'inactive'): SubagentListEntry {
  return { kind: 'child', id, mode: 'continuable', label: 'worker', activity, hasChildren: false }
}

function promptRequest(clientTimeZone?: string) {
  return {
    requestId: REQUEST_ID,
    parentSessionId: PARENT,
    childSessionId: CHILD,
    mode: 'continuable' as const,
    content: [{ type: 'text' as const, text: 'continue' }],
    ...clientTimeZone === undefined ? {} : { clientTimeZone },
  }
}

function emptyIdFailure(method: string, field: string) {
  return {
    code: 'gateway/bad-request',
    message: `invalid payload for ${method}`,
    details: {
      issues: [{
        origin: 'string',
        code: 'too_small',
        minimum: 1,
        inclusive: true,
        path: [field],
        message: 'Too small: expected string to have >=1 characters',
      }],
    },
  }
}

describe('subagent catalog Remote', () => {
  it('rejects an empty parent id before listing', async () => {
    const { subagents } = await bench()
    const listChildren = vi.spyOn(subagents, 'listChildren')

    await expect(subagents.remoteExportList(SessionId(''), signal))
      .rejects.toMatchObject(emptyIdFailure('subagent.list', 'parentSessionId'))
    expect(listChildren).not.toHaveBeenCalled()
  })

  it('samples row activity from the live Agent driver and reports parent availability', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' }, [CHILD]: { status: 'running' } })
    vi.spyOn(subagents, 'listChildren').mockResolvedValue([
      // The durable listing reports store presence; the browser row reports the driver.
      childRow(CHILD, 'inactive'),
      childRow(OTHER, 'running'),
      { kind: 'diagnostic', id: BROKEN, reason: 'corrupt' },
    ])

    await expect(subagents.remoteExportList(PARENT, signal)).resolves.toEqual({
      entries: [
        childRow(CHILD, 'running'),
        childRow(OTHER, 'inactive'),
        { kind: 'diagnostic', id: BROKEN, reason: 'corrupt' },
      ],
      parentAvailable: true,
    })
  })

  it('reports every row inactive and the parent unavailable without an Agent registry', async () => {
    const { subagents } = await bench()
    vi.spyOn(subagents, 'listChildren').mockResolvedValue([childRow(CHILD, 'running')])

    await expect(subagents.remoteExportList(PARENT, signal)).resolves.toEqual({
      entries: [childRow(CHILD, 'inactive')],
      parentAvailable: false,
    })
  })

  it('reports an unknown parent as unavailable while the registry serves other sessions', async () => {
    const { subagents } = await bench({ [CHILD]: { status: 'running' } })
    vi.spyOn(subagents, 'listChildren').mockResolvedValue([])

    await expect(subagents.remoteExportList(PARENT, signal))
      .resolves.toEqual({ entries: [], parentAvailable: false })
  })

  it('separates cancellation, the missing projections capability, and an unexplained read failure', async () => {
    const { subagents } = await bench()
    const listChildren = vi.spyOn(subagents, 'listChildren')

    const aborted = new AbortController()
    aborted.abort()
    listChildren.mockRejectedValue(new Error('read stopped'))
    await expect(subagents.remoteExportList(PARENT, aborted.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })

    listChildren.mockRejectedValue(new SubagentError('cancelled', 'CANCELLED'))
    await expect(subagents.remoteExportList(PARENT, signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })

    listChildren.mockRejectedValue(
      new SubagentError('no registry', 'SUBAGENT_CONTROL_PROJECTIONS_UNAVAILABLE'),
    )
    await expect(subagents.remoteExportList(PARENT, signal)).rejects.toMatchObject({
      code: 'subagent/projections-unavailable',
      message: expect.stringContaining('sessionProjections') as unknown as string,
    })

    listChildren.mockRejectedValue(new Error('disk gone'))
    await expect(subagents.remoteExportList(PARENT, signal))
      .rejects.toMatchObject({ code: 'gateway/internal', message: 'subagent catalog read failed' })
  })
})

describe('subagent prompt Remote', () => {
  it('rejects empty parent and child ids before delivery', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup')

    const cases: readonly {
      readonly field: 'parentSessionId' | 'childSessionId'
      readonly request: ReturnType<typeof promptRequest>
    }[] = [
      { field: 'parentSessionId', request: { ...promptRequest(), parentSessionId: SessionId('') } },
      { field: 'childSessionId', request: { ...promptRequest(), childSessionId: SessionId('') } },
    ]
    for (const { field, request } of cases) {
      await expect(subagents.prompt(request, signal))
        .rejects.toMatchObject(emptyIdFailure('subagent.prompt', field))
    }
    expect(followup).not.toHaveBeenCalled()
  })

  it('forwards non-text content blocks without narrowing them', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup').mockResolvedValue('m-content' as MessageId)
    const content = [{ type: 'reasoning' as const, text: 'retain this block' }]

    await expect(subagents.prompt({ ...promptRequest(), content }, signal))
      .resolves.toEqual({ messageId: 'm-content' })
    expect(followup.mock.calls[0]?.[2]).toEqual(content)
  })

  it('refuses an image the wire accepts, whatever else the content carries', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup')
    // Both variants the widened wire admits: the browser's encoded upload and a
    // durable reference. The Client narrows neither, so the Host answers both.
    const encoded = { type: 'image' as const, mediaType: 'image/png' as const, data: 'AAA=' }
    const durable = { type: 'image' as const, attachment: { attachmentId: 'att-1' } as never }
    for (const image of [encoded, durable]) {
      const content = [{ type: 'text' as const, text: 'look at this' }, image]
      await expect(subagents.prompt({ ...promptRequest(), content }, signal)).rejects.toMatchObject({
        code: 'subagent/attachment-unsupported',
        details: { childSessionId: CHILD, reason: 'SUBAGENT_IMAGE_UNSUPPORTED' },
      })
    }
    expect(followup).not.toHaveBeenCalled()
  })

  it('delivers the content under the caller-minted identity and canonical browser zone', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup').mockResolvedValue('m-1' as MessageId)

    await expect(subagents.prompt(promptRequest('Asia/Shanghai'), signal))
      .resolves.toEqual({ messageId: 'm-1' })
    expect(followup).toHaveBeenCalledWith(
      { status: 'idle' },
      CHILD,
      [{ type: 'text', text: 'continue' }],
      {
        source: { kind: 'user', rpcId: REQUEST_ID, clientTimeZone: 'Asia/Shanghai' },
        signal,
      },
    )
  })

  it('omits the zone from the durable source when the browser reported none', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup').mockResolvedValue('m-2' as MessageId)

    await expect(subagents.prompt(promptRequest(), signal)).resolves.toEqual({ messageId: 'm-2' })
    expect(followup.mock.calls[0]?.[3].source).toEqual({ kind: 'user', rpcId: REQUEST_ID })
  })

  it('accepts UTC and rejects an empty, untrimmed, malformed, or unknown zone', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    vi.spyOn(subagents, 'followup').mockResolvedValue('m-3' as MessageId)

    await expect(subagents.prompt(promptRequest('UTC'), signal)).resolves.toEqual({ messageId: 'm-3' })
    for (const zone of ['', ' UTC', 'Shanghai', 'Nowhere/Nowhere']) {
      await expect(subagents.prompt(promptRequest(zone), signal)).rejects.toMatchObject({
        code: 'subagent/invalid-time-zone', details: { value: zone },
      })
    }
  })

  it('refuses delivery when the exact parent Agent is not live', async () => {
    const { subagents } = await bench()
    const followup = vi.spyOn(subagents, 'followup')

    await expect(subagents.prompt(promptRequest(), signal)).rejects.toMatchObject({
      code: 'subagent/parent-unavailable', details: { parentSessionId: PARENT },
    })
    expect(followup).not.toHaveBeenCalled()
  })

  it('maps each admission failure onto its stable code and hides the rest', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const followup = vi.spyOn(subagents, 'followup')
    const cases: readonly [string, string][] = [
      ['NOT_RESUMABLE', 'subagent/not-resumable'],
      ['UNAUTHORIZED', 'subagent/unauthorized'],
      ['DRAINING', 'subagent/delivery-unavailable'],
      ['ACTIVATION_CLOSING', 'subagent/delivery-unavailable'],
      ['NO_PROVIDER', 'gateway/internal'],
    ]
    for (const [thrown, code] of cases) {
      followup.mockRejectedValue(new SubagentError('refused', thrown))
      await expect(subagents.prompt(promptRequest(), signal))
        .rejects.toMatchObject({ code })
    }

    followup.mockRejectedValue(new Error('inbox exploded'))
    await expect(subagents.prompt(promptRequest(), signal))
      .rejects.toMatchObject({ code: 'gateway/internal', message: 'subagent prompt failed' })
  })

  it('answers a caller-cancelled delivery as cancelled rather than a failure', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    const aborted = new AbortController()
    vi.spyOn(subagents, 'followup').mockImplementation(() => {
      aborted.abort()
      return Promise.reject(new SubagentError('gone', 'NOT_RESUMABLE'))
    })

    await expect(subagents.prompt(promptRequest(), aborted.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })
  })

  it('preserves a cancellation reported by the continuation operation', async () => {
    const { subagents } = await bench({ [PARENT]: { status: 'idle' } })
    vi.spyOn(subagents, 'followup')
      .mockRejectedValue(new SubagentError('stopped', 'CANCELLED'))

    await expect(subagents.prompt(promptRequest(), signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })
  })
})

describe('subagent interrupt Remote', () => {
  it('rejects empty child and parent ids before interrupting', async () => {
    const { subagents } = await bench()
    const interrupt = vi.spyOn(subagents, 'interrupt')

    for (const [childSessionId, parentSessionId] of [
      [SessionId(''), PARENT],
      [CHILD, SessionId('')],
    ] as const) {
      const field = childSessionId.length === 0 ? 'childSessionId' : 'parentSessionId'
      expect(() => subagents.interruptByParent(childSessionId, parentSessionId, 'continuable'))
        .toThrow(expect.objectContaining(emptyIdFailure('subagent.interrupt', field)))
    }
    expect(interrupt).not.toHaveBeenCalled()
  })

  it('admits the parent-addressed interrupt and acknowledges it', async () => {
    const { subagents } = await bench()
    const interrupt = vi.spyOn(subagents, 'interrupt').mockReturnValue()

    expect(subagents.interruptByParent(CHILD, PARENT, 'continuable')).toEqual({ accepted: true })
    expect(interrupt).toHaveBeenCalledWith(CHILD, { kind: 'user', parentSessionId: PARENT })
  })

  it('answers a foreign address as unauthorized and everything else as internal', async () => {
    const { subagents } = await bench()
    const interrupt = vi.spyOn(subagents, 'interrupt')

    interrupt.mockImplementation(() => { throw new SubagentError('not yours', 'UNAUTHORIZED') })
    expect(() => subagents.interruptByParent(CHILD, PARENT, 'continuable')).toThrow(
      expect.objectContaining({
        code: 'subagent/unauthorized',
        message: expect.any(String) as unknown as string,
        details: { childSessionId: CHILD },
      }),
    )

    interrupt.mockImplementation(() => { throw new Error('boom') })
    expect(() => subagents.interruptByParent(CHILD, PARENT, 'continuable')).toThrow(
      expect.objectContaining({ code: 'gateway/internal', message: 'subagent interrupt failed', details: {} }),
    )
  })
})
