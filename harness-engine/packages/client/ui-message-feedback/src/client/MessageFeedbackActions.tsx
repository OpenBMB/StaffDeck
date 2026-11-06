/**
 * Per-message feedback controls: a Like/Dislike pair plus an optional note.
 * The buttons render inside the assistant message's IconActions row, so they
 * reuse that row's chrome and sit between copy and branch. The note editor is
 * a popover (portaled to `document.body`) anchored to the note trigger, not an
 * inline expansion: a 260px textarea plus buttons cannot fit the row at any
 * viewport, and an inline element pushed the branch action and clock out of the
 * conversation column. Portaling out of the column also escapes its `overflow`
 * clip, so the panel cannot be cropped or detached from the message it annotates.
 * @module @deepseek-ai/dsh-client-ui-message-feedback/client/MessageFeedbackActions
 */

import {
  useCallback, useEffect, useRef, useState,
  type CSSProperties,
} from 'react'
import { createPortal } from 'react-dom'
import {
  IconDislikeOutline16, IconLikeOutline16, Tooltip, useAnchoredPosition,
} from '@deepseek-ai/dsh-client-ui-primitives'
import type { MessageFeedbackRating } from '@deepseek-ai/dsh-message-feedback/types'
import type { MessageFeedbackActionProps } from './slots.ts'
import css from './MessageFeedbackActions.module.css'

/** Safe distance kept between the panel and the viewport edges (the Menu portal margin). */
const PANEL_MARGIN = 12

/** Distance between the trigger's bottom edge and the panel's top. */
const PANEL_GAP = 4

/**
 * Unplaced portal panel: hidden but laid out so `offsetWidth` is real for the
 * clamp. The explicit insets match `Menu`'s measure style — a `position: fixed`
 * element with auto insets otherwise sits at its static position, a different
 * origin than the one the first placement measures from.
 */
const MEASURE_STYLE: CSSProperties = { visibility: 'hidden', left: 0, top: 0 }

/**
 * One message's feedback controls.
 * @param props - the owner's message identity, the injected verbs, and the
 * shared feedback hook.
 * @returns the rating buttons and the note trigger, with the note editor
 * portal-open beneath the trigger while it is open.
 */
