import type { ReactNode } from 'react';

import CodeBlock from '@/components/CodeBlock';
import { ApiError } from '@/api/client';
import type { StreamEvent } from '@/api/client';
import { formatClientDateTime } from '@/lib/timezone';
import type {
  ChatAttachmentRead,
  ChatMessage,
  ChatSession,
  ChatSessionEventRead,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
  UIConfigRead,
} from '@/types';

import {
  CHAT_MARKDOWN_CLASS,
  CHAT_MD_TABLE_CLASS,
  CHAT_MD_TABLE_SCROLL_CLASS,
} from './chatPageStyles';
import type {
  ComposerAttachment,
  CotTraceIconName,
  DraftScheduleType,
  SessionSlot,
  TraceLine,
  TraceSkill,
  TraceTool,
  TurnTrace,
} from './chatTypes';
export {
  SELECTED_AGENT_STORAGE_KEY,
  SESSION_FILTER_STORAGE_PREFIX,
  sessionFilterStorageKey,
} from '@/lib/agent-scope-storage';

export const MODEL_CONFIG_STORAGE_PREFIX = 'skill_agent_selected_model_config';
export const SESSION_READ_STORAGE_PREFIX = 'skill_agent_session_read_at';
export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'skill_agent_sidebar_collapsed';
export const RUNNING_EVENT_RECOVERY_WINDOW_MS = 600 * 1000;
export const CHAT_STREAM_IDLE_TIMEOUT_MS = 600 * 1000;
export const CHAT_STREAM_IDLE_CHECK_INTERVAL_MS = 5 * 1000;
export const CHAT_STREAM_HEARTBEAT_GRACE_MS = 20 * 1000;
export const CHAT_TRACE_RECOVERY_WINDOW_MS = 10 * 60 * 1000;
export const STREAM_TERMINAL_EVENTS = new Set(['complete', 'done', 'stream_end', 'stream_cancelled', 'stream_interrupted', 'error', 'error_occurred']);
export const HIDDEN_GENERAL_SKILL_TRACE_PHASES = new Set(['replying']);
const DRAFT_SCHEDULE_TYPES = new Set<DraftScheduleType>(['once', 'daily', 'weekly', 'monthly']);
const DRAFT_SCHEDULE_TYPE_LABELS: Record<DraftScheduleType, string> = {
  once: '一次性',
  daily: '每天',
  weekly: '每周',
  monthly: '每月',
};
const DRAFT_WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

export function sessionReadStorageKey(userId: string): string {
  return `${SESSION_READ_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

export function loadSessionReadTimes(userId: string): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(sessionReadStorageKey(userId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function persistSessionReadTimes(userId: string, values: Record<string, string>): void {
  window.localStorage.setItem(sessionReadStorageKey(userId), JSON.stringify(values));
}

export function isScheduledSession(session: ChatSession): boolean {
  return session.is_scheduled === true;
}

export function sessionHasUnreadReply(
  session: ChatSession,
  readTimes: Record<string, string>,
  activeSessionId?: string,
): boolean {
  if (session.id === activeSessionId) return false;
  const summary = session.summary || session.last_agent_question || '';
  if (!summary) return false;
  if (session.status === 'running' || session.status === 'executing') return false;
  const updatedAt = Date.parse(session.updated_at || '');
  const readAt = Date.parse(readTimes[session.id] || '');
  return Number.isFinite(updatedAt) && (!Number.isFinite(readAt) || updatedAt > readAt + 1000);
}

export function draftConversationKey(agentId: string): string {
  return `draft:${agentId}`;
}

export function isDraftConversationKey(id: string): boolean {
  return id.startsWith('draft:');
}

export function isMissingChatSessionError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function modelStorageKey(tenantId: string): string {
  return `${MODEL_CONFIG_STORAGE_PREFIX}:${tenantId}`;
}

export function modelDisplayName(model: { name?: string; model?: string }): string {
  return (model.name || model.model || '模型').trim();
}

export function modelDetailText(model: { name?: string; model?: string; provider?: string; is_default?: boolean }): string {
  const detail = model.model && model.model !== model.name ? model.model : model.provider || '';
  return model.is_default ? `${detail} · 默认` : detail;
}

export function normalizeMessageText(value?: string): string {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

export function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]*`|\*\*[^*]+?\*\*|!?\[[^\]\n]*\]\([^\)\n]+\))/g;
  let cursor = 0;
  let index = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > cursor) {
      nodes.push(text.slice(cursor, match.index));
    }
    const token = match[0];
    const key = `${keyPrefix}-inline-${index}`;
    if (token.startsWith('`') && token.endsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('**') && token.endsWith('**')) {
      nodes.push(<strong key={key}>{renderInlineMarkdown(token.slice(2, -2), key)}</strong>);
    } else {
      const image = token.match(/^!\[([^\]]*)\]\(([^\)\n]+)\)$/);
      if (image) {
        nodes.push(<span key={key}>{image[1] || '图片'}</span>);
        cursor = match.index + token.length;
        index += 1;
        continue;
      }
      const link = token.match(/^\[([^\]]*)\]\(([^\)\n]+)\)$/);
      if (link) {
        const href = link[2].trim();
        const label = link[1] || href;
        if (/^https?:\/\//i.test(href)) {
          nodes.push(
            <a key={key} href={href} target="_blank" rel="noreferrer">
              {label}
            </a>,
          );
        } else {
          nodes.push(
            <span key={key} className="md-link-label" title={href}>
              {label}
            </span>,
          );
        }
      } else {
        nodes.push(token);
      }
    }
    cursor = match.index + token.length;
    index += 1;
  }

  if (cursor < text.length) {
    nodes.push(text.slice(cursor));
  }
  return nodes;
}

function renderInlineLines(lines: string[], keyPrefix: string): ReactNode[] {
  return lines.flatMap((line, lineIndex) => {
    const nodes = renderInlineMarkdown(line, `${keyPrefix}-line-${lineIndex}`);
    if (lineIndex === 0) return nodes;
    return [<br key={`${keyPrefix}-br-${lineIndex}`} />, ...nodes];
  });
}

type MarkdownTableAlign = 'left' | 'center' | 'right';

function splitMarkdownTableRow(row: string): string[] {
  let text = row.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);

  const cells: string[] = [];
  let current = '';
  let inCode = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '`') {
      inCode = !inCode;
      current += char;
      continue;
    }
    if (char === '\\' && text[index + 1] === '|') {
      current += '|';
      index += 1;
      continue;
    }
    if (char === '|' && !inCode) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += char;
  }
  cells.push(current.trim());
  return cells;
}

function isMarkdownTableSeparator(line: string): boolean {
  const cells = splitMarkdownTableRow(line);
  return cells.length >= 2 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, '')));
}

function markdownTableAlign(separatorCell: string): MarkdownTableAlign {
  const normalized = separatorCell.replace(/\s+/g, '');
  if (normalized.startsWith(':') && normalized.endsWith(':')) return 'center';
  if (normalized.endsWith(':')) return 'right';
  return 'left';
}

function isMarkdownTableStart(lines: string[], index: number): boolean {
  if (index + 1 >= lines.length) return false;
  const header = lines[index].trim();
  if (!header.includes('|')) return false;
  return splitMarkdownTableRow(header).length >= 2 && isMarkdownTableSeparator(lines[index + 1]);
}

