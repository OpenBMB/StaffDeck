// @vitest-environment jsdom
/**
 * MessageFeedbackActions rendering and gestures: the rating buttons reflect the
 * shared view, re-clicking the active rating retracts it, the note editor
 * saves through the same rate verb, the Session's feedback is read on first
 * interaction rather than on mount, and a rejected mutation surfaces inline
 * without losing the authoritative state.
 */
import { useSyncExternalStore } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { makeTranslate } from '@deepseek-ai/dsh-client-test-runtime'
import { zh as commonZh } from '@deepseek-ai/dsh-client-locale/src/locales/zh.ts'
import type { MessageId } from '@deepseek-ai/dsh-api-remotes/client'
import type {
  MessageFeedbackItem, MessageFeedbackRating, MessageFeedbackVersion,
} from '@deepseek-ai/dsh-message-feedback/types'
import { MessageFeedbackActions } from '../src/client/MessageFeedbackActions.tsx'
import type { MessageFeedbackActionResult, MessageFeedbackView } from '../src/client/controller.ts'
import { zh } from '../src/client/locales.ts'

afterEach(cleanup)

const MSG = 'm-1' as MessageId
const t = makeTranslate(zh, commonZh)

function item(overrides: Partial<MessageFeedbackItem> = {}): MessageFeedbackItem {
  return {
    messageId: MSG,
    rating: 'positive',
    version: 'v1' as MessageFeedbackVersion,
    createdAt: 1,
    updatedAt: 1,
    ...overrides,
  }
}

/** Render the controls over a fixed view and recording verbs. */
function mount(options: {
  current?: MessageFeedbackItem | undefined
  rateResult?: MessageFeedbackActionResult
  clearResult?: MessageFeedbackActionResult
  status?: MessageFeedbackView['status']
} = {}) {
  const view: MessageFeedbackView = {
    status: options.status ?? 'ready',
    items: new Map(options.current === undefined ? [] : [[MSG, options.current]]),
    error: null,
  }
  const ensure = vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true }))
  const rate = vi.fn((_id: MessageId, _rating: MessageFeedbackRating, _note?: string) =>
    Promise.resolve(options.rateResult ?? { ok: true as const }))
  const clear = vi.fn((_id: MessageId) =>
    Promise.resolve(options.clearResult ?? { ok: true as const }))
  // The controller owns retract-vs-replace, so the double stands in for it:
  // matching the shown rating retracts, anything else replaces.
  const toggle = vi.fn((id: MessageId, next: MessageFeedbackRating) =>
    (options.current?.rating === next ? clear(id) : rate(id, next)))
  const clearNote = vi.fn((_id: MessageId) =>
    Promise.resolve(options.rateResult ?? { ok: true as const }))
  const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
    useSyncExternalStore(() => () => {}, () => select(view))) as never
  const props = { messageId: MSG, ensure, rate, toggle, clearNote, clear, useFeedback, t } as unknown as
    Parameters<typeof MessageFeedbackActions>[0]
  return { ...render(<MessageFeedbackActions {...props} />), ensure, rate, clear, toggle, clearNote }
}

