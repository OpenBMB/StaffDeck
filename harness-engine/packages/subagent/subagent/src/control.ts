/**
 * Browser-facing subagent control assembly: the catalog view sampled against
 * the live Agent registry, one browser zone's validation, and the stable
 * failure codes the Remote surface answers with.
 *
 * @module @deepseek-ai/dsh-subagent
 */

import type { Context } from '@deepseek-ai/cordis'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import type { SessionId } from '@deepseek-ai/dsh-session'
import { RemoteError } from '@deepseek-ai/dsh-typert-protocol'
import { z } from 'zod'
import type {
  SubagentCatalog, SubagentListEntry, SubagentPromptContentPart,
} from './control-types.ts'
import { SubagentError } from './error.ts'

const SESSION_ID_SCHEMA = z.string().min(1)
const CONTROL_ID_SCHEMAS = {
  'subagent.list': z.object({ parentSessionId: SESSION_ID_SCHEMA }),
  'subagent.prompt': z.object({
    parentSessionId: SESSION_ID_SCHEMA,
    childSessionId: SESSION_ID_SCHEMA,
    mode: z.literal('continuable'),
  }),
  'subagent.interrupt': z.object({
    parentSessionId: SESSION_ID_SCHEMA,
    childSessionId: SESSION_ID_SCHEMA,
    mode: z.literal('continuable'),
  }),
} as const

/**
 * Apply the subagent payload checks that are stricter than generated
 * branded-string codecs.
 * @param method - method name carried in the failure message.
 * @param payload - decoded control fields to validate.
 * @throws {RemoteError} `gateway/bad-request` with the original Zod issues.
 */
export function validateControlRequest(
  method: keyof typeof CONTROL_ID_SCHEMAS,
  payload: unknown,
): void {
  const parsed = CONTROL_ID_SCHEMAS[method].safeParse(payload)
  if (!parsed.success) {
    throw new RemoteError('gateway/bad-request', `invalid payload for ${method}`, { issues: parsed.error.issues })
  }
}

/**
 * Admit the content one continuation may deliver, refusing every image.
 *
 * The blocks become the child's user message verbatim, and this surface admits
 * no attachment: nothing here registers encoded bytes with the attachment
 * service, so an image would reach the child as a reference nothing resolves.
 * The wire accepts the encoded upload so this refusal — not a Client that
 * strips the block — is what the caller is answered with. Other block types
 * still cross unnarrowed.
 * @param childSessionId - the addressed child, named by the refusal.
 * @param content - blocks the caller asked to deliver.
 * @returns the admitted blocks, in order, as the durable content vocabulary.
 * @throws {RemoteError} `subagent/attachment-unsupported` when any block is an image.
 */
export function admitPromptContent(
  childSessionId: SessionId,
  content: readonly SubagentPromptContentPart[],
): ContentBlock[] {
  const admitted: ContentBlock[] = []
  for (const block of content) {
    if (block.type === 'image') {
      throw new RemoteError(
        'subagent/attachment-unsupported',
        'subagent continuation does not accept images',
        { childSessionId, reason: 'SUBAGENT_IMAGE_UNSUPPORTED' },
      )
    }
    admitted.push(block)
  }
  return admitted
}

/**
 * Project one durable listing onto the catalog view, replacing each row's
 * store-derived activity with the live Agent driver's status and reporting
 * whether the exact parent Agent is live. Without an Agent registry no driver
 * runs at all, so every row is inactive and the parent is unavailable.
 * @param ctx - Host context that may carry the Agent registry.
 * @param parentSessionId - the listed parent.
 * @param entries - the durable direct-child listing.
 * @returns the catalog view answered to one browser.
 */
export function catalogView(
  ctx: Context,
  parentSessionId: SessionId,
  entries: readonly SubagentListEntry[],
): SubagentCatalog {
  const agents = ctx.get('agents')
  return {
    entries: entries.map((entry): SubagentListEntry => entry.kind === 'child'
      ? { ...entry, activity: agents?.get(entry.id)?.status === 'running' ? 'running' : 'inactive' }
      : entry),
    parentAvailable: agents?.get(parentSessionId) !== undefined,
  }
}

/**
 * Refuse one catalog read while preserving cancellation and a missing
 * projections registry as distinct failures.
 * @param error - the thrown value.
 * @param signal - the caller's cancellation.
 * @returns Never — the refusal is thrown.
 * @throws {RemoteError} always.
 */
export function rejectCatalogRead(error: unknown, signal: AbortSignal): never {
  if (isCancellation(error, signal)) {
    throw new RemoteError('gateway/cancelled', 'subagent catalog read was cancelled', {}, { cause: error })
  }
  if (error instanceof SubagentError && error.code === 'SUBAGENT_CONTROL_PROJECTIONS_UNAVAILABLE') {
    throw new RemoteError(
      'subagent/projections-unavailable',
      'subagent catalog is unavailable: this deployment does not mount the sessionProjections registry (load @deepseek-ai/dsh-session-projection)',
      {},
      { cause: error },
    )
  }
  throw new RemoteError('gateway/internal', 'subagent catalog read failed', {}, { cause: error })
}

/**
 * Refuse one continuation prompt without exposing provider detail: admission
 * failures the caller can act on keep their own code, everything else is
 * internal.
 * @param error - the thrown value.
 * @param childSessionId - the addressed child.
 * @param signal - the caller's cancellation.
 * @returns Never — the refusal is thrown.
 * @throws {RemoteError} always.
 */
export function rejectPrompt(error: unknown, childSessionId: SessionId, signal: AbortSignal): never {
  if (isCancellation(error, signal)) {
    throw new RemoteError('gateway/cancelled', 'subagent prompt was cancelled', {}, { cause: error })
  }
  if (error instanceof SubagentError) {
    switch (error.code) {
      case 'NOT_RESUMABLE':
        throw new RemoteError(
          'subagent/not-resumable',
          'subagent cannot be resumed',
          { childSessionId },
          { cause: error },
        )
      case 'UNAUTHORIZED':
        throw new RemoteError(
          'subagent/unauthorized',
          'subagent does not belong to this parent',
          { childSessionId },
          { cause: error },
        )
      case 'DRAINING':
      case 'ACTIVATION_CLOSING':
      case 'CONTINUATION_UNAVAILABLE':
      case 'PERSISTENCE_UNAVAILABLE':
        throw new RemoteError(
          'subagent/delivery-unavailable',
          'subagent follow-up is temporarily unavailable',
          { childSessionId },
          { cause: error },
        )
      // A code outside the admission vocabulary is not the caller's move to make.
      default:
        break
    }
  }
  throw new RemoteError('gateway/internal', 'subagent prompt failed', {}, { cause: error })
}

function isCancellation(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof SubagentError && error.code === 'CANCELLED')
}