function renderMarkdownTable(lines: string[], startIndex: number, key: string): { node: ReactNode; nextIndex: number } {
  const header = splitMarkdownTableRow(lines[startIndex]);
  const separator = splitMarkdownTableRow(lines[startIndex + 1]);
  const aligns = separator.map(markdownTableAlign);
  const rows: string[][] = [];
  let index = startIndex + 2;

  while (index < lines.length) {
    const row = lines[index].trim();
    if (!row || !row.includes('|') || isMarkdownTableSeparator(row)) break;
    const cells = splitMarkdownTableRow(row);
    if (cells.length < 2) break;
    rows.push(cells);
    index += 1;
  }

  const columnCount = Math.max(header.length, separator.length, ...rows.map((row) => row.length));
  const cellStyle = (cellIndex: number) => ({ textAlign: (aligns[cellIndex] || 'left') as MarkdownTableAlign });
  const renderCells = (cells: string[], rowKey: string) =>
    Array.from({ length: columnCount }, (_, cellIndex) => (
      <td key={`${rowKey}-${cellIndex}`} style={cellStyle(cellIndex)}>
        {renderInlineMarkdown(cells[cellIndex] || '', `${rowKey}-${cellIndex}`)}
      </td>
    ));

  return {
    nextIndex: index,
    node: (
      <div key={key} className={CHAT_MD_TABLE_SCROLL_CLASS}>
        <table className={CHAT_MD_TABLE_CLASS}>
          <thead>
            <tr>
              {Array.from({ length: columnCount }, (_, cellIndex) => (
                <th key={`${key}-head-${cellIndex}`} style={cellStyle(cellIndex)}>
                  {renderInlineMarkdown(header[cellIndex] || '', `${key}-head-${cellIndex}`)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`${key}-row-${rowIndex}`}>{renderCells(row, `${key}-row-${rowIndex}`)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    ),
  };
}

function isBlockBoundary(line: string): boolean {
  const trimmed = line.trim();
  return (
    trimmed.startsWith('```') ||
    /^(-{3,}|\*{3,}|_{3,})$/.test(trimmed) ||
    /^#{1,6}\s+/.test(trimmed) ||
    /^>\s?/.test(trimmed) ||
    /^[-*]\s+/.test(trimmed) ||
    /^\d+[.)]\s+/.test(trimmed)
  );
}

export function renderMarkdownBlocks(content: string): ReactNode[] {
  const lines = content.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let index = 0;
  let blockIndex = 0;

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    const key = `md-${blockIndex}`;
    if (!trimmed) {
      index += 1;
      continue;
    }

    if (trimmed.startsWith('```')) {
      const language = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <CodeBlock key={key} className="md-code-block" code={codeLines.join('\n')} language={language || undefined} />,
      );
      blockIndex += 1;
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      blocks.push(<hr key={key} />);
      index += 1;
      blockIndex += 1;
      continue;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key}>{renderInlineMarkdown(heading[2], key)}</Tag>);
      index += 1;
      blockIndex += 1;
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      const quoteLines: string[] = [];
      while (index < lines.length && /^>\s?/.test(lines[index].trim())) {
        quoteLines.push(lines[index].trim().replace(/^>\s?/, ''));
        index += 1;
      }
      blocks.push(<blockquote key={key}>{renderMarkdownBlocks(quoteLines.join('\n'))}</blockquote>);
      blockIndex += 1;
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      const table = renderMarkdownTable(lines, index, key);
      blocks.push(table.node);
      index = table.nextIndex;
      blockIndex += 1;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ul key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ul>,
      );
      blockIndex += 1;
      continue;
    }

    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (index < lines.length && /^\d+[.)]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^\d+[.)]\s+/, ''));
        index += 1;
      }
      blocks.push(
        <ol key={key}>
          {items.map((item, itemIndex) => (
            <li key={`${key}-${itemIndex}`}>{renderInlineMarkdown(item, `${key}-${itemIndex}`)}</li>
          ))}
        </ol>,
      );
      blockIndex += 1;
      continue;
    }

    const paragraphLines: string[] = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !isBlockBoundary(lines[index]) &&
      !isMarkdownTableStart(lines, index)
    ) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    blocks.push(<p key={key}>{renderInlineLines(paragraphLines, key)}</p>);
    blockIndex += 1;
  }

  return blocks;
}

export function MarkdownMessage({ content }: { content: string }) {
  return <div className={CHAT_MARKDOWN_CLASS}>{renderMarkdownBlocks(content)}</div>;
}

export function traceSummaryIconName(_summary: { state: TraceLine['state'] }): CotTraceIconName {
  return 'execute';
}

export function traceLineIconName(line: TraceLine): CotTraceIconName {
  if (line.icon) return line.icon;
  if (line.kind === 'decision') return 'judge';
  if (line.kind === 'tool') return 'tool';
  if (line.kind === 'code') return 'generated';
  if (line.kind === 'thinking') return 'loading';
  return 'advance';
}

export function parseMessageTime(value?: string): number {
  if (!value) return 0;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const time = Date.parse(normalized);
  return Number.isFinite(time) ? time : 0;
}

function appendTurnAlias(aliases: string[], value: unknown): void {
  if (typeof value !== 'string') return;
  const normalized = value.trim();
  if (normalized && !aliases.includes(normalized)) aliases.push(normalized);
}