export function MessageFeedbackActions({ messageId, ensure, rate, toggle, clearNote, useFeedback, t }: MessageFeedbackActionProps) {
  const item = useFeedback(view => view.items.get(messageId))
  const loadFailed = useFeedback(view => view.status === 'error')
  const rating = item?.rating
  const [noteOpen, setNoteOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  // A rating or load failure surfaces beside the rating buttons, always legible
  // whether or not the note popover is open.
  const [rowFailure, setRowFailure] = useState<string | null>(null)
  // A note save failure surfaces inside the note popover, where the human is
  // looking; it stays open so the draft survives to be corrected.
  const [noteFailure, setNoteFailure] = useState<string | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  // The controls mount for every settled message in the transcript, so the
  // Session's feedback is read once on first hover/focus rather than on mount.
  const seeded = useRef(false)
  const seed = useCallback(() => {
    if (seeded.current) return
    seeded.current = true
    void ensure()
  }, [ensure])

  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  /** Bumped whenever an editing session ends, so a late save can tell it is stale. */
  const noteGeneration = useRef(0)

  /** Current panel open-state, readable from a stale closure via a ref. */
  const noteOpenRef = useRef(false)
  useEffect(() => { noteOpenRef.current = noteOpen }, [noteOpen])

  const errorCopy = useCallback((result: { ok: boolean; error?: { code: string } }) => {
    return result.error?.code === 'version-conflict' ? t('error.conflict') : t('error.generic')
  }, [t])

  const settleRating = useCallback((result: { ok: boolean; error?: { code: string } }) => {
    if (!alive.current) return
    setPending(false)
    setRowFailure(result.ok ? null : errorCopy(result))
  }, [errorCopy])

  const closeNote = useCallback(() => {
    // Ends the editing session, so any save still in flight becomes stale.
    noteGeneration.current += 1
    setNoteOpen(false)
  }, [])

  const onRate = useCallback((next: MessageFeedbackRating) => {
    setPending(true)
    setRowFailure(null)
    // The controller decides retract-vs-replace from the committed item, so a
    // click that lands before the first list read still toggles the stored
    // value instead of this render's empty view.
    closeNote()
    void toggle(messageId, next).then(settleRating)
  }, [closeNote, messageId, settleRating, toggle])

  // The rating is a parameter because only the note editor's render site can
  // prove one is recorded; that removes an unreachable undefined guard here.
  const onSaveNote = useCallback((current: MessageFeedbackRating) => {
    const trimmed = draft.trim()
    setPending(true)
    setNoteFailure(null)
    // A save belongs to the editing session that started it. Closing and
    // reopening the panel begins a new one, and a late reply from the old
    // session must not act on it: a stale success would shut the panel the
    // human just opened, and a stale failure would describe a draft this
    // session never sent.
    const generation = noteGeneration.current
    // What a session reopened before this save commits would be seeded with.
    const staleSeed = item?.note ?? ''
    // An emptied editor removes the note explicitly; `rate` alone preserves a
    // stored note, so it cannot express deletion.
    const settled = trimmed.length === 0
      ? clearNote(messageId)
      : rate(messageId, current, trimmed)
    void settled.then((result) => {
      if (!alive.current) return
      // `pending` tracks the request in flight, not the editing session, so it
      // is released either way; all three of like, dislike and Save read
      // `disabled={pending}`, and holding it would lock the row until remount.
      // Releasing it unconditionally is safe because those three are the only
      // mutation entries and each is gated by it, so at most one request is ever
      // in flight. A future entry that bypasses the gate would have to bind
      // `pending` to the generation instead of clearing it here.
      setPending(false)
      if (result.ok) {
        // Only the session that is still open may act on a success: closing it
        // already discarded the draft, and reopening seeded a new one.
        if (generation === noteGeneration.current) {
          setNoteFailure(null)
          setNoteOpen(false)
          return
        }
        // A newer session is open, seeded from the note as it read before this
        // save committed. Resync it so the editor shows what is stored and the
        // next save cannot overwrite the text that just landed. An edited draft
        // is the human's, so it is left alone.
        setDraft(draftNow => (draftNow === staleSeed ? trimmed : draftNow))
        return
      }
      // A failure from the session still on screen belongs in its panel. One
      // from an abandoned session is reported only when no new session has
      // taken over: the row then carries it, so a save that failed after the
      // human walked away is not silently dropped. Writing it into a reopened
      // panel instead would label the new draft with the old attempt's error.
      // `noteOpenRef` — not the `noteOpen` this closure was created from — is
      // read here, because a close+reopen between the save and resolution
      // leaves this closure with the panel state from when the save started.
      if (generation === noteGeneration.current || !noteOpenRef.current) {
        setNoteFailure(errorCopy(result))
      }
    })
  }, [clearNote, draft, errorCopy, item?.note, messageId, noteOpenRef, rate])

  // The trigger toggles: while closed it opens the popover (seeding the draft
  // with the recorded note), while open it closes it. Toggling closed via the
  // trigger also fires the outside/within logic correctly because the trigger
  // is inside the panel's "inside" region.
  const toggleNote = useCallback(() => {
    if (noteOpen) {
      closeNote()
      return
    }
    setDraft(item?.note ?? '')
    // A note-save failure belongs to the editing session that produced it. The
    // panel stays open on failure so the draft can be corrected, but once it is
    // closed and reopened the draft is reseeded from the stored note, so a
    // carried-over error would describe an attempt the new draft never made.
    // A failure that arrives after the panel closed is reported in the row, and
    // clearing it here is what retires that notice when a new session starts.
    setNoteFailure(null)
    setNoteOpen(true)
  }, [noteOpen, closeNote, item?.note])

  // Place the portaled panel from the trigger rect before paint and keep it
  // with the trigger on scroll/resize, the same anchoring `Menu` uses for its
  // portal mode.
  const pos = useAnchoredPosition({
    open: noteOpen,
    anchorRef: triggerRef,
    panelRef,
    gap: PANEL_GAP,
    margin: PANEL_MARGIN,
  })

  // Focus the input and close on Escape or outside pointer-down while open.
  useEffect(() => {
    if (!noteOpen) return
    inputRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      if (!(e.target instanceof Node)) return
      if (triggerRef.current?.contains(e.target) === true) return
      if (panelRef.current?.contains(e.target) === true) return
      closeNote()
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeNote()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [noteOpen, closeNote])

  // Return focus to the trigger only when the panel actually closes, not on the
  // initial mount (a freshly rendered message with a recorded rating must not
  // pull focus into its action row).
  const wasOpen = useRef(false)
  useEffect(() => {
    if (noteOpen) { wasOpen.current = true; return }
    if (wasOpen.current) triggerRef.current?.focus()
    wasOpen.current = false
  }, [noteOpen])

  const likeLabel = rating === 'positive' ? t('action.likeActive') : t('action.like')
  const dislikeLabel = rating === 'negative' ? t('action.dislikeActive') : t('action.dislike')

  return (
    <>
      <Tooltip label={likeLabel} side="bottom">
        <button
          type="button"
          className={css.action}
          aria-label={likeLabel}
          aria-pressed={rating === 'positive'}
          data-active={rating === 'positive' || undefined}
          disabled={pending}
          onFocus={seed}
          onPointerEnter={seed}
          onClick={() => { onRate('positive') }}
        >
          <IconLikeOutline16 />
        </button>
      </Tooltip>
      <Tooltip label={dislikeLabel} side="bottom">
        <button
          type="button"
          className={css.action}
          aria-label={dislikeLabel}
          aria-pressed={rating === 'negative'}
          data-active={rating === 'negative' || undefined}
          disabled={pending}
          onFocus={seed}
          onPointerEnter={seed}
          onClick={() => { onRate('negative') }}
        >
          <IconDislikeOutline16 />
        </button>
      </Tooltip>
      {rating !== undefined && (
        <button
          ref={triggerRef}
          type="button"
          className={css.noteOpen}
          aria-haspopup="dialog"
          aria-expanded={noteOpen}
          onClick={toggleNote}
        >
          {item?.note === undefined ? t('note.open') : item.note}
        </button>
      )}
      {rowFailure === null && loadFailed && (
        <span className={css.failure} role="status">{t('error.load')}</span>
      )}
      {rowFailure !== null && <span className={css.failure} role="status">{rowFailure}</span>}
      {/* A note-save failure normally lives inside the panel, beside the buttons
          that produced it. Whenever the panel is not on screen it falls back to
          the row instead: the rating may have disappeared underneath an open
          editor (another client retracts the feedback, a `version-conflict`
          reply commits `current: null`, the item goes away), or the human may
          have closed the panel before a slow save came back. Either way the row
          reports that the save did not land rather than dropping it. */}
      {!(rating !== undefined && noteOpen) && noteFailure !== null && (
        <span className={css.failure} role="status">{noteFailure}</span>
      )}
      {rating !== undefined && noteOpen && createPortal(
        <div
          ref={panelRef}
          className={css.notePanel}
          role="dialog"
          aria-label={t('note.dialog')}
          style={pos ?? MEASURE_STYLE}
        >
          <textarea
            ref={inputRef}
            className={css.noteInput}
            aria-label={t('note.aria')}
            placeholder={t('note.placeholder')}
            value={draft}
            rows={3}
            onChange={(event) => { setDraft(event.target.value) }}
          />
          <div className={css.noteActions}>
            <button
              type="button"
              className={css.noteSave}
              disabled={pending}
              onClick={() => { onSaveNote(rating) }}
            >
              {t('note.save')}
            </button>
            <button type="button" className={css.noteCancel} onClick={closeNote}>
              {t('note.cancel')}
            </button>
          </div>
          {noteFailure !== null && <span className={css.failure} role="status">{noteFailure}</span>}
        </div>,
        document.body,
      )}
    </>
  )
}
