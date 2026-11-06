/** Deterministic provider for model-visible foreground and Job diagnostic snapshots. */

import type { Context } from '@deepseek-ai/cordis'
import {
  NO_START_CAPABILITIES,
  type ResolvedSubagentStartRequest,
  type SubagentProvider,
} from '@deepseek-ai/dsh-subagent'
import { SessionId } from '@deepseek-ai/dsh-session'

export const name = 'subagent-result-diagnostic'
export const inject = ['subagents']

const RESULTS = [
  {
    id: '00000000-0000-4000-8000-0000000000d1',
    diagnostic: 'Product subagent failure (product: Claude Code; stage: query-run; category: limit)',
    output: [{ type: 'text' as const, text: 'partial assistant text' }],
  },
  {
    id: '00000000-0000-4000-8000-0000000000d2',
    diagnostic: 'Product subagent failure (product: Claude Code; stage: query-run; category: limit)',
    output: [],
  },
  {
    id: '00000000-0000-4000-8000-0000000000d3',
    diagnostic: 'Product subagent failure (product: Codex; stage: turn; category: transport; HTTP status: 503)',
    output: [{ type: 'text' as const, text: 'partial assistant text' }],
  },
  {
    id: '00000000-0000-4000-8000-0000000000d4',
    diagnostic: 'Product subagent failure (product: Codex; stage: turn; category: transport; HTTP status: 503)',
    output: [],
  },
] as const

class DiagnosticProvider implements SubagentProvider {
  readonly name = 'snapshot-diagnostic'
  readonly capabilities = NO_START_CAPABILITIES
  readonly inheritsParentContext = false
  private starts = 0

  async start(request: ResolvedSubagentStartRequest) {
    if (request.signal.aborted) {
      throw new Error('snapshot diagnostic provider start aborted')
    }
    const index = this.starts++
    const fixture = RESULTS[index]
    if (fixture === undefined) {
      throw new Error('snapshot diagnostic provider expected exactly four starts')
    }
    return {
      id: SessionId(fixture.id),
      localAgent: undefined,
      result: Promise.resolve({
        output: [...fixture.output],
        diagnostic: fixture.diagnostic,
        stopReason: 'error' as const,
      }),
      dispose: async () => {},
    }
  }
}

/** Register the fixed provider behind the public Codex-shaped snapshot tool. */
export function apply(ctx: Context): void {
  ctx.subagents.registerProvider(new DiagnosticProvider())
}
