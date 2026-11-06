import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it, vi } from 'vitest'
import {
  RemoteStream,
  RemoteStreamCarrierError,
  type ClientRemote,
  type RemoteStreamOptions,
} from '@deepseek-ai/dsh-api-gateway/client'
import type { ConnectionHandle } from '@deepseek-ai/dsh-client-connection/client'
import { SessionId } from '@deepseek-ai/dsh-session/types'
import { RemoteError, type RemoteFailure, type RemoteResult } from '@deepseek-ai/dsh-typert-protocol'
import * as WorkspaceClientPlugin from '../src/client/index.ts'
import {
  ClientWorkspaceModel,
  createWorkspaceStateStream,
  WorkspaceController,
  WorkspaceCreateError,
  type WorkspaceFollowSink,
  type WorkspaceRemote,
} from '../src/client/index.ts'
import type {
  WorkspaceArchiveSessionRequest,
  WorkspaceArchiveValue,
  WorkspaceCreateRequest,
  WorkspaceCreateValue,
  WorkspaceDeleteRequest,
  WorkspaceDeleteValue,
  WorkspaceFollowFrame,
  WorkspaceInsertBeforeRequest,
  WorkspaceInsertSessionBeforeRequest,
  WorkspaceOrderValue,
  WorkspaceRenameRequest,
  WorkspaceId,
  WorkspaceValue,
  WorkspaceView,
} from '../src/types.ts'

const AVAILABLE_CONNECTION = {
  generation: {
    getSnapshot: () => ({ id: 1, host: { home: '/home/fixture' } }),
    subscribe: () => () => {},
  },
}

function workspaceClient(
  remote: WorkspaceRemote,
  connection: Pick<ConnectionHandle, 'generation'> = AVAILABLE_CONNECTION,
): ClientRemote {
  return {
    workspace: remote,
    $stream: <Item>(options: RemoteStreamOptions<Item>) => new RemoteStream(connection, options),
  } as unknown as ClientRemote
}

interface Generation {
  readonly frames: readonly WorkspaceFollowFrame[]
  readonly error?: unknown
  readonly hold?: boolean
  readonly afterAbort?: () => void
  readonly afterAbortError?: unknown
}

const baseline = (id?: string): Extract<WorkspaceFollowFrame, { type: 'baseline' }> => ({
  type: 'baseline',
  value: {
    items: id === undefined ? [] : [{
      workspaceId: id as never,
      path: `/work/${id}`,
      title: id,
      sessionIds: [],
      createdAt: '2026-01-01T00:00:00.000Z',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }],
    archivedSessionIds: [],
  },
})

const wid = (id: string): WorkspaceId => id as WorkspaceId
const sid = (id: string): SessionId => SessionId(id)