function metadataString(messageItem: ChatMessage, key: string): string | undefined {
  const value = messageItem.metadata?.[key];
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

export function messageTurnAliases(messageItem: ChatMessage): string[] {
  const aliases: string[] = [];
  appendTurnAlias(aliases, messageItem.turnId);
  appendTurnAlias(aliases, messageItem.turn_id);
  appendTurnAlias(aliases, metadataString(messageItem, 'turn_id'));
  appendTurnAlias(aliases, metadataString(messageItem, 'user_message_id'));
  appendTurnAlias(aliases, metadataString(messageItem, 'client_turn_id'));
  appendTurnAlias(aliases, messageItem.serverMessageId);
  if (messageItem.role === 'user') appendTurnAlias(aliases, messageItem.id);
  return aliases;
}

function preferredTurnAlias(messageItem: ChatMessage, aliases: string[]): string | undefined {
  if (messageItem.role === 'user' && !messageItem.id.startsWith('local_')) return messageItem.id;
  return aliases[0];
}

export function buildTurnAliasMap(messages: ChatMessage[]): Map<string, string> {
  const parent = new Map<string, string>();

  const find = (value: string): string => {
    const current = parent.get(value);
    if (!current) {
      parent.set(value, value);
      return value;
    }
    if (current === value) return value;
    const root = find(current);
    parent.set(value, root);
    return root;
  };

  const union = (canonical: string, alias: string) => {
    const canonicalRoot = find(canonical);
    const aliasRoot = find(alias);
    if (canonicalRoot !== aliasRoot) {
      parent.set(aliasRoot, canonicalRoot);
    }
  };

  messages.forEach((messageItem) => {
    const aliases = messageTurnAliases(messageItem);
    const canonical = preferredTurnAlias(messageItem, aliases);
    if (!canonical) return;
    aliases.forEach((alias) => union(canonical, alias));
  });

  const result = new Map<string, string>();
  parent.forEach((_value, key) => {
    result.set(key, find(key));
  });
  return result;
}

export function canonicalTurnIdForValue(turnId: string | null | undefined, aliasMap: Map<string, string>): string | undefined {
  const normalized = typeof turnId === 'string' ? turnId.trim() : '';
  if (!normalized) return undefined;
  return aliasMap.get(normalized) || normalized;
}

export function canonicalMessageTurnId(messageItem: ChatMessage, aliasMap: Map<string, string>): string | undefined {
  const aliases = messageTurnAliases(messageItem);
  for (const alias of aliases) {
    const canonical = aliasMap.get(alias);
    if (canonical) return canonical;
  }
  return effectiveMessageTurnId(messageItem);
}

function latestUserMessageForTurn(slot: SessionSlot, turnId?: string | null): ChatMessage | undefined {
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  const scoped = messages.filter((messageItem) => (
    messageItem.role === 'user'
    && (!canonicalTurnId || canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId)
  ));
  const candidates = scoped.length
    ? scoped
    : messages.filter((messageItem) => messageItem.role === 'user');
  return candidates.sort((left, right) => parseMessageTime(right.created_at) - parseMessageTime(left.created_at))[0];
}

function timestampAfterMessage(messageItem?: ChatMessage): string {
  const baseTime = messageItem ? parseMessageTime(messageItem.created_at) : 0;
  return new Date((baseTime > 0 ? baseTime : Date.now()) + 1).toISOString();
}

function hasServerMessageForTurn(messageItem: ChatMessage, serverMessages: ChatMessage[]): boolean {
  const messages = [...serverMessages, messageItem];
  const aliasMap = buildTurnAliasMap(messages);
  const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
  if (!messageTurnId) return false;
  return serverMessages.some(
    (serverMessage) => (
      canonicalMessageTurnId(serverMessage, aliasMap) === messageTurnId
      && serverMessage.role === messageItem.role
    ),
  );
}

export function sameRoleTurn(left: ChatMessage, right: ChatMessage): boolean {
  const aliasMap = buildTurnAliasMap([left, right]);
  const leftTurnId = canonicalMessageTurnId(left, aliasMap);
  const rightTurnId = canonicalMessageTurnId(right, aliasMap);
  return Boolean(leftTurnId && rightTurnId && leftTurnId === rightTurnId && left.role === right.role);
}

export function hasAssistantMessageForTurn(slot: SessionSlot, turnId: string): boolean {
  if (!turnId) return false;
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  return messages.some((messageItem) => (
    messageItem.role === 'assistant'
    && !messageItem.isStreaming
    && canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId
    && Boolean(normalizeMessageText(messageItem.content))
  ));
}

export function hasAssistantCarrierForTurn(slot: SessionSlot, turnId: string): boolean {
  if (!turnId) return false;
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  const aliasMap = buildTurnAliasMap(messages);
  const canonicalTurnId = canonicalTurnIdForValue(turnId, aliasMap);
  return messages.some((messageItem) => (
    messageItem.role === 'assistant'
    && !messageItem.isStreaming
    && canonicalMessageTurnId(messageItem, aliasMap) === canonicalTurnId
    && (
      Boolean(normalizeMessageText(messageItem.content))
      || messageItem.isError
      || messageItem.id.startsWith('__trace_')
    )
  ));
}

export function streamingMessageId(sessionId: string, turnId?: string | null): string {
  const normalizedTurnId = typeof turnId === 'string' ? turnId.trim() : '';
  return normalizedTurnId ? `__streaming_${sessionId}_${normalizedTurnId}` : `__streaming_${sessionId}`;
}

export function isStreamingMessageId(messageId: string, sessionId: string): boolean {
  const prefix = `__streaming_${sessionId}`;
  return messageId === prefix || messageId.startsWith(`${prefix}_`);
}

export function upsertStreamingTracePlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const streamId = streamingMessageId(sessionId, turnId);
  const streamingMessage: ChatMessage = {
    id: streamId,
    turnId,
    role: 'assistant',
    content: '',
    created_at: timestampAfterMessage(latestUserMessageForTurn(slot, turnId)),
    isStreaming: true,
  };
  const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
  if (index >= 0) {
    const current = slot.realtimeMessages[index];
    if (
      current.turnId === streamingMessage.turnId
      && current.isStreaming
      && current.content === streamingMessage.content
    ) {
      return false;
    }
    slot.realtimeMessages = [...slot.realtimeMessages];
    slot.realtimeMessages[index] = { ...current, ...streamingMessage, created_at: current.created_at || streamingMessage.created_at };
    return true;
  }
  slot.realtimeMessages = [...slot.realtimeMessages, streamingMessage];
  return true;
}

export function upsertTraceStatusPlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const traceId = `__trace_${sessionId}_${turnId}`;
  const streamId = streamingMessageId(sessionId, turnId);
  const traceMessage: ChatMessage = {
    id: traceId,
    turnId,
    role: 'assistant',
    content: '',
    created_at: timestampAfterMessage(latestUserMessageForTurn(slot, turnId)),
    isStreaming: false,
  };
  const existingAliasMap = buildTurnAliasMap([...slot.serverMessages, ...slot.realtimeMessages, traceMessage]);
  const canonicalTraceTurnId = canonicalTurnIdForValue(turnId, existingAliasMap);
  const existingAssistantIndex = slot.realtimeMessages.findIndex((item) => (
    item.role === 'assistant'
    && item.id !== traceId
    && item.id !== streamId
    && canonicalMessageTurnId(item, existingAliasMap) === canonicalTraceTurnId
  ));
  if (existingAssistantIndex >= 0) return false;
  const index = slot.realtimeMessages.findIndex((item) => item.id === traceId);
  if (index >= 0) {
    const current = slot.realtimeMessages[index];
    if (current.turnId === traceMessage.turnId && current.content === traceMessage.content) return false;
    slot.realtimeMessages = [...slot.realtimeMessages];
    slot.realtimeMessages[index] = { ...current, ...traceMessage, created_at: current.created_at || traceMessage.created_at };
    return true;
  }
  const streamingIndex = slot.realtimeMessages.findIndex((item) => (
    item.id === streamId
    && canonicalMessageTurnId(item, existingAliasMap) === canonicalTraceTurnId
  ));
  if (streamingIndex >= 0) {
    const current = slot.realtimeMessages[streamingIndex];
    slot.realtimeMessages = slot.realtimeMessages.filter((item, itemIndex) => (
      itemIndex === streamingIndex
      || !(
        item.turnId === turnId
        && item.role === 'assistant'
        && (item.id === traceId || item.id === streamId)
      )
    ));
    const nextIndex = slot.realtimeMessages.findIndex((item) => item === current);
    slot.realtimeMessages[nextIndex] = {
      ...current,
      id: traceId,
      isStreaming: false,
      created_at: current.created_at || traceMessage.created_at,
    };
    return true;
  }
  slot.realtimeMessages = [
    ...slot.realtimeMessages.filter((item) => item.id !== streamId || item.turnId !== turnId),
    traceMessage,
  ];
  return true;
}

export function explicitMessageTurnId(messageItem: ChatMessage): string | undefined {
  const camelTurnId = typeof messageItem.turnId === 'string' ? messageItem.turnId.trim() : '';
  if (camelTurnId) return camelTurnId;
  const snakeTurnId = typeof messageItem.turn_id === 'string' ? messageItem.turn_id.trim() : '';
  return snakeTurnId || undefined;
}

export function effectiveMessageTurnId(messageItem: ChatMessage): string | undefined {
  return explicitMessageTurnId(messageItem) || (messageItem.role === 'user' ? messageItem.id : undefined);
}

export function explicitStreamTurnId(data: Record<string, unknown>, fallbackTurnId: string): string {
  const turnId = typeof data.turn_id === 'string' ? data.turn_id.trim() : '';
  if (turnId) return turnId;
  const userMessageId = typeof data.user_message_id === 'string' ? data.user_message_id.trim() : '';
  if (userMessageId) return userMessageId;
  return fallbackTurnId;
}

export function eventTraceTurnId(event: ChatSessionEventRead): string {
  const data = isPlainRecord(event.data) ? event.data : {};
  const explicit = explicitStreamTurnId(data, '');
  if (explicit) return explicit;
  if (event.event === 'user_message_received') {
    return typeof data.message_id === 'string' ? data.message_id.trim() : '';
  }
  return '';
}

export function normalizeSessionEventForStream(event: ChatSessionEventRead): StreamEvent {
  const data = isPlainRecord(event.data) ? event.data : {};
  if (event.event === 'stream_status') {
    return { event: 'status', data };
  }
  if (event.event === 'router_decision_created') {
    return { event: 'router_decision', data };
  }
  if (event.event === 'assistant_message_created') {
    const content = typeof data.reply === 'string' ? data.reply : '';
    return { event: 'stream_replace', data: { ...data, content } };
  }
  return { event: event.event, data };
}

