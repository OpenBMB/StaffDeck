import type { ChatAttachmentRead, ChatMessage } from '@/types';

export type SessionSlot = {
  serverMessages: ChatMessage[];
  realtimeMessages: ChatMessage[];
};

export type StreamSlot = {
  loading: boolean;
  phase: string;
  timer: number | null;
  accumulated: string;
  turnId: string | null;
  cancelledTurnId: string | null;
  abortController: AbortController | null;
  relayRecoveryStartedAt: number | null;
  relayRecoveryTurnId: string | null;
};

export type TraceSkill = {
  skillId: string;
  name?: string;
  stepId?: string;
  state?: string;
};

export type TraceTool = {
  toolId: string;
  toolCallId?: string;
  toolName: string;
  rawToolName?: string;
  success?: boolean;
  isError?: boolean;
  content?: unknown;
};

export type CotTraceIconName = 'advance' | 'execute' | 'generated' | 'judge' | 'loading' | 'select' | 'tool';

export type TraceLine = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  text: string;
  detail?: string;
  code?: string;
  language?: string;
  output?: string;
  outputLanguage?: string;
  outputTitle?: string;
  state: 'running' | 'completed' | 'failed';
  collapsible?: boolean;
  icon?: CotTraceIconName;
  placeholder?: boolean;
  provisional?: boolean;
};

export type TurnTrace = {
  lines: TraceLine[];
  startedAt: number;
  completedAt?: number;
};

export type ComposerAttachment = ChatAttachmentRead & {
  uploadStatus: 'uploading' | 'ready' | 'error';
  uploadKey: string;
};

export type ComposerInteractionMode = 'normal' | 'scheduled_task';
export type DraftScheduleType = 'once' | 'daily' | 'weekly' | 'monthly';

export function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

export function createStreamSlot(): StreamSlot {
  return {
    loading: false,
    phase: '',
    timer: null,
    accumulated: '',
    turnId: null,
    cancelledTurnId: null,
    abortController: null,
    relayRecoveryStartedAt: null,
    relayRecoveryTurnId: null,
  };
}

export function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}