function workspace(id: string, overrides: Partial<WorkspaceView> = {}): WorkspaceView {
  return {
    workspaceId: wid(id),
    path: `/work/${id}`,
    title: id,
    sessionIds: [],
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function remoteOk<T>(value: T): RemoteResult<T> {
  return { ok: true, value }
}

function remoteFailure(error: RemoteFailure): RemoteResult<never> {
  return { ok: false, error }
}

function accepts(overrides: Partial<WorkspaceFollowSink> = {}): WorkspaceFollowSink {
  const ignore = (): void => {}
  return {
    replaceBaseline: ignore,
    upsertView: ignore,
    removeView: ignore,
    replaceOrder: ignore,
    replaceArchived: ignore,
    ...overrides,
  }
}

class ScriptedWorkspaceRemote implements WorkspaceRemote {
  readonly signals: AbortSignal[] = []
  calls = 0

  constructor(private readonly generations: readonly Generation[]) {}

  create(_request: WorkspaceCreateRequest): Promise<RemoteResult<WorkspaceCreateValue>> {
    throw new Error('unused')
  }

  rename(_request: WorkspaceRenameRequest): Promise<RemoteResult<WorkspaceValue>> {
    throw new Error('unused')
  }

  delete(_request: WorkspaceDeleteRequest): Promise<RemoteResult<WorkspaceDeleteValue>> {
    throw new Error('unused')
  }

  insertBefore(_request: WorkspaceInsertBeforeRequest): Promise<RemoteResult<WorkspaceOrderValue>> {
    throw new Error('unused')
  }

  insertSessionBefore(_request: WorkspaceInsertSessionBeforeRequest): Promise<RemoteResult<WorkspaceValue>> {
    throw new Error('unused')
  }

  archiveSession(_request: WorkspaceArchiveSessionRequest): Promise<RemoteResult<WorkspaceArchiveValue>> {
    throw new Error('unused')
  }

  async *follow(signal = new AbortController().signal): AsyncIterable<WorkspaceFollowFrame> {
    const generation = this.generations[this.calls++]
    if (generation === undefined) throw new Error('no scripted Workspace generation')
    this.signals.push(signal)
    for (const frame of generation.frames) yield frame
    if (generation.error !== undefined) throw generation.error
    if (generation.hold === true && !signal.aborted) {
      await new Promise<void>((resolve) => {
        signal.addEventListener('abort', () => { resolve() }, { once: true })
      })
      generation.afterAbort?.()
      if (generation.afterAbortError !== undefined) throw generation.afterAbortError
    }
  }
}

class CommandWorkspaceRemote implements WorkspaceRemote {
  readonly create = vi.fn<WorkspaceRemote['create']>(request => Promise.resolve(remoteOk({
    workspace: workspace('created', { path: request.path }),
    created: true,
  })))

  readonly rename = vi.fn<WorkspaceRemote['rename']>(request => Promise.resolve(remoteOk({
    workspace: workspace(String(request.workspaceId), { title: request.title }),
  })))

  readonly delete = vi.fn<WorkspaceRemote['delete']>(() => Promise.resolve(remoteOk({ deleted: true })))

  readonly insertBefore = vi.fn<WorkspaceRemote['insertBefore']>(request => Promise.resolve(remoteOk({
    workspaceIds: [request.workspaceId],
  })))

  readonly insertSessionBefore = vi.fn<WorkspaceRemote['insertSessionBefore']>(request => Promise.resolve(remoteOk({
    workspace: workspace(String(request.workspaceId), { sessionIds: [request.sessionId] }),
  })))

  readonly archiveSession = vi.fn<WorkspaceRemote['archiveSession']>(request => Promise.resolve(remoteOk({
    archivedSessionIds: [request.sessionId],
  })))

  async *follow(_signal?: AbortSignal): AsyncIterable<WorkspaceFollowFrame> {}
}

async function waitFor(check: () => void): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt++) {
    try {
      check()
      return
    } catch {
      await Promise.resolve()
    }
  }
  check()
}

function provideClientServices(ctx: Context, remote: WorkspaceRemote): void {
  const connection: ConnectionHandle = {
    isLoopback: true,
    generation: AVAILABLE_CONNECTION.generation,
    state: { getSnapshot: () => 'connected' as const, subscribe: () => () => {} },
    rpc: {
      call: () => Promise.reject(new Error('unexpected generic RPC call')),
    },
    reconnect: () => {},
    registerGenerationSource: () => () => {},
    start: () => ({ stop: () => {} }),
  }
  ctx.reflect.provide('connection', connection)
  ctx.reflect.provide('remote', workspaceClient(remote, connection))
  ctx.reflect.provide('remote.workspace', remote)
}