export function isTerminalSessionEvent(
  event: ChatSessionEventRead,
  isTerminalStreamEvent: (event: ChatSessionEventRead) => boolean,
): boolean {
  if (event.event === 'assistant_message_created') return true;
  return isTerminalStreamEvent(event);
}

export function attachTurnIdsToServerMessages(
  serverMessages: ChatMessage[],
  realtimeMessages: ChatMessage[],
): ChatMessage[] {
  const realtimeTurnIdsByServerId = new Map(
    realtimeMessages
      .filter((item) => item.turnId && item.serverMessageId)
      .map((item) => [item.serverMessageId as string, item.turnId as string]),
  );

  return serverMessages.map((messageItem) => {
    const turnId = explicitMessageTurnId(messageItem) || realtimeTurnIdsByServerId.get(messageItem.id);
    if (turnId) return { ...messageItem, turnId };
    if (messageItem.role === 'user') return { ...messageItem, turnId: messageItem.id };
    return messageItem;
  });
}

function shouldKeepRealtimeMessage(
  messageItem: ChatMessage,
  serverMessages: ChatMessage[],
  latestServerTime: number,
  activeTurnId?: string | null,
): boolean {
  if (messageItem.role === 'user' && messageItem.metadata?.queued === true) return true;
  if (messageItem.isStreaming) {
    const aliasMap = buildTurnAliasMap([...serverMessages, messageItem]);
    const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
    const activeCanonicalTurnId = canonicalTurnIdForValue(activeTurnId, aliasMap);
    return !messageTurnId || !activeCanonicalTurnId || messageTurnId === activeCanonicalTurnId;
  }
  if (hasServerMessageForTurn(messageItem, serverMessages)) return false;
  if (messageItem.serverMessageId && serverMessages.some((serverMessage) => serverMessage.id === messageItem.serverMessageId)) {
    return false;
  }
  if (
    messageItem.role === 'assistant'
    && (
      Boolean(normalizeMessageText(messageItem.content))
      || messageItem.isError
      || messageItem.id.startsWith('__trace_')
    )
  ) {
    return true;
  }
  if (activeTurnId) {
    const aliasMap = buildTurnAliasMap([...serverMessages, messageItem]);
    const messageTurnId = canonicalMessageTurnId(messageItem, aliasMap);
    const activeCanonicalTurnId = canonicalTurnIdForValue(activeTurnId, aliasMap);
    if (messageTurnId && activeCanonicalTurnId && messageTurnId === activeCanonicalTurnId) return true;
  }
  if (!latestServerTime) return true;
  return parseMessageTime(messageItem.created_at) > latestServerTime;
}

export { shouldKeepRealtimeMessage, hasServerMessageForTurn, latestUserMessageForTurn, timestampAfterMessage };

export function computeMergedMessages(slot: SessionSlot, activeTurnId?: string | null): ChatMessage[] {
  const serverIds = new Set(slot.serverMessages.map((item) => item.id));
  const latestServerTime = Math.max(0, ...slot.serverMessages.map((item) => parseMessageTime(item.created_at)));
  const extras = slot.realtimeMessages.filter((item) => {
    if (serverIds.has(item.id)) return false;
    return shouldKeepRealtimeMessage(item, slot.serverMessages, latestServerTime, activeTurnId);
  });
  const combined = [
    ...slot.serverMessages.map((messageItem, index) => ({ messageItem, index, source: 'server' as const })),
    ...extras.map((messageItem, index) => ({ messageItem, index: slot.serverMessages.length + index, source: 'realtime' as const })),
  ];
  const aliasMap = buildTurnAliasMap(combined.map((entry) => entry.messageItem));
  const turnStarts = new Map<string, number>();
  combined.forEach(({ messageItem }) => {
    if (messageItem.role !== 'user') return;
    const turnId = canonicalMessageTurnId(messageItem, aliasMap);
    if (!turnId) return;
    const createdAt = parseMessageTime(messageItem.created_at);
    const previous = turnStarts.get(turnId);
    if (previous === undefined || createdAt < previous) {
      turnStarts.set(turnId, createdAt);
    }
  });
  combined.forEach(({ messageItem }) => {
    const turnId = canonicalMessageTurnId(messageItem, aliasMap);
    if (!turnId || turnStarts.has(turnId)) return;
    turnStarts.set(turnId, parseMessageTime(messageItem.created_at));
  });
  const roleOrder: Record<ChatMessage['role'], number> = {
    user: 0,
    assistant: 1,
    tool: 2,
    system: 3,
  };

  const sorted = combined
    .sort((left, right) => {
      const leftTurnId = canonicalMessageTurnId(left.messageItem, aliasMap);
      const rightTurnId = canonicalMessageTurnId(right.messageItem, aliasMap);
      const leftTurnStart = leftTurnId ? turnStarts.get(leftTurnId) : undefined;
      const rightTurnStart = rightTurnId ? turnStarts.get(rightTurnId) : undefined;
      const leftSortTime = leftTurnStart ?? parseMessageTime(left.messageItem.created_at);
      const rightSortTime = rightTurnStart ?? parseMessageTime(right.messageItem.created_at);
      if (leftSortTime !== rightSortTime) return leftSortTime - rightSortTime;
      if (leftTurnId && leftTurnId === rightTurnId && left.messageItem.role !== right.messageItem.role) {
        return (roleOrder[left.messageItem.role] ?? 3) - (roleOrder[right.messageItem.role] ?? 3);
      }
      return (
        parseMessageTime(left.messageItem.created_at) - parseMessageTime(right.messageItem.created_at) ||
        left.index - right.index
      );
    });

  const selectedAssistantByTurn = new Map<string, { messageItem: ChatMessage; index: number; source: 'server' | 'realtime' }>();
  const assistantRank = (entry: { messageItem: ChatMessage; source: 'server' | 'realtime' }) => {
    const content = normalizeMessageText(entry.messageItem.content);
    let rank = 0;
    if (entry.source === 'server') rank += 100;
    if (content) rank += 60;
    if (entry.messageItem.isStreaming && (!activeTurnId || entry.messageItem.turnId === activeTurnId)) rank += 40;
    if (!entry.messageItem.isStreaming) rank += 10;
    return rank;
  };
  sorted.forEach((entry) => {
    if (entry.messageItem.role !== 'assistant') return;
    const turnId = canonicalMessageTurnId(entry.messageItem, aliasMap);
    if (!turnId) return;
    const previous = selectedAssistantByTurn.get(turnId);
    if (!previous || assistantRank(entry) >= assistantRank(previous)) {
      selectedAssistantByTurn.set(turnId, entry);
    }
  });

  return sorted
    .filter((entry) => {
      if (entry.messageItem.role !== 'assistant') return true;
      const turnId = canonicalMessageTurnId(entry.messageItem, aliasMap);
      if (!turnId) return true;
      return selectedAssistantByTurn.get(turnId)?.messageItem === entry.messageItem;
    })
    .map((item) => item.messageItem);
}

function publicStreamPhase(data: Record<string, unknown>): string {
  const phase = typeof data.phase === 'string' ? data.phase : '';
  const text = typeof data.text === 'string' ? data.text : '';
  if (phase === 'error') return text || '请求失败';
  if (phase === 'scheduled_task_draft') return text || '生成定时任务草案';
  if (isKnowledgeTracePhase(phase)) return text || knowledgeTraceText(data);
  return '正在思考';
}

export { publicStreamPhase };

type RecoverableTraceProgress = {
  id?: string;
  kind?: string;
  text?: string;
  detail?: string | null;
  code?: string | null;
  output?: string | null;
  state?: string;
};

function hasRecoverableTraceProgress(lines: RecoverableTraceProgress[]): boolean {
  return lines.some((line) => {
    if (!line) return false;
    if (line.state && line.state !== 'running') return false;
    if (line.detail || line.code || line.output) return true;
    if (line.kind && line.kind !== 'decision') return true;
    const text = String(line.text || '').trim();
    return Boolean(text);
  });
}

