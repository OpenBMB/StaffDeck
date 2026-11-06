/** Test-only direct Remote face over the Session Controller's internal controllers. */

import type { Context } from '@deepseek-ai/cordis'
import type { ModelSelection as AgentModelSelection } from '@deepseek-ai/dsh-agent'
import type { SessionId } from '@deepseek-ai/dsh-session'
import {
  SessionPersistenceCorruptionError,
  SessionPersistenceNotFoundError,
  SessionPersistenceRevision,
  type BorrowedSessionSource,
  type SessionInspection,
} from '@deepseek-ai/dsh-session-persistence'
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection'
import SessionQueryEngine from '@deepseek-ai/dsh-session-query'
import { vi } from 'vitest'
import {
  RemoteError,
  remoteErrorOf,
  type RemoteResult,
} from '@deepseek-ai/dsh-typert-protocol'
import SessionController from '../src/index.ts'
import type {
  ModelCatalog,
  SessionAttachmentRequest,
  SessionAttachmentValue,
  SessionCancelRequest,
  SessionCancelValue,
  SessionControlFrame,
  SessionCreateRequest,
  SessionCreateValue,
  SessionForkRequest,
  SessionForkValue,
  SessionFollowFrame,
  SessionFollowRequest,
  SessionListRequest,
  SessionListValue,
  SessionOpenWorkspacePathRequest,
  SessionOpenWorkspacePathValue,
  SessionPage,
  SessionPageRequest,
  SessionPromptRequest,
  SessionPromptValue,
  SessionRenameRequest,
  SessionRenameValue,
  SessionSearchRequest,
  SessionSearchValue,
  SessionSelectModelRequest,
  SessionSelectModelValue,
  SessionUpdateQueueRequest,
  SessionUpdateQueueValue,
} from '../src/types.ts'

/** Direct test face matching the generated `ctx.remote.session` unary methods. */
export interface TestSessionRemote {
  canOpenWorkspacePath(): Promise<RemoteResult<boolean>>
  list(request: SessionListRequest, signal?: AbortSignal): Promise<RemoteResult<SessionListValue>>
  search(request: SessionSearchRequest, signal?: AbortSignal): Promise<RemoteResult<SessionSearchValue>>
  create(request: SessionCreateRequest): Promise<RemoteResult<SessionCreateValue>>
  selectModel(request: SessionSelectModelRequest): Promise<RemoteResult<SessionSelectModelValue>>
  modelCatalog(): Promise<RemoteResult<ModelCatalog>>
  rename(request: SessionRenameRequest): Promise<RemoteResult<SessionRenameValue>>
  fork(request: SessionForkRequest): Promise<RemoteResult<SessionForkValue>>
  prompt(request: SessionPromptRequest, signal?: AbortSignal): Promise<RemoteResult<SessionPromptValue>>
  attachment(request: SessionAttachmentRequest): Promise<RemoteResult<SessionAttachmentValue>>
  updateQueue(request: SessionUpdateQueueRequest): Promise<RemoteResult<SessionUpdateQueueValue>>
  cancel(request: SessionCancelRequest): Promise<RemoteResult<SessionCancelValue>>
  openWorkspacePath(
    request: SessionOpenWorkspacePathRequest,
    signal?: AbortSignal,
  ): Promise<RemoteResult<SessionOpenWorkspacePathValue>>
  page(request: SessionPageRequest, signal?: AbortSignal): Promise<RemoteResult<SessionPage>>
  follow(request: SessionFollowRequest, signal?: AbortSignal): AsyncIterable<SessionFollowFrame>
  control(signal?: AbortSignal): AsyncIterable<SessionControlFrame>
}

/** Dependencies and policy supplied by a Session Controller unit harness. */
export interface TestSessionRemoteDefaults {
  readonly defaultModelSelection: () => AgentModelSelection
  readonly cwd: string
  readonly coldBlankProbeMaxBytes?: number
  readonly nativeOpen?: boolean
  readonly saveDefaultModelSelection?: (selection: AgentModelSelection) => void | Promise<void>
  readonly openPath?: (path: string, signal: AbortSignal) => Promise<void>
  readonly canOpenPath?: () => boolean
}

const installed = new WeakMap<Context, SessionController>()

type LegacyTestPersistence = Record<string, unknown> & {
  readonly inspect?: (
    sessionId: SessionId,
    signal?: AbortSignal,
  ) => Promise<SessionInspection | undefined>
  readonly borrowSession?: (
    sessionId: SessionId,
    signal?: AbortSignal,
  ) => Promise<BorrowedSessionSource>
}

/** Add the preparation-backed point-read contract to compact persistence doubles. */
export function testSessionPersistence(
  ctx: Context,
  persistence: LegacyTestPersistence,
): LegacyTestPersistence {
  if (persistence.borrowSession !== undefined) return persistence
  return {
    ...persistence,
    borrowSession: async (sessionId, signal) => {
      signal?.throwIfAborted()
      const inspection = await persistence.inspect?.(sessionId, signal)
      signal?.throwIfAborted()
      if (inspection === undefined) throw new SessionPersistenceNotFoundError(sessionId)
      try {
        const preparedSession = ctx.sessions.prepare(inspection.meta.id, {
          seed: [...inspection.events],
          meta: inspection.meta,
          seedSource: 'persistence',
        })
        return {
          source: 'prepared',
          inspection: {
            meta: preparedSession.header,
            events: Object.freeze([...inspection.events]),
          },
          revision: SessionPersistenceRevision(`test:${sessionId}:${String(preparedSession.seq)}`),
          preparedSession,
          [Symbol.dispose]: () => {},
        }
      } catch (error: unknown) {
        throw new SessionPersistenceCorruptionError(
          `test session "${sessionId}" failed validation: ${String(error)}`,
          { cause: error },
        )
      }
    },
  }
}

