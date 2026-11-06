import type { ContentBlock, ToolSchema } from '@deepseek-ai/dsh-llm/types'
import type { SessionEvent } from '@deepseek-ai/dsh-session/types'
import type {
  AssistantProvenanceView, AssistantRequestConfig,
} from './records.ts'

export type {
  AssistantProvenanceView, AssistantRequestConfig,
} from './records.ts'

/** Complete model-visible request header in force for an ordinary generation. */
export interface ConversationPromptSnapshot {
  /** Provider/model and sampling configuration from the effective request header. */
  config: AssistantRequestConfig
  /** Rendered system prompt text; empty when the request had no system prompt. */
  system: string
  /** Complete tool catalog sent with the request, including tools that were never called. */
  tools: readonly ToolSchema[]
}

/** System/tool change introduced while preparing one ordinary request. */
export interface RequestPromptChange {
  /** Sequence of the request/header event that introduced this state. */
  seq: number
  /** Unix epoch ms from the request/header event. */
  time: number
  /** How the model-visible prompt differs from the previous recorded state. */
  kind: 'initial' | 'system' | 'tools' | 'system-and-tools'
  /** State immediately before this change; absent for the initial header. */
  previous?: ConversationPromptSnapshot
}

/** Canonical prompt snapshot and any model-visible change introduced by one request header. */
export interface RequestPromptInspection {
  /** Complete prompt state recorded by the header. */
  prompt: ConversationPromptSnapshot
  /** System/tool change relative to the preceding loaded header. */
  change?: RequestPromptChange
}

/**
 * The {@link inspectRequestPrompt} signature as a value seam: Chat and
 * Trajectory Definitions receive it from the uiConversation service because a
 * client bundle cannot value-import another plugin's module.
 */
export type RequestPromptInspector = (
  previous: ConversationPromptSnapshot | undefined,
  event: SessionEvent<'request/header'>,
) => RequestPromptInspection

/**
 * Canonicalize one request header and classify its model-visible prompt change.
 * @param previous - Prompt from the preceding loaded request header, when available.
 * @param event - Durable full request header to inspect.
 * @returns The canonical prompt and an initial/system/tool change when it can be established.
 */
export function inspectRequestPrompt(
  previous: ConversationPromptSnapshot | undefined,
  event: SessionEvent<'request/header'>,
): RequestPromptInspection {
  const header = event.data.header
  const rawTools: unknown = header.tools
  const prompt: ConversationPromptSnapshot = {
    config: header.config,
    system: header.system ?? '',
    tools: Array.isArray(rawTools) ? rawTools as readonly ToolSchema[] : [],
  }
  if (previous === undefined && event.data.reason !== 'initial') return { prompt }
  const systemChanged = previous !== undefined && previous.system !== prompt.system
  const toolsChanged = previous !== undefined
    && JSON.stringify(previous.tools) !== JSON.stringify(prompt.tools)
  if (previous !== undefined && !systemChanged && !toolsChanged) return { prompt }
  return {
    prompt,
    change: {
      seq: event.seq,
      time: event.time,
      kind: previous === undefined
        ? 'initial'
        : systemChanged && toolsChanged
          ? 'system-and-tools'
          : systemChanged ? 'system' : 'tools',
      ...(previous === undefined ? {} : { previous }),
    },
  }
}

/** Lifecycle fields shared by ordinary generation and compaction requests. */
interface RequestViewBase {
  /** Sequence that opened the operation represented by this request. */
  startSeq: number
  startedAt: number
  completedAt: number | null
  status: 'running' | 'complete' | 'error'
  error?: string
  /** Stable provider code for localized presentation of known failures. */
  errorCode?: string
  provenance?: AssistantProvenanceView
  requestConfig?: AssistantRequestConfig
  usage?: unknown
  /** Assistant message or compaction summary sequence produced by this request. */
  resultSeq?: number
}

/** One ordinary assistant generation assembled from durable request events. */
interface AssistantRequestView extends RequestViewBase {
  purpose: 'assistant'
  turn: number
  /** Agent-loop step that issued this request. */
  step: number
  /** Effective ordinary request input, inherited until a later header changes it. */
  prompt?: ConversationPromptSnapshot
  /** Prompt change logged while preparing this request. */
  promptChange?: RequestPromptChange
  /** Retry ordinal scheduled after a failed ordinary request. */
  retry?: number
  maxRetries?: number
  retryDelayMs?: number
}

/** One compaction provider request, either turn-owned or standalone between turns. */
interface CompactionRequestView extends RequestViewBase {
  purpose: 'compaction'
  /** Owning turn, or `null` when manual compaction ran between turns. */
  turn: number | null
  /** Direct compaction requests do not consume an agent-loop step. */
  step: 0
  /** Compaction replacement message sequence, when one was committed. */
  replacementSeq?: number
  /** Safe compaction summary projection. */
  summary?: readonly ContentBlock[]
  /** Complete compaction provider output before the safe projection. */
  rawOutput?: readonly ContentBlock[]
}

/** One provider request assembled from durable request lifecycle events. */
export type RequestView = AssistantRequestView | CompactionRequestView

/** Request data consumed by the stage-oriented Trajectory layout. */
export interface RequestInspectionSnapshot {
  requests: readonly RequestView[]
  callSchemas: ReadonlyMap<string, ToolSchema>
}