export function hasRecoverableEventProgress(events: ChatSessionEventRead[]): boolean {
  return events.some((event) => {
    if (event.event === 'memory_recalled') return false;
    if (event.event === 'router_decision_created') {
      const data = isPlainRecord(event.data) ? event.data : {};
      const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
      const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
      const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
      return Boolean(intent || reason || decision);
    }
    return true;
  });
}

export function isRecoverableRunningTrace(row: { completed_at?: string | null; lines: RecoverableTraceProgress[]; started_at: string }): boolean {
  if (row.completed_at) return false;
  const startedAt = parseMessageTime(row.started_at);
  if (startedAt <= 0) return false;
  if (Date.now() - startedAt > CHAT_TRACE_RECOVERY_WINDOW_MS) return false;
  const lines = row.lines || [];
  return hasRecoverableTraceProgress(lines);
}

const KNOWLEDGE_TRACE_PHASES = new Set([
  'knowledge',
  'okf_route',
  'okf_only',
  'document_route',
  'document_route_lexical',
  'bucket_route',
  'bucket_route_lexical',
  'section_expand',
  'read_chunks',
  'evidence_pack',
  'no_visible_knowledge',
  'no_documents',
  'no_buckets',
]);

export function isKnowledgeTracePhase(phase: string): boolean {
  return KNOWLEDGE_TRACE_PHASES.has(phase);
}

export function knowledgeTraceText(data: Record<string, unknown>): string {
  const raw = typeof data.message === 'string'
    ? data.message
    : typeof data.text === 'string'
      ? data.text
      : '';
  if (!raw) return '检索知识库';
  return raw;
}

export function knowledgeTraceDetail(data: Record<string, unknown>): string | undefined {
  const query = isPlainRecord(data.query) && typeof data.query.query === 'string' ? data.query.query : '';
  const parts = [
    query ? `查询：${query}` : '',
    typeof data.selected_count === 'number' ? `命中知识图谱 ${data.selected_count} 个` : '',
    typeof data.candidate_count === 'number' ? `候选 ${data.candidate_count} 个` : '',
    typeof data.chunk_count === 'number' ? `读取 ${data.chunk_count} 个片段` : '',
    typeof data.evidence_count === 'number' ? `整理 ${data.evidence_count} 条证据` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : undefined;
}

export function knowledgeResultTraceDetail(data: Record<string, unknown>): string | undefined {
  const concepts = Array.isArray(data.selected_concepts) ? data.selected_concepts.length : 0;
  const chunks = Array.isArray(data.chunks) ? data.chunks.length : 0;
  const evidence = Array.isArray(data.evidence_pack) ? data.evidence_pack.length : 0;
  const parts = [
    concepts ? `命中知识图谱 ${concepts} 个` : '',
    chunks ? `读取 ${chunks} 个片段` : '',
    evidence ? `生成 ${evidence} 条引用候选` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : undefined;
}

export function normalizeTraceSkill(value: unknown): TraceSkill | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const skillId = typeof item.skillId === 'string' ? item.skillId : '';
  if (!skillId) return null;
  return {
    skillId,
    name: typeof item.name === 'string' ? item.name : skillId,
    stepId: typeof item.stepId === 'string' ? item.stepId : undefined,
    state: typeof item.state === 'string' ? item.state : undefined,
  };
}

export function streamSkillLabel(data: Record<string, unknown>, skill: TraceSkill): string {
  if (skill.state === 'suspended') return '挂起SOP';
  const decision = typeof data.runtimeDecision === 'string' ? data.runtimeDecision : '';
  const fromSkillId = typeof data.fromSkillId === 'string' ? data.fromSkillId : '';
  const toSkillId = typeof data.toSkillId === 'string' ? data.toSkillId : '';
  if (decision === 'start_skill') return '选择SOP';
  if (decision === 'suspend_current_and_start_new_skill') return '切换SOP';
  if (
    (decision === 'answer_related_question_then_resume' || decision === 'answer_chitchat_then_resume')
    && fromSkillId
    && toSkillId
    && fromSkillId !== toSkillId
  ) return '切换SOP';
  if (decision === 'exit_current_skill') return '恢复SOP';
  return '推进SOP';
}

export function normalizeTraceTool(value: unknown): TraceTool | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const toolId = typeof item.toolId === 'string' ? item.toolId : '';
  if (!toolId) return null;
  return {
    toolId,
    toolCallId: typeof item.toolCallId === 'string' ? item.toolCallId : undefined,
    toolName: typeof item.toolName === 'string' ? item.toolName : toolId,
    rawToolName: typeof item.rawToolName === 'string' ? item.rawToolName : toolId,
    success: typeof item.success === 'boolean' ? item.success : undefined,
    isError: typeof item.isError === 'boolean' ? item.isError : undefined,
    content: item.content,
  };
}

function shortTraceValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
}

export function toolTraceDetail(tool: TraceTool): string | undefined {
  const content = tool.content && typeof tool.content === 'object' ? tool.content as Record<string, unknown> : null;
  const data = content?.data && typeof content.data === 'object' ? content.data as Record<string, unknown> : null;
  const parts = [
    tool.rawToolName && tool.rawToolName !== tool.toolName ? tool.rawToolName : '',
    shortTraceValue(data?.source),
    data?.found === false ? '未命中' : data?.found === true ? '已命中' : '',
    shortTraceValue(data?.miss_reason),
    shortTraceValue(data?.recommendation),
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

export function reflectionTraceDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.reason === 'string' ? data.reason : '',
    typeof data.target_tool_name === 'string' ? `工具 ${data.target_tool_name}` : '',
    typeof data.target_skill_id === 'string' ? `SOP ${data.target_skill_id}` : '',
    typeof data.target_step_id === 'string' ? `步骤 ${data.target_step_id}` : '',
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

function streamErrorText(data: Record<string, unknown>, eventName: string): string {
  const code = typeof data.code === 'string' ? data.code.trim() : '';
  if (code === 'LLM_ERROR') return '模型调用失败';
  if (eventName === 'stream_interrupted') return '响应生成中断';
  if (code) return `执行失败 ${code}`;
  const errorType = typeof data.error_type === 'string' ? data.error_type.trim() : '';
  return errorType ? `执行失败 ${errorType}` : '执行失败';
}

function streamErrorDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.code === 'string' ? data.code.trim() : '',
    typeof data.error_type === 'string' ? data.error_type.trim() : '',
    typeof data.message === 'string' ? data.message.trim() : '',
    typeof data.reason === 'string' ? data.reason.trim() : '',
    typeof data.text === 'string' ? data.text.trim() : '',
  ].filter(Boolean);
  const deduped = parts.filter((part, index) => parts.indexOf(part) === index);
  return deduped.length > 0 ? deduped.join(' · ').slice(0, 2000) : undefined;
}

export function streamErrorTraceLine(data: Record<string, unknown>, eventName: string): TraceLine {
  const code = typeof data.code === 'string' ? data.code.trim() : '';
  const errorType = typeof data.error_type === 'string' ? data.error_type.trim() : '';
  const key = code || errorType || eventName || 'error';
  return {
    id: eventName === 'stream_interrupted' ? 'generation_interrupted' : `error_${key}`,
    kind: 'decision',
    text: streamErrorText(data, eventName),
    detail: streamErrorDetail(data),
    state: 'failed',
    icon: 'loading',
  };
}

export function routerDecisionTraceLine(data: Record<string, unknown>): TraceLine {
  const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
  const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
  const skillId = typeof data.target_skill_id === 'string' ? data.target_skill_id.trim() : '';
  const stepId = typeof data.target_step_id === 'string' ? data.target_step_id.trim() : '';
  const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
  const detail = [reason, skillId ? `目标SOP ${skillId}` : '', stepId ? `目标节点 ${stepId}` : '']
    .filter(Boolean)
    .join(' · ');
  return {
    id: 'decision_router',
    kind: 'decision',
    text: intent ? `判断意图 ${intent}` : decision ? `判断意图 ${decision}` : '判断意图',
    detail: detail || undefined,
    state: 'completed',
    icon: 'judge',
  };
}

