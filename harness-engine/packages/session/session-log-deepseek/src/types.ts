/** Wire types for lossless incremental DeepSeek session-log upload. */

import type { SessionEvent, SessionHeader } from '@deepseek-ai/dsh-session'

/** Versioned incremental session-log field carried by an official DeepSeek request. */
export interface DeepSeekSessionLogExtension {
  readonly version: 1
  readonly session: SessionHeader
  /** Highest sequence durably recorded as accepted before this request, or `-1`. */
  readonly afterSeq: number
  /** Highest sequence represented by {@link events}. */
  readonly throughSeq: number
  /** Complete canonical event envelopes for every sequence from `afterSeq + 1` through `throughSeq`. */
  readonly events: readonly SessionEvent[]
}

declare module '@deepseek-ai/dsh-deepseek-llm-api-extensions/types' {
  interface DeepSeekLlmApiExtensionMap {
    dsh_session_log: DeepSeekSessionLogExtension
  }
}

declare module '@deepseek-ai/dsh-session/types' {
  interface SessionEventMap {
    /** Records that the configured endpoint accepted one delivery through `throughSeq`. */
    'session-log-deepseek/delivery-accepted': {
      /** Session identity the accepted delivery carried; inherited fork markers retain the parent's id. */
      sessionId: import('@deepseek-ai/dsh-session/types').SessionId
      /** Last canonical event included in the accepted request. */
      throughSeq: number
    }
  }
}
