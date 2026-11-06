/** Scoped Remote Event wiring for the browser question consumer. */
import { Context } from '@deepseek-ai/cordis'
import { describe, expect, it, vi } from 'vitest'
import { SlotRegistry } from '@deepseek-ai/dsh-client-ui-renderer/client'
import { LocaleRuntime } from '@deepseek-ai/dsh-client-locale/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { QuestionComposer } from '../src/client/QuestionComposer.tsx'
import { PendingQuestion } from '../src/client/contract/slots.ts'
import { createQuestionDraftStore } from '../src/client/draft-store.ts'
import { apply, inject } from '../src/client/index.ts'

const SESSION_ID = 'session-question' as SessionId
const SESSION_SCOPE = Symbol('question-session-scope')
const QUESTIONS = [{ id: 'mode', question: 'Choose a mode' }] as const
const PLAN_QUESTIONS: PendingQuestion['questions'] = [{
  id: 'plan',
  question: 'Approve this plan?',
  detail: '# Plan',
  options: [{ label: 'Approve' }, { label: 'Keep planning' }],
  intent: { kind: 'plan-review' as const, approve: 'Approve' },
}]
const ANSWER = { answers: [{ id: 'mode', selected: ['Fast'] }] }

type QuestionRequest = {
  questions: PendingQuestion['questions']
  signal?: AbortSignal
}
type QuestionAnswer = typeof ANSWER
type QuestionNext = () => Promise<QuestionAnswer>
type QuestionListener = (
  this: Context,
  request: QuestionRequest,
  next: QuestionNext,
) => Promise<QuestionAnswer>

async function bench(declare = true) {
  const ctx = new Context()
  await ctx.plugin(SlotRegistry).await()
  const slots = ctx.get('slots') as SlotRegistry
  if (declare) {
    slots.register(
      { name: 'root', children: { 'conversation.composer': { kind: 'chain', scope: 'session' } } } as never,
      () => null,
    )
  }
  const locale = new LocaleRuntime(ctx)
  ctx.provide('locale', locale)
  const agent = ctx.extend({ [SESSION_SCOPE]: SESSION_ID })
  const scopeOf = vi.fn((candidate: Context) => (
    candidate as Context & { [SESSION_SCOPE]?: SessionId }
  )[SESSION_SCOPE])
  ctx.provide('sessions', { scopeOf } as never)
  const pending = new Map<PendingQuestion, () => Promise<void>>()
  const registerPendingInteraction = vi.fn((_precedence: (value: PendingQuestion) => number) => (
    value: PendingQuestion,
    delegate: () => Promise<void>,
  ) => {
    _precedence(value)
    pending.set(value, delegate)
    return () => { pending.delete(value) }
  })
  ctx.provide('uiSession', { registerPendingInteraction } as never)
  let listener: QuestionListener | undefined
  const on = vi.fn((event: string, value: QuestionListener) => {
    expect(event).toBe('user-questions/request')
    listener = value
    return () => { listener = undefined }
  })
  ctx.provide('remote', { $on: on } as never)
  const fiber = ctx.plugin({ inject: [...inject], apply })
  await fiber.await()
  const invoke = (
    owner: Context,
    request: QuestionRequest,
    next: QuestionNext,
  ): Promise<QuestionAnswer> => {
    if (listener === undefined) throw new Error('question listener was not installed')
    return listener.call(owner, request, next)
  }
  return {
    ctx,
    slots,
    locale,
    agent,
    scopeOf,
    pending: { getSnapshot: () => [...pending.keys()] },
    registerPendingInteraction,
    on,
    fiber,
    invoke,
    async releasePending() {
      const delegates = [...pending.values()]
      pending.clear()
      await Promise.allSettled(delegates.map(delegate => delegate()))
    },
  }
}