export function stepResultTraceLine(data: Record<string, unknown>): TraceLine {
  const toolCall = isPlainRecord(data.tool_call) ? data.tool_call : undefined;
  const knowledgeQuery = isPlainRecord(data.knowledge_query) ? data.knowledge_query : undefined;
  const nextStepId = typeof data.next_step_id === 'string' ? data.next_step_id.trim() : '';
  const reply = typeof data.reply === 'string' ? data.reply.trim() : '';
  const toolName = typeof toolCall?.name === 'string' ? toolCall.name.trim() : '';
  const knowledgeQueryText = typeof knowledgeQuery?.query === 'string' ? knowledgeQuery.query.trim() : '';
  const detail = [
    nextStepId ? `下一节点 ${nextStepId}` : '',
    knowledgeQueryText ? `查询：${knowledgeQueryText}` : '',
    !toolName && !knowledgeQueryText && reply ? reply.slice(0, 80) : '',
  ].filter(Boolean).join(' · ');

  if (toolName) {
    return {
      id: `decision_step_tool_${toolName}`,
      kind: 'decision',
      text: `决定调用工具 ${toolName}`,
      detail: detail || undefined,
      state: 'running',
      icon: 'tool',
    };
  }
  if (knowledgeQueryText) {
    return {
      id: 'decision_step_knowledge',
      kind: 'decision',
      text: '决定查询知识库',
      detail: detail || undefined,
      state: 'running',
      icon: 'advance',
    };
  }
  return {
    id: 'decision_step_result',
    kind: 'decision',
    text: nextStepId ? '决定下一步' : '完成步骤判断',
    detail: detail || undefined,
    state: 'completed',
    icon: 'advance',
  };
}

export function mergeTraceLine(existing: TraceLine, incoming: TraceLine): TraceLine {
  const keepExistingContent = incoming.provisional === true && existing.provisional !== true;
  const nextState =
    existing.state !== 'running' && incoming.state === 'running'
      ? existing.state
      : incoming.state;
  return {
    ...existing,
    ...incoming,
    text: keepExistingContent ? existing.text : incoming.text || existing.text,
    detail: keepExistingContent ? existing.detail : incoming.detail ?? existing.detail,
    code: incoming.code ?? existing.code,
    language: incoming.language ?? existing.language,
    output: incoming.output ?? existing.output,
    outputLanguage: incoming.outputLanguage ?? existing.outputLanguage,
    outputTitle: incoming.outputTitle ?? existing.outputTitle,
    state: nextState,
    provisional: incoming.provisional === true && existing.provisional === true,
  };
}