/** Concrete point-read query used by Session Controller tests that do not exercise search. */
class TestSessionQuery extends SessionQueryEngine {
  override searchSessions(): Promise<never> {
    return Promise.reject(new Error('session search is not configured in this test'))
  }

  override searchEvents(): Promise<never> {
    return Promise.reject(new Error('event search is not configured in this test'))
  }
}

/** Install the required projection and point-query services for direct controller tests. */
export function installSessionReadTestServices(ctx: Context): void {
  if (ctx.get('sessionProjections') === undefined) new SessionProjectionRegistry(ctx)
  if (ctx.get('sessionQuery') === undefined) new TestSessionQuery(ctx)
}

function installControllers(
  ctx: Context,
  defaults: TestSessionRemoteDefaults,
): SessionController {
  const found = installed.get(ctx)
  if (found !== undefined) return found

  if (ctx.get('typert') === undefined) {
    const dispose = (): void => {}
    ctx.provide('typert', {
      lookups: { configure: () => dispose },
      contexts: { configureHost: () => dispose },
    } as never)
  }
  if (ctx.get('agentDefaultModel') === undefined) {
    ctx.provide('agentDefaultModel', {
      currentSelection: defaults.defaultModelSelection,
      saveSelection: async (selection: AgentModelSelection) => {
        await defaults.saveDefaultModelSelection?.(selection)
      },
    } as never)
  }
  if (ctx.get('llm') === undefined) {
    ctx.provide('llm', {
      listProviders: () => {
        const selection = defaults.defaultModelSelection()
        return [{ id: selection.provider, name: selection.provider }]
      },
    } as never)
  }
  installSessionReadTestServices(ctx)
  const cwd = vi.spyOn(process, 'cwd').mockReturnValue(defaults.cwd)
  let controller: SessionController
  try {
    controller = new SessionController(
      ctx,
      {
        ...defaults.coldBlankProbeMaxBytes === undefined
          ? {}
          : { coldBlankProbeMaxBytes: defaults.coldBlankProbeMaxBytes },
        ...defaults.nativeOpen === undefined ? {} : { nativeOpen: defaults.nativeOpen },
      },
      {
        ...defaults.openPath === undefined ? {} : { openPath: defaults.openPath },
        ...defaults.canOpenPath === undefined ? {} : { canOpenPath: defaults.canOpenPath },
      },
    )
  } finally {
    cwd.mockRestore()
  }
  installed.set(ctx, controller)
  return controller
}

/** Build or return the production Session Controller for a direct unit harness. */
export function createSessionTestController(
  ctx: Context,
  defaults: TestSessionRemoteDefaults,
): SessionController {
  return installControllers(ctx, defaults)
}

function remoteResult<T>(
  operation: () => T | Promise<T>,
  signal?: AbortSignal,
): Promise<RemoteResult<T>> {
  return Promise.resolve()
    .then(operation)
    .then(value => ({ ok: true as const, value }))
    .catch((error: unknown) => ({
      ok: false as const,
      error: signal?.aborted === true
        ? new RemoteError('gateway/cancelled', 'request was aborted', {})
        : remoteErrorOf(error)
          ?? new RemoteError(
            'gateway/internal',
            error instanceof Error ? error.message : String(error),
            {},
          ),
    }))
}

/** Build the generated Session Remote's unary result semantics without a carrier. */
export function createSessionTestRemote(
  ctx: Context,
  defaults: TestSessionRemoteDefaults,
): TestSessionRemote {
  const direct = createSessionTestController(ctx, defaults)
  return {
    canOpenWorkspacePath: () => remoteResult(() => direct.canOpenWorkspacePath()),
    list: (request, signal = new AbortController().signal) => remoteResult(
      () => direct.list(request, signal),
      signal,
    ),
    search: (request, signal = new AbortController().signal) => remoteResult(
      () => direct.search(request, signal),
      signal,
    ),
    create: request => remoteResult(() => direct.create(request)),
    selectModel: request => remoteResult(() => direct.selectModel(request)),
    modelCatalog: () => remoteResult(() => direct.modelCatalog()),
    rename: request => remoteResult(() => direct.rename(request)),
    fork: request => remoteResult(() => direct.fork(request)),
    prompt: (request, signal = new AbortController().signal) => remoteResult(
      () => direct.prompt(request, signal),
      signal,
    ),
    attachment: request => remoteResult(() => direct.attachment(request)),
    updateQueue: request => remoteResult(() => direct.updateQueue(request)),
    cancel: request => remoteResult(() => direct.cancel(request)),
    openWorkspacePath: (request, signal = new AbortController().signal) => remoteResult(
      () => direct.openWorkspacePath(request, signal),
      signal,
    ),
    page: (request, signal = new AbortController().signal) => remoteResult(
      () => direct.page(request, signal),
      signal,
    ),
    follow: (request, signal = new AbortController().signal) => direct.follow(request, signal),
    control: (signal = new AbortController().signal) => direct.control(signal),
  }
}