describe('Workspace Controller Client apply', () => {
  it('provides the Workspace service and stops its follow generation with the plugin fiber', async () => {
    const ctx = new Context()
    const remote = new ScriptedWorkspaceRemote([{ frames: [baseline('mounted')], hold: true }])
    provideClientServices(ctx, remote)
    const fiber = ctx.plugin(WorkspaceClientPlugin)
    await fiber
    await waitFor(() => {
      expect(ctx.workspaces.list.getSnapshot()).toMatchObject({
        phase: 'ready',
        state: 'idle',
        items: [{ workspaceId: 'mounted' }],
      })
    })

    await fiber.dispose()

    expect(remote.signals[0]?.aborted).toBe(true)
    expect(ctx.get('workspaces')).toBeUndefined()
  })

  it('publishes exhausted carrier retries as a gateway/internal error state', async () => {
    const ctx = new Context()
    // Neither generation reaches an accepted baseline, so the retry budget runs
    // out and the escaping carrier failure crosses the stream boundary marked.
    const remote = new ScriptedWorkspaceRemote([
      { frames: [], error: new RemoteStreamCarrierError('generation lost') },
      { frames: [], error: new RemoteStreamCarrierError('generation lost again') },
    ])
    provideClientServices(ctx, remote)
    const fiber = ctx.plugin(WorkspaceClientPlugin)
    await fiber
    await waitFor(() => {
      expect(ctx.workspaces.list.getSnapshot()).toMatchObject({
        state: 'error',
        error: { code: 'gateway/internal', message: 'generation lost again' },
      })
    })
    expect(remote.calls).toBe(2)
    await fiber.dispose()
  })

  it('marks carrier loss while retrying and publishes a later protocol failure', async () => {
    const ctx = new Context()
    const remote = new ScriptedWorkspaceRemote([
      {
        frames: [baseline('old')],
        error: new RemoteStreamCarrierError('generation lost'),
      },
      { frames: [baseline('fresh'), baseline('duplicate')] },
    ])
    provideClientServices(ctx, remote)
    const carrierFailure = vi.spyOn(ClientWorkspaceModel.prototype, 'handleCarrierFailure')
    const streamFailure = vi.spyOn(ClientWorkspaceModel.prototype, 'handleStreamFailure')
    const fiber = ctx.plugin(WorkspaceClientPlugin)
    await fiber
    await waitFor(() => {
      expect(ctx.workspaces.list.getSnapshot()).toMatchObject({
        phase: 'ready',
        state: 'error',
        items: [{ workspaceId: 'fresh' }],
        error: { code: 'gateway/internal', message: 'Workspace state stream emitted more than one opening snapshot' },
      })
    })

    expect(carrierFailure).toHaveBeenCalledOnce()
    expect(streamFailure).toHaveBeenCalledOnce()
    await fiber.dispose()
  })
})

