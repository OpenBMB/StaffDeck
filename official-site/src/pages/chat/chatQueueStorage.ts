import type { ChatAttachmentRead } from '@/types';

import type { ComposerInteractionMode } from './chatTypes';

const CHAT_QUEUE_STORAGE_PREFIX = 'skill_agent_chat_queue';
const INTERACTION_MODES = new Set<ComposerInteractionMode>(['normal', 'scheduled_task']);

type ChatQueueStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export type PreparedChatTurn = {
  queueId: string;
  conversationId: string;
  agentId: string;
  turnId: string;
  text: string;
  attachments: ChatAttachmentRead[];
  interactionMode: ComposerInteractionMode;
  modelConfigId?: string;
  createdAt: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isQueuedAttachment(value: unknown): value is ChatAttachmentRead {
  if (!isRecord(value)) return false;
  return (
    typeof value.id === 'string'
    && typeof value.filename === 'string'
    && typeof value.content_type === 'string'
    && typeof value.size === 'number'
    && ['text', 'pdf', 'image', 'binary'].includes(String(value.kind || ''))
  );
}

function isPreparedChatTurn(value: unknown): value is PreparedChatTurn {
  if (!isRecord(value)) return false;
  return (
    typeof value.queueId === 'string'
    && typeof value.conversationId === 'string'
    && typeof value.agentId === 'string'
    && typeof value.turnId === 'string'
    && typeof value.text === 'string'
    && Array.isArray(value.attachments)
    && value.attachments.every(isQueuedAttachment)
    && typeof value.interactionMode === 'string'
    && INTERACTION_MODES.has(value.interactionMode as ComposerInteractionMode)
    && (value.modelConfigId === undefined || typeof value.modelConfigId === 'string')
    && typeof value.createdAt === 'string'
    && Number.isFinite(Date.parse(value.createdAt))
  );
}

export function chatQueueStorageKey(tenantId: string, userId: string): string {
  return `${CHAT_QUEUE_STORAGE_PREFIX}:${tenantId || 'default'}:${userId || 'anonymous'}`;
}

export function readQueuedChatTurns(storage: ChatQueueStorage, key: string): PreparedChatTurn[] {
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) throw new Error('Invalid chat queue payload');

    const seen = new Set<string>();
    const turns = parsed.filter((value): value is PreparedChatTurn => {
      if (!isPreparedChatTurn(value)) return false;
      const identity = `${value.queueId}:${value.turnId}`;
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
    if (turns.length !== parsed.length) {
      writeQueuedChatTurns(storage, key, turns);
    }
    return turns;
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // Storage access can be blocked by the browser privacy policy.
    }
    return [];
  }
}

export function writeQueuedChatTurns(
  storage: ChatQueueStorage,
  key: string,
  turns: PreparedChatTurn[],
): boolean {
  try {
    if (turns.length === 0) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, JSON.stringify(turns));
    }
    return true;
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // Storage cleanup is best-effort when the browser quota is unavailable.
    }
    return false;
  }
}
