/**
 * Display projection of reference forms in sent user text (bubble and queue
 * rows). The logged model text remains the single truth; this is presentation
 * only, and every part renders inline so a single-line message never breaks
 * across lines. Three decoration sources, by precedence: the wire session form
 * `@[label](dsh-session:...)` folds to its label; exact session labels
 * supplied by an adjacent recall decorate their bare `@label` mention; and
 * plain `/name` / `@name` word-boundary tokens decorate by shape alone (sent
 * tokens were validated at compose time).
 */
import type { ReactNode } from 'react'
import { ReferenceIcon } from './ReferenceIcon.tsx'
import css from './user-text.module.css'

interface DecorationRange {
  readonly start: number
  readonly end: number
  /** Matched source text (hover title). */
  readonly label: string
  readonly kind: 'session' | 'plain'
  /** Pre-resolved display text (wire folds); derived from label when absent. */
  readonly display?: string
}

/**
 * Split one sent text into inline plain runs and reference chips.
 * @param text - the logged model text of the message or queue row.
 * @param sessionLabels - exact session mention labels associated by an adjacent recall.
 * @returns inline nodes covering the whole text.
 */
export function projectUserText(text: string, sessionLabels: readonly string[]): ReactNode {
  const ranges: DecorationRange[] = []
  const labelStops: number[] = []
  const uriStops: number[] = []
  for (let i = 0; i < text.length; i++) {
    if (text[i] === ']' || text.charCodeAt(i) === 10) labelStops.push(i)
    if (/[\s)]/u.test(text[i]!)) uriStops.push(i)
  }
  labelStops.push(text.length); uriStops.push(text.length)
  let labelIndex = 0
  let uriIndex = 0
  let search = 0
  while (search < text.length) {
    const start = text.indexOf('@[', search)
    if (start < 0) break
    while (labelStops[labelIndex]! < start + 2) labelIndex++
    const close = labelStops[labelIndex]!
    if (close === text.length) break
    search = close + 1
    if (text[close] !== ']' || close === start + 2 || !text.startsWith('(dsh-session:', close + 1)) continue
    const uriStart = close + 2
    while (uriStops[uriIndex]! < uriStart) uriIndex++
    const end = uriStops[uriIndex]!
    if (text[end] !== ')' || end === uriStart + 'dsh-session:'.length) continue
    ranges.push({ start, end: end + 1, label: text.slice(start, end + 1),
                  kind: 'session', display: text.slice(start + 2, close) })
    search = end + 1
  }
  for (const rawLabel of [...new Set(sessionLabels)].sort((a, b) => b.length - a.length)) {
    const label = `@${rawLabel}`
    let start = text.indexOf(label)
    while (start >= 0) {
      ranges.push({ start, end: start + label.length, label, kind: 'session' })
      start = text.indexOf(label, start + label.length)
    }
  }
  const re = /(^|\s)(\/[\w-]+|@"[^"\n]+"|@[^\s]+)/gu
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const tokenStart = m.index + (m[1] as string).length // (^|\s) captures '' at line start
    const rawLabel = m[2] as string // non-optional alternation capture
    let end = rawLabel.length
    if (!rawLabel.startsWith('@"')) {
      while (end > 0 && /[.,;:!?，。；：！？]/u.test(rawLabel[end - 1]!)) end--
    }
    const label = rawLabel.slice(0, end)
    if (label.length <= 1) continue
    ranges.push({ start: tokenStart, end: tokenStart + label.length, label, kind: 'plain' })
  }
  const rankOf = (range: DecorationRange): number => range.kind === 'session' ? 0 : 1
  ranges.sort((a, b) => a.start - b.start || rankOf(a) - rankOf(b) || b.end - a.end)
  const parts: ReactNode[] = []
  let cursor = 0
  const pushPlain = (from: number, to: number): void => {
    parts.push(<span key={`t${from}`} className={css.plainRun}>{text.slice(from, to)}</span>)
  }
  for (const range of ranges) {
    if (range.start < cursor) continue
    const { start: tokenStart, end, label, kind } = range
    if (tokenStart > cursor) pushPlain(cursor, tokenStart)
    const referenceKind = kind === 'session'
      ? 'session'
      : label.startsWith('@')
        ? label.endsWith('/') ? 'folder' : 'file'
        : undefined
    const displayLabel = range.display
      ?? (referenceKind === undefined
        ? label
        : referenceKind === 'session'
          ? label.slice(1)
          : label.slice(1).replace(/^"|"$/gu, '').split(/[\\/]/u).filter(Boolean).at(-1) ?? label.slice(1))
    parts.push(
      <span
        key={tokenStart}
        className={css.refChip}
        data-ref-chip={referenceKind ?? 'skill'}
        title={label}
      >
        {referenceKind !== undefined && (
          <ReferenceIcon kind={referenceKind} size={16} className={css.refIcon} />
        )}
        {displayLabel}
      </span>,
    )
    cursor = end
  }
  if (parts.length === 0) return <span className={css.plainRun}>{text}</span>
  if (cursor < text.length) pushPlain(cursor, text.length)
  return <>{parts}</>
}
