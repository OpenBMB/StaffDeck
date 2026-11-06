/** UI Subagent-owned projection of descendant counts from Session summaries. */

import type { SessionId } from '@deepseek-ai/dsh-session/types'

interface LineageEntry {
  readonly id: SessionId
  readonly parentId?: SessionId
  readonly origin?: 'subagent'
  readonly running: boolean
}

/** Descendant counts for one possible parent Session. */
export interface SubagentDescendantSummary {
  readonly count: number
  readonly runningCount: number
}

/* jscpd:ignore-start -- UI Subagent and UI Workspace independently project their own views. */
/**
 * Index uninterrupted subagent descendants under each ancestor.
 * @param summaries - Session summaries keyed by id.
 * @returns descendant totals keyed by possible parent id.
 */
export function indexSubagentDescendants(
  summaries: Readonly<Record<SessionId, LineageEntry>>,
): ReadonlyMap<SessionId, SubagentDescendantSummary> {
  const indexed = new Map<SessionId, { count: number; runningCount: number }>()
  for (const descendant of Object.values(summaries)) {
    if (descendant.origin !== 'subagent') continue
    const seen = new Set<SessionId>()
    let current: LineageEntry | undefined = descendant
    while (current?.origin === 'subagent' && current.parentId !== undefined && !seen.has(current.id)) {
      seen.add(current.id)
      const aggregate = indexed.get(current.parentId)
      if (aggregate === undefined) {
        indexed.set(current.parentId, { count: 1, runningCount: descendant.running ? 1 : 0 })
      } else {
        aggregate.count += 1
        if (descendant.running) aggregate.runningCount += 1
      }
      current = summaries[current.parentId]
    }
  }
  return indexed
}
/* jscpd:ignore-end */
