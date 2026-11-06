/** Question composer props and one pending Remote waterfall response. */
import type { PropsLocale, PropsRuntime, PropsStore } from '@deepseek-ai/dsh-client-ui-slots'
// The client module declares the conversation.composer SlotMap entry required by PropsRuntime.
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import type {
  AskUserQuestionAnswer, AskUserQuestionItem,
} from '@deepseek-ai/dsh-user-questions'
import type { createQuestionDraftStore } from '../draft-store.ts'

declare module '@deepseek-ai/dsh-client-ui-session/client' {
  interface SessionPendingInteractionMap {
    /** Pending question or plan-review request. */
    question: PendingQuestion
  }
}

/** One structured answer batch covering every question of the request. */
export type QuestionAnswer = AskUserQuestionAnswer

/** One question of the request. */
type QuestionItem = AskUserQuestionItem

/** One option the asker offered on a question. */
type QuestionOption = NonNullable<QuestionItem['options']>[number]

/* jscpd:ignore-start -- Question and Approval intentionally own independent pending-settlement lifecycles. */
function settlePendingComposer(settle: () => void, failureMessage: string): Promise<void> {
  try {
    settle()
    return Promise.resolve()
  } catch (error) {
    return Promise.reject(error instanceof Error
      ? error
      : new Error(failureMessage, { cause: error }))
  }
}
/* jscpd:ignore-end */

/**
 * A request narrowed to the `plan-review` presentation intent: everything the
 * decision card renders and answers with, so the panel never re-reads the
 * request fields. `approve` and `decline` are the asker's own options — an
 * answer must carry one of those labels verbatim — and `plan` is the markdown
 * body under review.
 */
export interface PlanReview {
  /** The reviewed question's id, echoed in the answer. */
  id: string
  /** The question text, kept as the card's accessible name. */
  question: string
  /** The plan markdown under review. */
  plan: string
  /** The option that approves the plan. */
  approve: QuestionOption
  /** The option that declines it; absent when the asker offered no other option. */
  decline?: QuestionOption
}

/**
 * Narrow a request to a renderable plan review, or return undefined to leave it
 * to the generic question flow.
 *
 * The card is one decision over one plan, and it claims a request only when it
 * can send every answer that request allows — an intent changes the layout,
 * never which answers are reachable. So the batch must be a single question
 * that declares the intent, carries the plan as its detail, offers the approve
 * label the intent names, and is a binary single choice: at most one option
 * besides approve, and not multi-select. A third option or a multi-select batch
 * has answers two buttons cannot express, so the generic flow keeps it — as it
 * keeps any request whose intent the asker's own service would have rejected,
 * because the client sits downstream of a wire boundary and every request must
 * stay answerable.
 *
 * @param questions - the request's whole question batch.
 * @returns The narrowed review, or undefined when the generic flow owns it.
 */
export function planReviewOf(questions: readonly QuestionItem[]): PlanReview | undefined {
  if (questions.length !== 1) return undefined
  // Length-checked above; the index read is the narrowing tax, not a guess.
  const question = questions[0] as QuestionItem
  const intent = question.intent
  if (intent?.kind !== 'plan-review' || question.detail === undefined) return undefined
  if (question.multiSelect === true) return undefined
  const options = question.options ?? []
  if (options.length > 2) return undefined
  const approve = options.find(option => option.label === intent.approve)
  if (approve === undefined) return undefined
  const decline = options.find(option => option.label !== intent.approve)
  return {
    id: question.id,
    question: question.question,
    plan: question.detail,
    approve,
    ...(decline === undefined ? {} : { decline }),
  }
}

let nextQuestionKey = 0

/** Create a wire-preserved user-question rejection. */
function questionError(message: string, code: 'ASK_ABORTED' | 'ASK_CANCELLED'): Error {
  const error = new Error(message) as Error & { code: string }
  error.name = 'UserQuestionError'
  error.code = code
  return error
}