describe('Workspace state stream', () => {
  it('delivers one baseline followed by increments', async () => {
    const opening = baseline('one')
    const workspace = opening.value.items[0]!
    const remote = new ScriptedWorkspaceRemote([{
      frames: [
        opening,
        { type: 'upsert', workspace },
        { type: 'remove', workspaceId: workspace.workspaceId },
        { type: 'order', workspaceIds: [workspace.workspaceId] },
        { type: 'archived', archivedSessionIds: ['session-one' as never] },
      ],
      hold: true,
    }])
    const replaceBaseline = vi.fn<WorkspaceFollowSink['replaceBaseline']>()
    const upsertView = vi.fn<WorkspaceFollowSink['upsertView']>()
    const removeView = vi.fn<WorkspaceFollowSink['removeView']>()
    const replaceOrder = vi.fn<WorkspaceFollowSink['replaceOrder']>()
    const replaceArchived = vi.fn<WorkspaceFollowSink['replaceArchived']>()
    const accept = accepts({
      replaceBaseline,
      upsertView,
      removeView,
      replaceOrder,
      replaceArchived,
    })
    const stream = createWorkspaceStateStream(workspaceClient(remote), {
      accept,
      failed: vi.fn(),
    })

    stream.start()
    stream.start()
    await vi.waitFor(() => { expect(replaceArchived).toHaveBeenCalledOnce() })

    expect(replaceBaseline).toHaveBeenCalledWith(opening.value)
    expect(upsertView).toHaveBeenCalledWith(workspace)
    expect(removeView).toHaveBeenCalledWith(workspace.workspaceId)
    expect(replaceOrder).toHaveBeenCalledWith([workspace.workspaceId])
    expect(replaceArchived).toHaveBeenCalledWith(['session-one'])
    await stream.dispose()
    expect(remote.signals[0]?.aborted).toBe(true)
  })

  it('retains the old state across carrier loss and applies the replacement baseline', async () => {
    const carrier = new RemoteStreamCarrierError('socket lost')
    const remote = new ScriptedWorkspaceRemote([
      { frames: [baseline('old')], error: carrier },
      { frames: [baseline('fresh')], hold: true },
    ])
    const replaceBaseline = vi.fn<WorkspaceFollowSink['replaceBaseline']>()
    const carrierFailed = vi.fn()
    const failed = vi.fn()
    const stream = createWorkspaceStateStream(workspaceClient(remote), {
      accept: accepts({ replaceBaseline }),
      carrierFailed,
      failed,
    })

    stream.start()
    await vi.waitFor(() => { expect(replaceBaseline).toHaveBeenCalledTimes(2) })

    expect(replaceBaseline.mock.calls.map(([value]) => value.items[0]?.title)).toEqual(['old', 'fresh'])
    expect(carrierFailed).toHaveBeenCalledWith(carrier)
    expect(failed).not.toHaveBeenCalled()
    await stream.dispose()
  })

  it('classifies a normal end after the opening baseline as carrier loss', async () => {
    const remote = new ScriptedWorkspaceRemote([
      { frames: [baseline('old')] },
      { frames: [baseline('fresh')], hold: true },
    ])
    const replaceBaseline = vi.fn<WorkspaceFollowSink['replaceBaseline']>()
    const carrierFailed = vi.fn()
    const stream = createWorkspaceStateStream(workspaceClient(remote), {
      accept: accepts({ replaceBaseline }),
      carrierFailed,
      failed: vi.fn(),
    })

    stream.start()
    await vi.waitFor(() => { expect(replaceBaseline).toHaveBeenCalledTimes(2) })
    expect(carrierFailed.mock.calls[0]?.[0]).toMatchObject({
      message: 'Workspace state stream ended without a terminal result',
    })
    await stream.dispose()
  })

  it('suppresses callback failure after disposal begins', async () => {
    const failed = vi.fn()
    let closing: Promise<void> | undefined
    const stream = createWorkspaceStateStream(
      workspaceClient(new ScriptedWorkspaceRemote([{ frames: [baseline()] }])),
      {
        accept: accepts({
          replaceBaseline: () => {
            closing = stream.dispose()
            throw new Error('disposed callback')
          },
        }),
        failed,
      },
    )

    stream.start()
    await vi.waitFor(() => { expect(closing).toBeDefined() })
    await closing
    expect(failed).not.toHaveBeenCalled()
  })

  it.each([
    {
      name: 'an increment before the baseline',
      frames: [{ type: 'remove', workspaceId: 'one' as never }] as WorkspaceFollowFrame[],
      message: 'update before its opening snapshot',
    },
    {
      name: 'a duplicate baseline',
      frames: [baseline(), baseline()] as WorkspaceFollowFrame[],
      message: 'more than one opening snapshot',
    },
    {
      name: 'a normal end before the baseline',
      frames: [] as WorkspaceFollowFrame[],
      message: 'ended before its opening snapshot',
    },
  ])('reports $name as a terminal failure', async ({ frames, message }) => {
    const failed = vi.fn()
    const stream = createWorkspaceStateStream(
      workspaceClient(new ScriptedWorkspaceRemote([{ frames }])),
      { accept: accepts(), failed },
    )

    stream.start()
    await vi.waitFor(() => { expect(failed).toHaveBeenCalledOnce() })
    const failure: unknown = failed.mock.calls[0]?.[0]
    expect(failure).toBeInstanceOf(Error)
    if (!(failure instanceof Error)) throw new Error('expected Workspace stream failure')
    expect(failure.message).toContain(message)
    await stream.dispose()
  })

  it('restarts a live generation without reporting cancellation as failure', async () => {
    const remote = new ScriptedWorkspaceRemote([
      { frames: [baseline('first')], hold: true },
      { frames: [baseline('second')], hold: true },
    ])
    const replaceBaseline = vi.fn<WorkspaceFollowSink['replaceBaseline']>()
    const failed = vi.fn()
    const stream = createWorkspaceStateStream(workspaceClient(remote), {
      accept: accepts({ replaceBaseline }),
      failed,
    })

    stream.start()
    await vi.waitFor(() => { expect(replaceBaseline).toHaveBeenCalledOnce() })
    stream.restart()
    await vi.waitFor(() => { expect(replaceBaseline).toHaveBeenCalledTimes(2) })
    expect(failed).not.toHaveBeenCalled()
    await stream.dispose()
  })
})