describe('apply', () => {
  it('declares the services it binds', () => {
    expect(inject).toEqual(['sessions', 'remote', 'uiSession', 'slots', 'locale'])
  })

  it('installs the Remote Event listener and delegates an unscoped request', async () => {
    const b = await bench(false)
    const next = vi.fn(async () => ANSWER)

    await expect(b.invoke(b.ctx, { questions: QUESTIONS }, next)).resolves.toBe(ANSWER)

    expect(b.on).toHaveBeenCalledOnce()
    expect(next).toHaveBeenCalledOnce()
    expect(b.slots.entries('conversation.composer')).toHaveLength(0)
    expect(b.pending.getSnapshot()).toEqual([])
  })

  it('projects a scoped request through one stable composer and returns its answer', async () => {
    const b = await bench()
    const next = vi.fn(async () => ANSWER)
    const result = b.invoke(b.agent, { questions: QUESTIONS }, next)
    await Promise.resolve()

    const entry = b.slots.entries('conversation.composer')[0]!
    expect(entry.component).toBe(QuestionComposer)
    expect(entry.inject).toBeUndefined()
    expect(entry.locale).toBe('question')
    const store = entry.store as ReturnType<typeof createQuestionDraftStore>
    expect(store.create(SESSION_ID).getSnapshot()).toEqual({
      progress: { index: 0, drafts: [] },
    })
    const pending = b.pending.getSnapshot()[0]!
    const select = entry.select as (
      owner: { pendingInteraction: PendingQuestion | undefined },
    ) => PendingQuestion | null
    expect(select({ pendingInteraction: undefined })).toBeNull()
    expect(select({ pendingInteraction: pending })).toBe(pending)
    expect(pending).toMatchObject({ kind: 'question', sessionId: SESSION_ID, questions: QUESTIONS })

    await pending.answer(ANSWER)
    await expect(result).resolves.toBe(ANSWER)
    expect(next).not.toHaveBeenCalled()
    expect(b.pending.getSnapshot()).toEqual([])
    expect(b.slots.entries('conversation.composer')).toHaveLength(1)
  })

  it('preserves ASK_CANCELLED as a rejected waterfall result', async () => {
    const b = await bench()
    const result = b.invoke(b.agent, { questions: QUESTIONS }, async () => ANSWER)
    await Promise.resolve()
    const pending = b.pending.getSnapshot()[0]!
    const rejection = expect(result).rejects.toMatchObject({
      name: 'UserQuestionError',
      code: 'ASK_CANCELLED',
      message: 'the user cancelled ask_user_question',
    })

    await pending.cancel()
    await rejection
    expect(b.pending.getSnapshot()).toEqual([])
    expect(b.slots.entries('conversation.composer')).toHaveLength(1)
  })

  it('publishes a plan-review request with its distinct interaction kind', async () => {
    const b = await bench()
    const result = b.invoke(b.agent, { questions: PLAN_QUESTIONS }, async () => ANSWER)
    await Promise.resolve()
    const pending = b.pending.getSnapshot()[0]!

    expect(pending.kind).toBe('plan-review')
    await pending.answer(ANSWER)
    await expect(result).resolves.toBe(ANSWER)
    expect(b.pending.getSnapshot()).toEqual([])
  })

  it('removes a cancelled request while preserving the stable composer', async () => {
    const b = await bench()
    const controller = new AbortController()
    const result = b.invoke(b.agent, { questions: QUESTIONS, signal: controller.signal }, async () => ANSWER)
    await Promise.resolve()
    expect(b.pending.getSnapshot()).toHaveLength(1)

    controller.abort()

    await expect(result).rejects.toMatchObject({ code: 'ASK_ABORTED' })
    expect(b.pending.getSnapshot()).toEqual([])
    expect(b.slots.entries('conversation.composer')).toHaveLength(1)
  })

  it('delegates an active request when its interaction domain unloads', async () => {
    const b = await bench()
    const next = vi.fn(async () => ANSWER)
    const result = b.invoke(b.agent, { questions: QUESTIONS }, next)
    await Promise.resolve()
    expect(b.pending.getSnapshot()).toHaveLength(1)

    await b.releasePending()

    await expect(result).resolves.toBe(ANSWER)
    expect(next).toHaveBeenCalledOnce()
    expect(b.pending.getSnapshot()).toEqual([])
  })

  it('removes the stable composer with the plugin lifetime', async () => {
    const b = await bench()
    expect(b.slots.entries('conversation.composer')).toHaveLength(1)

    await b.fiber.dispose()

    expect(b.slots.entries('conversation.composer')).toHaveLength(0)
  })
})

describe('PendingQuestion', () => {
  it('preserves an already-aborted request signal as ASK_ABORTED', async () => {
    const lifetime = new AbortController()
    lifetime.abort()
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS, lifetime.signal)

    await expect(pending.result).rejects.toMatchObject({
      name: 'UserQuestionError',
      code: 'ASK_ABORTED',
      message: 'ask_user_question was aborted before the user answered',
    })
  })

  it('rejects on later request cancellation and removes the listener after settlement', async () => {
    const lifetime = new AbortController()
    const remove = vi.spyOn(lifetime.signal, 'removeEventListener')
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS, lifetime.signal)
    const rejected = expect(pending.result).rejects.toMatchObject({ code: 'ASK_ABORTED' })

    lifetime.abort()

    await rejected
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
  })

  it('ignores a lifecycle abort after the answer already settled', async () => {
    const lifetime = new AbortController()
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS, lifetime.signal)

    await pending.answer(ANSWER)
    await expect(pending.result).resolves.toBe(ANSWER)
    pending.abort(new Error('late disposal'))
    pending.delegate()
  })

  it('rejects an unanswered request with its caller-owned lifecycle reason', async () => {
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS)
    const reason = new Error('scope released')
    const rejected = expect(pending.result).rejects.toBe(reason)

    pending.abort(reason)

    await rejected
  })

  it('wraps a non-Error answer settlement failure with its cause', async () => {
    const failure = 'resolve failed'
    const completion = Promise.withResolvers<QuestionAnswer>()
    const withResolvers = vi.spyOn(Promise, 'withResolvers').mockImplementationOnce(() => ({
      promise: completion.promise,
      resolve: () => { throw failure },
      reject: completion.reject,
    }))
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS)
    withResolvers.mockRestore()

    const settlement = await pending.answer(ANSWER).catch((error: unknown) => error)

    expect(settlement).toBeInstanceOf(Error)
    expect(settlement).toMatchObject({
      message: 'pending question settlement failed',
      cause: failure,
    })
    completion.resolve(ANSWER)
    await expect(pending.result).resolves.toBe(ANSWER)
  })

  it('wraps a non-Error cancellation settlement failure with its cause', async () => {
    const failure = 'reject failed'
    const completion = Promise.withResolvers<QuestionAnswer>()
    const withResolvers = vi.spyOn(Promise, 'withResolvers').mockImplementationOnce(<T>() => ({
      promise: completion.promise,
      resolve: completion.resolve as (value: T | PromiseLike<T>) => void,
      reject: () => { throw failure },
    }))
    const pending = new PendingQuestion(SESSION_ID, QUESTIONS)
    withResolvers.mockRestore()

    const settlement = await pending.cancel().catch((error: unknown) => error)

    expect(settlement).toBeInstanceOf(Error)
    expect(settlement).toMatchObject({
      message: 'pending question cancellation failed',
      cause: failure,
    })
    completion.resolve(ANSWER)
    await expect(pending.result).resolves.toBe(ANSWER)
  })
})