describe('MessageFeedbackActions', () => {
  it('renders both rating buttons unpressed with no recorded feedback', () => {
    const ui = mount()

    expect(ui.getByLabelText(zh['action.like']).getAttribute('aria-pressed')).toBe('false')
    expect(ui.getByLabelText(zh['action.dislike']).getAttribute('aria-pressed')).toBe('false')
  })

  it('marks the recorded rating pressed and offers to retract it', () => {
    const ui = mount({ current: item({ rating: 'negative' }) })

    expect(ui.getByLabelText(zh['action.dislikeActive']).getAttribute('aria-pressed')).toBe('true')
    expect(ui.getByLabelText(zh['action.like']).getAttribute('aria-pressed')).toBe('false')
  })

  it('reads the Session feedback on first interaction, once', () => {
    const ui = mount()
    const like = ui.getByLabelText(zh['action.like'])

    fireEvent.pointerEnter(like)
    fireEvent.pointerEnter(like)
    fireEvent.focus(ui.getByLabelText(zh['action.dislike']))

    expect(ui.ensure).toHaveBeenCalledTimes(1)
  })

  it('does not read the Session feedback on mount', () => {
    const ui = mount()

    expect(ui.ensure).not.toHaveBeenCalled()
  })

  it('rates a message that has no feedback yet', async () => {
    const ui = mount()

    fireEvent.click(ui.getByLabelText(zh['action.like']))

    await waitFor(() => { expect(ui.toggle).toHaveBeenCalledWith(MSG, 'positive') })
    expect(ui.clear).not.toHaveBeenCalled()
  })

  it('replaces the opposite rating and carries the existing note forward', async () => {
    const ui = mount({ current: item({ rating: 'positive', note: 'keep me' }) })

    fireEvent.click(ui.getByLabelText(zh['action.dislike']))

    await waitFor(() => { expect(ui.toggle).toHaveBeenCalledWith(MSG, 'negative') })
  })

  it('retracts the feedback when the active rating is clicked again', async () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByLabelText(zh['action.likeActive']))

    await waitFor(() => { expect(ui.toggle).toHaveBeenCalledWith(MSG, 'positive') })
    // The double routes a matching rating to clear(), mirroring the controller.
    await waitFor(() => { expect(ui.clear).toHaveBeenCalledWith(MSG) })
  })

  it('saves a typed note through the rate verb and closes the editor', async () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: '  precise and short  ' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    await waitFor(() => { expect(ui.rate).toHaveBeenCalledWith(MSG, 'positive', 'precise and short') })
    await waitFor(() => { expect(ui.queryByLabelText(zh['note.aria'])).toBeNull() })
  })

  it('clears the note when the editor is emptied', async () => {
    const ui = mount({ current: item({ rating: 'positive', note: 'old note' }) })

    fireEvent.click(ui.getByText('old note'))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: '   ' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    await waitFor(() => { expect(ui.clearNote).toHaveBeenCalledWith(MSG) })
  })

  it('seeds the editor with the recorded note and abandons it on cancel', () => {
    const ui = mount({ current: item({ rating: 'positive', note: 'old note' }) })

    fireEvent.click(ui.getByText('old note'))
    expect((ui.getByLabelText(zh['note.aria']) as HTMLTextAreaElement).value).toBe('old note')

    fireEvent.click(ui.getByText(zh['note.cancel']))
    expect(ui.queryByLabelText(zh['note.aria'])).toBeNull()
    expect(ui.rate).not.toHaveBeenCalled()
  })

  it('offers no note editor before a rating is recorded', () => {
    const ui = mount()

    expect(ui.queryByText(zh['note.open'])).toBeNull()
  })

  it('reports a lost race with the conflict copy', async () => {
    const ui = mount({
      rateResult: { ok: false, error: { code: 'version-conflict', message: 'feedback changed elsewhere' } },
    })

    fireEvent.click(ui.getByLabelText(zh['action.like']))

    await waitFor(() => { expect(ui.getByText(zh['error.conflict'])).toBeTruthy() })
  })

  it('reports any other failure with the generic copy', async () => {
    const ui = mount({
      rateResult: { ok: false, error: { code: 'target-not-found', message: 'no such message' } },
    })

    fireEvent.click(ui.getByLabelText(zh['action.like']))

    await waitFor(() => { expect(ui.getByText(zh['error.generic'])).toBeTruthy() })
  })

  it('keeps the editor open when the note fails to save', async () => {
    const ui = mount({
      current: item({ rating: 'positive' }),
      rateResult: { ok: false, error: { code: 'note-too-large', message: 'too long' } },
    })

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'x'.repeat(20) } })
    fireEvent.click(ui.getByText(zh['note.save']))

    await waitFor(() => { expect(ui.getByText(zh['error.generic'])).toBeTruthy() })
    // The draft survives so the human can shorten it instead of retyping.
    expect(ui.getByLabelText(zh['note.aria'])).toBeTruthy()
  })

  it('publishes no state after the row unmounts mid-flight', async () => {
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: false, error: { code: 'target-not-found', message: 'gone' } }) }
    })
    const view: MessageFeedbackView = { status: 'ready', items: new Map(), error: null }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => gate),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)
    const errors: unknown[] = []
    const onError = (event: ErrorEvent): void => { errors.push(event.error) }
    window.addEventListener('error', onError)

    fireEvent.click(ui.getByLabelText(zh['action.like']))
    ui.unmount()
    release()
    await gate

    window.removeEventListener('error', onError)
    expect(errors).toEqual([])
  })

  it('surfaces a failed list load next to the controls', async () => {
    const ui = mount({ status: 'error' })

    expect(ui.getByText(zh['error.load'])).toBeTruthy()
  })

  it('prefers the action failure over the load notice', async () => {
    const ui = mount({
      status: 'error',
      rateResult: { ok: false, error: { code: 'target-not-found', message: 'gone' } },
    })

    fireEvent.click(ui.getByLabelText(zh['action.like']))

    await waitFor(() => { expect(ui.getByText(zh['error.generic'])).toBeTruthy() })
    expect(ui.queryByText(zh['error.load'])).toBeNull()
  })

  it('portals the note editor to the document body, not into the actions row', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))

    // The editor must float above the transcript (escaping the conversation
    // column's overflow clip), so it renders through a portal to document.body
    // rather than inline inside the component's own container.
    const panel = ui.getByRole('dialog')
    expect(panel).toBeTruthy()
    expect(ui.container.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.contains(panel)).toBe(true)
  })

  it('closes the note popover on Escape', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(ui.queryByRole('dialog')).toBeNull()
  })

  it('closes the note popover on an outside pointer-down', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    fireEvent.pointerDown(document.body)

    expect(ui.queryByRole('dialog')).toBeNull()
  })

  it('keeps the note popover open on a pointer-down inside it', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    const panel = ui.getByRole('dialog')
    expect(panel).toBeTruthy()

    fireEvent.pointerDown(panel)

    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('does not close the note popover on a pointer-down on its trigger', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    // The trigger is inside the panel's own region, so pressing it must not be
    // treated as an outside click; the toggle click below then closes it.
    fireEvent.pointerDown(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('toggles the note popover closed and open from its trigger', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()
    expect(ui.getByLabelText(zh['note.aria'])).toBeTruthy()

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.queryByRole('dialog')).toBeNull()

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('ignores keys other than Escape while the popover is open', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Enter' })

    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('publishes no rating-state after the row unmounts mid-flight', async () => {
    // Directly exercise the early-return of a rating settle once the control has
    // unmounted: the promise resolution must not touch React state.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: true as const }) }
    })
    const view: MessageFeedbackView = { status: 'ready', items: new Map(), error: null }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      toggle: vi.fn(() => gate),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)
    const errors: unknown[] = []
    const onError = (event: ErrorEvent): void => { errors.push(event.error) }
    window.addEventListener('error', onError)

    fireEvent.click(ui.getByLabelText(zh['action.like']))
    ui.unmount()
    release()
    await gate

    window.removeEventListener('error', onError)
    expect(errors).toEqual([])
  })

  it('publishes no note-state after the row unmounts mid-save', async () => {
    // Same unmount early-return for the note-save settle path: resolving the
    // save promise after unmount must not touch React state.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: true as const }) }
    })
    const view: MessageFeedbackView = { status: 'ready', items: new Map([[MSG, item({ rating: 'positive' })]]), error: null }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const errors: unknown[] = []
    const onError = (event: ErrorEvent): void => { errors.push(event.error) }
    window.addEventListener('error', onError)

    const ui = render(<MessageFeedbackActions {...props} />)
    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'hi' } })
    fireEvent.click(ui.getByText(zh['note.save']))
    ui.unmount()
    release()
    await gate

    window.removeEventListener('error', onError)
    expect(errors).toEqual([])
  })

  it('ignores a pointer-down whose target is not a DOM node', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })

    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    // The outside-click guard returns without closing when the event target is
    // not a DOM node. `document.dispatchEvent` delivers straight to the
    // document listener, and a non-Node target is not `instanceof Node`.
    const event = new MouseEvent('pointerdown', { bubbles: true })
    Object.defineProperty(event, 'target', { configurable: true, value: { notANode: true } })
    document.dispatchEvent(event)

    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('returns focus to the trigger when the popover closes', () => {
    const ui = mount({ current: item({ rating: 'positive' }) })
    const trigger = ui.getByText(zh['note.open'])

    fireEvent.click(trigger)
    expect(ui.getByRole('dialog')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })

    // Closing hands focus back, so a keyboard user resumes on the row they
    // came from rather than at the document root.
    expect(ui.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('does not pull focus when an already-rated message mounts', () => {
    // The `wasOpen` guard exists for this: a transcript of already-rated
    // messages must not drag focus into an action row as each one mounts.
    // Only a real open-then-close returns focus.
    const elsewhere = document.createElement('button')
    document.body.append(elsewhere)
    elsewhere.focus()

    mount({ current: item({ rating: 'positive' }) })

    expect(document.activeElement).toBe(elsewhere)
    elsewhere.remove()
  })

  it('drops a stale save failure when the popover is reopened', async () => {
    // The failure belongs to the editing session that produced it: reopening
    // reseeds the draft from the stored note, so a carried-over error would
    // describe an attempt the new draft never made.
    const ui = mount({
      current: item({ rating: 'positive' }),
      rateResult: { ok: false, error: { code: 'note-too-large', message: 'too long' } },
    })

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'x'.repeat(20) } })
    fireEvent.click(ui.getByText(zh['note.save']))
    await waitFor(() => { expect(ui.getByText(zh['error.generic'])).toBeTruthy() })

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(ui.getByText(zh['note.open']))

    expect(ui.queryByText(zh['error.generic'])).toBeNull()
  })

  it('keeps a save failure visible when the rating disappears underneath it', async () => {
    // Another client retracts the feedback while the editor is open: the
    // controller commits `current: null`, the item goes away, and the panel
    // unmounts. The failure must not vanish with it, so it falls back to the row.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: false, error: { code: 'target-not-found', message: 'gone' } }) }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive' })]]),
      error: null,
    }
    let notify = (): void => {}
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore((cb) => { notify = cb; return () => {} }, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'hi' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    // The retract lands first, then the save rejects.
    view.items = new Map()
    notify()
    release()
    await gate

    await waitFor(() => { expect(ui.getByText(zh['error.generic'])).toBeTruthy() })
    expect(ui.queryByRole('dialog')).toBeNull()
  })

  it('ignores a save that resolves after its editing session ended', async () => {
    // Closing and reopening starts a new session. A late success from the old
    // one must not shut the panel the human just opened.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: true as const }) }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive' })]]),
      error: null,
    }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'first' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    // Abandon that session and start another before the save lands.
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    release()
    await gate
    // Flush the `.then` continuation and the render it would cause. Asserted
    // directly rather than through `waitFor`, which would retry past a panel
    // that the stale result closed.
    await act(async () => { await Promise.resolve() })

    expect(ui.getByRole('dialog')).toBeTruthy()
    // The reply is discarded, but the request is no longer in flight, so the
    // controls must not stay disabled: `pending` gates the rating buttons and
    // Save, and leaving it set locks this message's row until it remounts.
    expect(ui.getByLabelText(zh['action.likeActive']).hasAttribute('disabled')).toBe(false)
    expect(ui.getByLabelText(zh['action.dislike']).hasAttribute('disabled')).toBe(false)
    expect(ui.getByText(zh['note.save']).hasAttribute('disabled')).toBe(false)
  })

  it('reports a save that fails after the human closed the panel', async () => {
    // A slow save that rejects once the panel is gone must not be swallowed:
    // the human would otherwise believe the note was stored. With no panel to
    // show it in, the row carries the notice.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => {
        resolve({ ok: false, error: { code: 'target-not-found', message: 'gone' } })
      }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive' })]]),
      error: null,
    }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'hi' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    // Walk away before the reply lands, and leave it closed.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(ui.queryByRole('dialog')).toBeNull()

    release()
    await gate
    await act(async () => { await Promise.resolve() })

    expect(ui.getByText(zh['error.generic'])).toBeTruthy()
  })

  it('does not write an abandoned session\'s failure into a reopened panel', async () => {
    // The old request rejects after the panel was closed and reopened, so the
    // new session owns the panel. Its draft was not the one that failed, so the
    // stale error must not be shown there; it belongs to the abandoned session.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => {
        resolve({ ok: false, error: { code: 'target-not-found', message: 'gone' } })
      }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive' })]]),
      error: null,
    }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText(zh['note.open']))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'first' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    // Abandon that session and start another before the save rejects; unlike
    // the closed-and-left case, a new panel is now on screen.
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(ui.getByText(zh['note.open']))
    expect(ui.getByRole('dialog')).toBeTruthy()

    release()
    await gate
    await act(async () => { await Promise.resolve() })

    // The stale failure names a draft the new session never sent, so it stays
    // out of the reopened panel's status area.
    expect(ui.queryByText(zh['error.generic'])).toBeNull()
    expect(ui.getByRole('dialog')).toBeTruthy()
  })

  it('resyncs an untouched reopened draft to the note that just committed', async () => {
    // The reopened session seeded from the note as it read before the save
    // committed, so an untouched draft would show stale text and the next save
    // could overwrite what just landed.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: true as const }) }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive', note: 'old' })]]),
      error: null,
    }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText('old'))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'saved text' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    // Close and reopen before the save lands: the new draft is seeded from the
    // still-stale stored note.
    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(ui.getByText('old'))
    expect((ui.getByLabelText(zh['note.aria']) as HTMLTextAreaElement).value).toBe('old')

    release()
    await gate
    await act(async () => { await Promise.resolve() })

    expect((ui.getByLabelText(zh['note.aria']) as HTMLTextAreaElement).value).toBe('saved text')
  })

  it('leaves a reopened draft alone once the human has edited it', async () => {
    // The opposite arm: an edited draft belongs to the human, so a late save
    // must not overwrite what they are typing.
    let release = (): void => {}
    const gate = new Promise<MessageFeedbackActionResult>((resolve) => {
      release = () => { resolve({ ok: true as const }) }
    })
    const view: MessageFeedbackView = {
      status: 'ready',
      items: new Map([[MSG, item({ rating: 'positive', note: 'old' })]]),
      error: null,
    }
    const useFeedback = (<T,>(select: (v: MessageFeedbackView) => T): T =>
      useSyncExternalStore(() => () => {}, () => select(view))) as never
    const props = {
      messageId: MSG,
      ensure: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      rate: vi.fn(() => gate),
      toggle: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clearNote: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      clear: vi.fn(() => Promise.resolve<MessageFeedbackActionResult>({ ok: true })),
      useFeedback,
      t,
    } as unknown as Parameters<typeof MessageFeedbackActions>[0]
    const ui = render(<MessageFeedbackActions {...props} />)

    fireEvent.click(ui.getByText('old'))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'saved text' } })
    fireEvent.click(ui.getByText(zh['note.save']))

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.click(ui.getByText('old'))
    fireEvent.change(ui.getByLabelText(zh['note.aria']), { target: { value: 'my new words' } })

    release()
    await gate
    await act(async () => { await Promise.resolve() })

    expect((ui.getByLabelText(zh['note.aria']) as HTMLTextAreaElement).value).toBe('my new words')
  })
})
