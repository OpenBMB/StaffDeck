/** General Settings row for completed-Turn transcript presentation. */

import { useState } from 'react'
import type { SnapshotStore } from '@deepseek-ai/dsh-client-store'
import { IconChevronDownOutline14, Menu } from '@deepseek-ai/dsh-client-ui-primitives'
import type { InjectFace, PropsLocale, PropsRuntime } from '@deepseek-ai/dsh-client-ui-slots'
import type { TranscriptViewMode } from '../../chat-settings.ts'
import type { ChatKey } from '../locale.ts'
import css from './TranscriptViewRow.module.css'

/** Registration-side transcript preference face. */
export interface TranscriptViewRowInjected {
  hooks: {
    /** Persisted transcript preference bound as useTranscriptView. */
    transcriptView: SnapshotStore<TranscriptViewMode>
  }
  /** Change the completed-Turn transcript presentation. */
  setTranscriptView: (mode: TranscriptViewMode) => void
}

/** Full Settings-row props. */
export type TranscriptViewRowProps =
  PropsRuntime<'settings.general.item'>
  & PropsLocale<'chat'>
  & InjectFace<TranscriptViewRowInjected>

const OPTIONS: readonly { id: TranscriptViewMode; label: ChatKey }[] = [
  { id: 'normal', label: 'settings.transcript.normal' },
  { id: 'compact', label: 'settings.transcript.compact' },
]

/**
 * Render the completed-Turn transcript mode selector.
 * @param props - composed Settings slot props.
 * @returns the preference row.
 */
export function TranscriptViewRow({ useTranscriptView, setTranscriptView, t }: TranscriptViewRowProps) {
  const mode = useTranscriptView(value => value)
  const [open, setOpen] = useState(false)
  const selectedLabel = mode === 'normal'
    ? 'settings.transcript.normal'
    : 'settings.transcript.compact'
  const closeMenu = () => { setOpen(false) }
  const selectMode = (id: string) => {
    closeMenu()
    setTranscriptView(id as TranscriptViewMode)
  }
  const selector = (
    <button
      type="button"
      className={css.selector}
      aria-haspopup="menu"
      aria-expanded={open}
      onClick={() => { setOpen(value => !value) }}
    >
      {t(selectedLabel)}
      <IconChevronDownOutline14 className={css.chevron} />
    </button>
  )

  return (
    <div className={css.row}>
      <div className={css.rowText}>
        <div className={css.title}>{t('settings.transcript.title')}</div>
        <div className={css.desc}>{t('settings.transcript.description')}</div>
      </div>
      <Menu
        open={open}
        onClose={closeMenu}
        items={OPTIONS.map(option => ({ id: option.id, label: t(option.label) }))}
        selectedId={mode}
        onSelect={selectMode}
        align="end"
        portal
        anchor={selector}
      />
    </div>
  )
}
