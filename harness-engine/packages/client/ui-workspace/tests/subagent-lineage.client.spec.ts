import { describe, expect, it } from 'vitest'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import { indexSubagentDescendants } from '../src/client/subagent-lineage.ts'

const sid = (id: string): SessionId => id as SessionId

describe('UI Workspace descendant projection', () => {
  it('counts nested running descendants and stops at ordinary forks', () => {
    const root = { id: sid('root'), running: false }
    const child = { id: sid('child'), parentId: root.id, origin: 'subagent' as const, running: true }
    const leaf = { id: sid('leaf'), parentId: child.id, origin: 'subagent' as const, running: false }
    const fork = { id: sid('fork'), parentId: child.id, running: false }
    const result = indexSubagentDescendants(Object.fromEntries(
      [root, child, leaf, fork].map(item => [item.id, item]),
    ))
    expect(result.get(root.id)).toEqual({ count: 2, runningCount: 1 })
    expect(result.get(child.id)).toEqual({ count: 1, runningCount: 0 })
    expect(result.has(fork.id)).toBe(false)
  })

  it('terminates cycles and retains missing-parent aggregates', () => {
    const a = { id: sid('a'), parentId: sid('b'), origin: 'subagent' as const, running: false }
    const b = { id: sid('b'), parentId: sid('a'), origin: 'subagent' as const, running: false }
    const orphan = { id: sid('orphan'), parentId: sid('missing'), origin: 'subagent' as const, running: true }
    const result = indexSubagentDescendants({ [a.id]: a, [b.id]: b, [orphan.id]: orphan })
    expect(result.get(a.id)?.count).toBe(2)
    expect(result.get(b.id)?.count).toBe(2)
    expect(result.get(sid('missing'))).toEqual({ count: 1, runningCount: 1 })
  })
})