function formatTracePayload(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function tracePayloadLanguage(value: string): string {
  if (!value.trim()) return 'text';
  try {
    JSON.parse(value);
    return 'json';
  } catch {
    return 'text';
  }
}

export function generalSkillTraceDetail(data: Record<string, unknown>, phase: string): string | undefined {
  const review = isPlainRecord(data.review) ? data.review : undefined;
  if (phase.startsWith('reflection_')) {
    return [
      typeof review?.reason === 'string' ? review.reason : '',
      typeof review?.repair_hint === 'string' ? review.repair_hint : '',
    ]
      .filter(Boolean)
      .join(' · ') || undefined;
  }
  const detail = typeof data.rationale === 'string'
    ? data.rationale
    : typeof data.text === 'string'
      ? data.text
      : undefined;
  return detail?.trim() || undefined;
}

export function generalSkillTraceOutput(data: Record<string, unknown>, phase: string, accumulatedText?: string): {
  output?: string;
  language?: string;
  title?: string;
} {
  if (phase === 'stdout_chunk') {
    const output = formatTracePayload(accumulatedText || data.stdout_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: '查看运行输出' } : {};
  }
  if (phase === 'stderr_chunk') {
    const output = formatTracePayload(accumulatedText || data.stderr_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: '查看错误输出' } : {};
  }
  if (phase === 'code_finished' || phase === 'code_timeout') {
    const result: Record<string, unknown> = {};
    if ('return_code' in data) result.return_code = data.return_code;
    if ('structured_result' in data) result.structured_result = data.structured_result;
    if (typeof data.stdout_preview === 'string' && data.stdout_preview.trim()) result.stdout = data.stdout_preview;
    if (typeof data.stderr_preview === 'string' && data.stderr_preview.trim()) result.stderr = data.stderr_preview;
    const output = Object.keys(result).length > 0
      ? formatTracePayload(result)
      : formatTracePayload(data.stdout_preview || data.stderr_preview || data.text);
    return output ? { output, language: tracePayloadLanguage(output), title: phase === 'code_timeout' ? '查看超时结果' : '查看执行结果' } : {};
  }
  if (phase.startsWith('reflection_')) {
    const result: Record<string, unknown> = {};
    if ('structured_result' in data) result.structured_result = data.structured_result;
    if ('review' in data) result.review = data.review;
    if (typeof data.stdout_preview === 'string' && data.stdout_preview.trim()) result.stdout = data.stdout_preview;
    if (typeof data.stderr_preview === 'string' && data.stderr_preview.trim()) result.stderr = data.stderr_preview;
    const output = Object.keys(result).length > 0 ? formatTracePayload(result) : '';
    return output ? { output, language: tracePayloadLanguage(output), title: '查看校验详情' } : {};
  }
  return {};
}

export function traceLineAllowed(line: TraceLine, config: UIConfigRead): boolean {
  if (line.state === 'failed') return true;
  if (line.kind === 'thinking' || line.kind === 'decision') return config.show_thinking_trace;
  if (line.kind === 'code') return config.show_thinking_trace;
  if (line.kind === 'skill') return config.show_skill_trace;
  if (line.kind === 'tool') return config.show_tool_trace;
  return true;
}

export function traceSummary(trace: TurnTrace, lines: TraceLine[]): { text: string; state: TraceLine['state'] } {
  if (trace.completedAt) {
    if (lines.some((line) => line.state === 'failed')) {
      return { text: '执行遇到问题', state: 'failed' };
    }
    return { text: '执行记录', state: 'completed' };
  }
  if (lines.some((line) => line.state === 'running')) {
    return { text: '执行记录', state: 'running' };
  }
  if (lines.some((line) => line.state === 'failed')) {
    return { text: '执行遇到问题', state: 'failed' };
  }
  return { text: '执行记录', state: 'completed' };
}

export function traceDetails(lines: TraceLine[]): TraceLine[] {
  const details = lines.filter((line) => {
    if (line.placeholder) return false;
    if (line.kind === 'thinking' && line.state !== 'failed') return false;
    return true;
  });
  return details.length > 0
    ? details
    : lines.filter((line) => !line.placeholder && (line.kind !== 'thinking' || line.state === 'failed'));
}

export function canRateMessage(item: ChatMessage): boolean {
  return (
    item.role === 'assistant'
    && !item.isStreaming
    && !item.isError
    && !item.id.startsWith('__')
    && !item.id.startsWith('text_')
    && !item.id.startsWith('error_')
  );
}

export function stripTrailingCitationSummary(content: string): string {
  return content;
}

function citationLabelsInContent(content: string): Set<number> {
  const labels = new Set<number>();
  content.replace(/\[(\d+)\]/g, (_match, value: string) => {
    const label = Number(value);
    if (Number.isInteger(label) && label >= 1) {
      labels.add(label);
    }
    return _match;
  });
  return labels;
}

function citationLabelNumber(citation: KnowledgeCitation, fallback: number): number {
  const labelText = citation.label || citation.id;
  const match = String(labelText || '').match(/\[(\d+)\]/);
  if (match) {
    const label = Number(match[1]);
    if (Number.isInteger(label) && label >= 1) {
      return label;
    }
  }
  return fallback;
}

export function knowledgeCitations(item: ChatMessage, content: string): KnowledgeCitation[] {
  const citations = item.metadata?.knowledge_citations;
  if (!Array.isArray(citations)) return [];
  const usedLabels = citationLabelsInContent(content);
  if (usedLabels.size === 0) return [];
  const seen = new Set<string>();
  const result: KnowledgeCitation[] = [];
  citations.forEach((citation, index) => {
    if (!citation || !citation.id) return;
    const labelNumber = citationLabelNumber(citation, index + 1);
    if (!usedLabels.has(labelNumber)) return;
    const identity = (
      citation.title || citation.section_path || citation.summary || citation.excerpt || citation.source_path || citation.concept_id || citation.id
    );
    const key = normalizeMessageText(identity).toLowerCase();
    if (!key || seen.has(key)) return;
    seen.add(key);
    result.push({ ...citation, label: `[${labelNumber}]` });
  });
  return result.sort((a, b) => citationLabelNumber(a, 0) - citationLabelNumber(b, 0));
}

export function scheduledDraftForMessage(item: ChatMessage): ScheduledTaskDraftRead | null {
  const draft = item.metadata?.scheduled_task_draft;
  if (!isPlainRecord(draft) || draft.should_create === false) return null;
  if (typeof draft.title !== 'string' || typeof draft.prompt !== 'string' || typeof draft.agent_id !== 'string') {
    return null;
  }
  return draft as unknown as ScheduledTaskDraftRead;
}

export function createdScheduledTaskForMessage(item: ChatMessage): ScheduledTaskRead | undefined {
  const task = item.metadata?.scheduled_task_created;
  if (!isPlainRecord(task)) return undefined;
  if (typeof task.id !== 'string' || typeof task.title !== 'string' || typeof task.prompt !== 'string') {
    return undefined;
  }
  return task as unknown as ScheduledTaskRead;
}

export function isScheduledTaskPrompt(item: ChatMessage): boolean {
  return item.role === 'user' && item.metadata?.interaction_mode === 'scheduled_task';
}

export function citationKindLabel(citation: KnowledgeCitation): string {
  if (citation.kind === 'concept') return '知识图谱';
  if (citation.kind === 'okf') return '知识图谱引用';
  return '引用来源';
}

export function citationDisplayTitle(citation: KnowledgeCitation): string {
  const raw = citation.title || citation.section_path || citation.source_path || citation.concept_id || '知识引用';
  return raw.trim() || '知识引用';
}

export function citationSourceLabel(citation: KnowledgeCitation): string {
  const raw = citation.source_path || '';
  if (!raw) return '';
  return raw.trim();
}

export function citationSectionLabel(citation: KnowledgeCitation): string {
  const raw = citation.section_path || citation.title || '';
  return raw.trim();
}

// ---------------------------------------------------------------------------
// Clipboard / pasted image helpers
// ---------------------------------------------------------------------------
const MAX_PASTED_REMOTE_IMAGES = 6;

type ClipboardImageItem = {
  types: readonly string[];
  getType: (type: string) => Promise<Blob>;
};

export function clipboardContainsComposerImage(clipboardData: DataTransfer): boolean {
  if (Array.from(clipboardData.files || []).some((file) => file.type.startsWith('image/'))) {
    return true;
  }
  if (Array.from(clipboardData.items || []).some((item) => item.kind === 'file' && item.type.startsWith('image/'))) {
    return true;
  }
  return extractImageSourceUrls(clipboardData.getData('text/html')).length > 0
    || extractImageSourceUrls(clipboardData.getData('text/plain')).length > 0;
}

export async function extractPastedComposerFiles(clipboardData: DataTransfer): Promise<File[]> {
  const files = extractPastedComposerFilesSync(clipboardData);
  const seen = new Set(files.map(pastedFileKey));

  const pushFile = (file: File | null | undefined) => {
    if (!file || file.size <= 0) return;
    const key = pastedFileKey(file);
    if (seen.has(key)) return;
    seen.add(key);
    files.push(file);
  };

  const imageSources = [
    ...extractImageSourceUrls(clipboardData.getData('text/html')),
    ...extractImageSourceUrls(clipboardData.getData('text/plain')),
  ].filter((source) => !isImageDataUrl(source));

  for (const [index, source] of imageSources.slice(0, MAX_PASTED_REMOTE_IMAGES).entries()) {
    pushFile(await imageSourceToFile(source, files.length + index));
  }

  if (files.length === 0) {
    const clipboardImages = await readClipboardImageItems();
    clipboardImages.forEach((file) => pushFile(file));
  }

  return files;
}

function extractPastedComposerFilesSync(clipboardData: DataTransfer): File[] {
  const files: File[] = [];
  const seen = new Set<string>();

  const pushFile = (file: File | null | undefined, index: number) => {
    if (!file || file.size <= 0) return;
    const normalized = normalizePastedFile(file, index);
    const key = pastedFileKey(normalized);
    if (seen.has(key)) return;
    seen.add(key);
    files.push(normalized);
  };

  Array.from(clipboardData.files || []).forEach((file, index) => pushFile(file, index));

  Array.from(clipboardData.items || []).forEach((item, index) => {
    if (item.kind !== 'file' || !item.type.startsWith('image/')) return;
    pushFile(item.getAsFile(), files.length + index);
  });

  const dataUrls = [
    ...extractImageDataUrls(clipboardData.getData('text/html')),
    ...extractImageDataUrls(clipboardData.getData('text/plain')),
  ];
  dataUrls.forEach((dataUrl, index) => pushFile(dataUrlToImageFile(dataUrl, index), files.length + index));

  return files;
}

function normalizePastedFile(file: File, index: number): File {
  const type = file.type || 'application/octet-stream';
  const hasUsefulName = Boolean(file.name && !/^image\.(png|jpe?g|gif|webp)$/i.test(file.name));
  if (hasUsefulName) return file;

  const filename = type.startsWith('image/')
    ? `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`
    : (file.name || `pasted-file-${Date.now()}-${index + 1}`);
  return new File([file], filename, { type, lastModified: file.lastModified || Date.now() });
}

function pastedFileKey(file: File): string {
  return `${file.type || 'application/octet-stream'}:${file.size}`;
}

function extractImageDataUrls(raw: string): string[] {
  return extractImageSourceUrls(raw).filter(isImageDataUrl);
}

function extractImageSourceUrls(raw: string): string[] {
  if (!raw) return [];
  const urls = new Set<string>();
  const text = raw.trim();
  if (isImageDataUrl(text) || isLikelyImageUrl(text)) {
    urls.add(text);
  }

  try {
    const document = new DOMParser().parseFromString(raw, 'text/html');
    Array.from(document.images).forEach((image) => {
      const src = image.getAttribute('src') || '';
      if (isSupportedPastedImageSource(src, true)) urls.add(src.trim());
    });
  } catch {
    // DOMParser is best-effort here; the regex below still catches inline image data.
  }

  const matches = raw.match(/data:image\/[a-z0-9.+-]+(?:;[a-z0-9.+-]+=[^,;]*)*;base64,[a-z0-9+/=\r\n]+/gi) || [];
  matches.forEach((url) => {
    if (isImageDataUrl(url)) urls.add(url);
  });
  const urlMatches = raw.match(/https?:\/\/[^\s"'<>]+/gi) || [];
  urlMatches.forEach((url) => {
    if (isLikelyImageUrl(url)) urls.add(url);
  });
  return Array.from(urls);
}

function isImageDataUrl(value: string): boolean {
  return /^data:image\/[a-z0-9.+-]+(?:;[^,]*)?,/i.test(value.trim());
}

function isSupportedPastedImageSource(value: string, fromImageElement = false): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (isImageDataUrl(trimmed)) return true;
  if (trimmed.startsWith('blob:')) return true;
  if (/^https?:\/\//i.test(trimmed)) return fromImageElement || isLikelyImageUrl(trimmed);
  if (trimmed.startsWith('//')) return fromImageElement || isLikelyImageUrl(`https:${trimmed}`);
  return false;
}

function isLikelyImageUrl(value: string): boolean {
  return /^https?:\/\/[^\s"'<>]+\.(?:png|jpe?g|gif|webp|bmp|svg|heic|tiff?)(?:[?#][^\s"'<>]*)?$/i.test(value.trim());
}

async function imageSourceToFile(source: string, index: number): Promise<File | null> {
  const normalized = normalizePastedImageSource(source);
  if (!normalized) return null;
  if (isImageDataUrl(normalized)) {
    return dataUrlToImageFile(normalized, index);
  }
  try {
    const response = await fetch(normalized);
    if (!response.ok) return null;
    const blob = await response.blob();
    if (!blob.type.startsWith('image/')) return null;
    return blobToPastedImageFile(blob, index, pastedImageNameFromUrl(normalized));
  } catch {
    return null;
  }
}

function normalizePastedImageSource(source: string): string | null {
  const trimmed = source.trim();
  if (!trimmed) return null;
  if (isImageDataUrl(trimmed) || trimmed.startsWith('blob:') || /^https?:\/\//i.test(trimmed)) return trimmed;
  if (trimmed.startsWith('//')) return `${window.location.protocol}${trimmed}`;
  return null;
}

function pastedImageNameFromUrl(source: string): string | undefined {
  try {
    const pathname = new URL(source, window.location.href).pathname;
    const name = decodeURIComponent(pathname.split('/').filter(Boolean).pop() || '');
    return isLikelyImageFilename(name) ? name : undefined;
  } catch {
    return undefined;
  }
}

function isLikelyImageFilename(value: string): boolean {
  return /\.(?:png|jpe?g|gif|webp|bmp|svg|heic|tiff?)$/i.test(value);
}

async function readClipboardImageItems(): Promise<File[]> {
  const clipboard = navigator.clipboard as (Clipboard & { read?: () => Promise<ClipboardImageItem[]> }) | undefined;
  if (!clipboard?.read) return [];
  try {
    const items = await clipboard.read();
    const files: File[] = [];
    for (const [index, item] of items.entries()) {
      const imageType = item.types.find((type) => type.startsWith('image/'));
      if (!imageType) continue;
      const blob = await item.getType(imageType);
      files.push(blobToPastedImageFile(blob, index));
    }
    return files;
  } catch {
    return [];
  }
}

function dataUrlToImageFile(dataUrl: string, index: number): File | null {
  const match = dataUrl.trim().match(/^data:(image\/[a-z0-9.+-]+)((?:;[^,]*)?),(.*)$/i);
  if (!match) return null;
  const type = match[1] || 'image/png';
  const meta = match[2] || '';
  const payload = match[3] || '';

  try {
    const bytes = meta.toLowerCase().includes(';base64')
      ? bytesFromBase64(payload)
      : new TextEncoder().encode(decodeURIComponent(payload));
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    return new File([buffer], `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`, { type });
  } catch {
    return null;
  }
}

function blobToPastedImageFile(blob: Blob, index: number, filename?: string): File {
  const type = blob.type || 'image/png';
  return new File([blob], filename || `pasted-image-${Date.now()}-${index + 1}.${imageExtension(type)}`, {
    type,
    lastModified: Date.now(),
  });
}

function bytesFromBase64(payload: string): Uint8Array {
  const binary = window.atob(payload.replace(/\s/g, ''));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function imageExtension(contentType: string): string {
  const normalized = contentType.toLowerCase().split(';')[0];
  if (normalized === 'image/jpeg' || normalized === 'image/jpg') return 'jpg';
  if (normalized === 'image/png') return 'png';
  if (normalized === 'image/gif') return 'gif';
  if (normalized === 'image/webp') return 'webp';
  if (normalized === 'image/bmp') return 'bmp';
  if (normalized === 'image/svg+xml') return 'svg';
  if (normalized === 'image/heic') return 'heic';
  if (normalized === 'image/tiff') return 'tiff';
  return 'png';
}

// ---------------------------------------------------------------------------
// Attachments
// ---------------------------------------------------------------------------
export function toRequestAttachment(attachment: ComposerAttachment): ChatAttachmentRead {
  const { uploadStatus: _uploadStatus, uploadKey: _uploadKey, ...rest } = attachment;
  return rest;
}

export function messageAttachments(messageItem: ChatMessage): ChatAttachmentRead[] {
  const attachments = messageItem.metadata?.attachments;
  if (!Array.isArray(attachments)) return [];
  return attachments.filter(isChatAttachment);
}

function isChatAttachment(value: unknown): value is ChatAttachmentRead {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<ChatAttachmentRead>;
  return typeof item.id === 'string' && typeof item.filename === 'string';
}

export function attachmentTypeLabel(attachment: ChatAttachmentRead): string {
  const size = formatAttachmentSize(attachment.size);
  const type = attachment.kind === 'pdf'
    ? 'PDF'
    : attachment.kind === 'image'
      ? '图片'
      : attachment.kind === 'text'
        ? '文本'
        : '文件';
  return `${type}${size ? ` · ${size}` : ''}`;
}

function formatAttachmentSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

// ---------------------------------------------------------------------------
// Scheduled task draft schedule helpers
// ---------------------------------------------------------------------------
export function formatDraftSchedule(draft: ScheduledTaskDraftRead): string {
  const schedule = draft.schedule || {};
  const scheduleType = normalizeDraftScheduleType(draft.schedule_type);
  if (scheduleType === 'weekly') {
    const weekdays = Array.isArray(schedule.weekdays)
      ? schedule.weekdays.map((item) => DRAFT_WEEKDAY_LABELS[Number(item)]).filter(Boolean).join('、')
      : '周一';
    return `每周 ${weekdays} ${schedule.time || '09:00'}`;
  }
  if (scheduleType === 'monthly') {
    return `每月 ${schedule.day_of_month || 1} 号 ${schedule.time || '09:00'}`;
  }
  if (scheduleType === 'once') {
    const value = String(schedule.run_at || '');
    const formatted = formatClientDateTime(value, '');
    return formatted
      ? `一次性 ${formatted}`
      : '一次性';
  }
  return `每天 ${schedule.time || '09:00'}`;
}

export function scheduleTypeLabel(type: ScheduledTaskDraftRead['schedule_type']): string {
  return DRAFT_SCHEDULE_TYPE_LABELS[normalizeDraftScheduleType(type)];
}

export function scheduleEditValue(draft: ScheduledTaskDraftRead): string {
  const schedule = draft.schedule || {};
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') return String(schedule.run_at || '');
  return String(schedule.time || '09:00');
}

export function scheduleFromEditValue(draft: ScheduledTaskDraftRead, value: string): Record<string, unknown> {
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') {
    return { ...(draft.schedule || {}), run_at: value };
  }
  return { ...(draft.schedule || {}), time: value };
}

export function draftScheduleForType(schedule: Record<string, unknown>, type: DraftScheduleType): Record<string, unknown> {
  const time = String(schedule.time || '09:00');
  if (type === 'once') {
    return { run_at: String(schedule.run_at || '') };
  }
  if (type === 'weekly') {
    return {
      time,
      weekdays: Array.isArray(schedule.weekdays) ? schedule.weekdays : [0],
    };
  }
  if (type === 'monthly') {
    return {
      time,
      day_of_month: schedule.day_of_month || 1,
    };
  }
  return { time };
}

export function normalizeDraftScheduleType(value: string): DraftScheduleType {
  const scheduleType = value as DraftScheduleType;
  return DRAFT_SCHEDULE_TYPES.has(scheduleType) ? scheduleType : 'daily';
}