/** One answerable Client presentation of a pending Host waterfall. */
export class PendingQuestion {
  /** Presentation discriminator used by Session pending-interaction consumers. */
  readonly kind: 'question' | 'plan-review'
  /** Opaque render identity and request key for the Session-scoped draft store. */
  readonly key: string
  /** The request's question list. */
  readonly questions: readonly AskUserQuestionItem[]
  /** Result returned by the Remote Event listener to the Host waterfall. */
  readonly result: Promise<QuestionAnswer>

  readonly #resolve: (answer: QuestionAnswer) => void
  readonly #reject: (reason: unknown) => void
  readonly #signal: AbortSignal | undefined
  readonly #onAbort: (() => void) | undefined
  readonly #delegated = Symbol('pending question delegated')
  #settled = false

  /**
   * @param sessionId - Agent/Session identity owning the scoped request.
   * @param questions - complete question batch.
   * @param signal - Host request and delivery lifetime.
   */
  constructor(
    readonly sessionId: SessionId,
    questions: readonly AskUserQuestionItem[],
    signal?: AbortSignal,
  ) {
    nextQuestionKey += 1
    this.key = `question:${String(nextQuestionKey)}`
    this.questions = questions
    this.kind = planReviewOf(questions) === undefined ? 'question' : 'plan-review'
    const completion = Promise.withResolvers<QuestionAnswer>()
    this.result = completion.promise
    this.#resolve = completion.resolve
    this.#reject = completion.reject
    this.#signal = signal
    if (signal === undefined) {
      this.#onAbort = undefined
      return
    }
    const onAbort = (): void => {
      this.abort(questionError('ask_user_question was aborted before the user answered', 'ASK_ABORTED'))
    }
    this.#onAbort = onAbort
    signal.addEventListener('abort', onAbort, { once: true })
    if (signal.aborted) onAbort()
  }

  /**
   * Resolve the Host waterfall with the whole answer batch.
   * @param answer - complete structured answer batch.
   */
  answer(answer: QuestionAnswer): Promise<void> {
    return settlePendingComposer(() => {
      this.finish(() => { this.#resolve(answer) })
    }, 'pending question settlement failed')
  }

  /** Delegate an unanswered request to the next waterfall listener. */
  delegate(): void {
    if (this.#settled) return
    this.finish(() => { this.#reject(this.#delegated) })
  }

  /**
   * Test whether a rejection requests waterfall delegation.
   * @param reason - rejection received from {@link PendingQuestion.result}.
   * @returns whether {@link PendingQuestion.delegate} produced it.
   */
  isDelegation(reason: unknown): boolean {
    return reason === this.#delegated
  }

  /** Reject the Host waterfall because the user closed the question. */
  cancel(): Promise<void> {
    return settlePendingComposer(() => {
      this.finish(() => {
        this.#reject(questionError('the user cancelled ask_user_question', 'ASK_CANCELLED'))
      })
    }, 'pending question cancellation failed')
  }

  /**
   * End an unanswered presentation when its transport, scope, or plugin lifetime ends.
   * @param reason - rejection exposed to the waiting Remote Event listener.
   */
  abort(reason: unknown): void {
    if (this.#settled) return
    this.finish(() => { this.#reject(reason) })
  }

  private finish(settle: () => void): void {
    if (this.#settled) throw new Error(`pending question ${this.key} is already settled`)
    this.#settled = true
    if (this.#signal !== undefined && this.#onAbort !== undefined) {
      this.#signal.removeEventListener('abort', this.#onAbort)
    }
    settle()
  }
}

/** Pending value returned by the composer-chain selector. */
export type QuestionWait = PendingQuestion

/**
 * Full component props: the framework runtime share (chain currency +
 * session/global standard kit) plus the chain `matched` share — the entry's
 * selector result, already narrowed to the question carrier — plus the
 * standard locale seat; the carrier plus the domain face above carry the
 * whole behavior surface.
 */
export type QuestionComposerProps =
  PropsRuntime<'conversation.composer'>
  & PropsStore<ReturnType<typeof createQuestionDraftStore>>
  & { matched: QuestionWait }
  & PropsLocale<'question'>