describe('WorkspaceController', () => {
  it('publishes the model source and exposes successful Workspace commands', async () => {
    const remote = new CommandWorkspaceRemote()
    const model = new ClientWorkspaceModel(remote)
    model.replaceBaseline({ items: [workspace('one')], archivedSessionIds: [] })
    const controller = new WorkspaceController(new Context(), model)

    expect(controller.list).toBe(model)
    await expect(controller.create({ path: '/work/created' })).resolves.toMatchObject({ workspaceId: 'created' })
    await expect(controller.rename(wid('one'), 'renamed')).resolves.toMatchObject({ title: 'renamed' })
    await expect(controller.insertBefore(wid('one'))).resolves.toBeUndefined()
    await expect(controller.insertSessionBefore(wid('one'), sid('session'))).resolves.toMatchObject({
      sessionIds: ['session'],
    })
    await expect(controller.archiveSession(sid('session'))).resolves.toBeUndefined()
    await expect(controller.delete(wid('one'))).resolves.toBeUndefined()
  })

  it('maps generated business failures to the command facade errors', async () => {
    const remote = new CommandWorkspaceRemote()
    const controller = new WorkspaceController(new Context(), new ClientWorkspaceModel(remote))
    const missingWorkspace = new RemoteError('workspace/not-found', 'gone', { workspaceId: wid('missing') })
    const missingSession = new RemoteError('session/not-found', 'missing session', { sessionId: sid('session') })

    remote.create.mockResolvedValueOnce(remoteFailure(new RemoteError('workspace/invalid-path', 'missing path', { path: '/missing' })))
    const create = controller.create({ path: '/missing' })
    await expect(create).rejects.toBeInstanceOf(WorkspaceCreateError)
    await expect(create).rejects.toThrow('workspace/invalid-path: missing path')

    remote.rename.mockResolvedValueOnce(remoteFailure(missingWorkspace))
    await expect(controller.rename(wid('missing'), 'name')).rejects.toThrow('workspace rename failed: workspace/not-found: gone')
    remote.delete.mockResolvedValueOnce(remoteFailure(missingWorkspace))
    await expect(controller.delete(wid('missing'))).rejects.toThrow('workspace delete failed: workspace/not-found: gone')
    remote.insertBefore.mockResolvedValueOnce(remoteFailure(missingWorkspace))
    await expect(controller.insertBefore(wid('missing'))).rejects.toThrow('workspace reorder failed: workspace/not-found: gone')
    remote.archiveSession.mockResolvedValueOnce(remoteFailure(missingSession))
    await expect(controller.archiveSession(sid('session')))
      .rejects.toThrow('workspace session archive failed: session/not-found: missing session')
    remote.insertSessionBefore.mockResolvedValueOnce(remoteFailure(new RemoteError(
      'workspace/move-invalid', 'invalid move', { workspaceId: wid('missing'), sessionId: sid('session') },
    )))
    await expect(controller.insertSessionBefore(wid('missing'), sid('session')))
      .rejects.toThrow('workspace move failed: workspace/move-invalid: invalid move')
  })
})
