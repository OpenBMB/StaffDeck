import { Button, Dropdown, Empty, Input, Modal, Select, Typography, message } from 'antd';
import type { ChangeEvent, ClipboardEvent, DragEvent, MouseEvent, ReactNode } from 'react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ApiError,
  SHOW_DEBUG,
  TENANT_ID,
  api,
  clearAuthSession,
  getAuthSession,
  isAuthError,
  streamChatTurn,
  uploadChatAttachments,
} from '../api/client';
import type { ChatStreamEvent } from '../api/client';
import CodeBlock from '../components/CodeBlock';
import EmployeeAvatarMark from '../components/EmployeeAvatarMark';
import StaffdeckIcon from '../components/StaffdeckIcon';
import {
  agentResourceCount,
  employeeDisplayName,
  employeeProfile,
  staffdeckDisplayText,
  visibleChatEmployees,
} from '../employee';
import { ThemeToggleButton } from '../theme';
import type {
  AgentProfileRead,
  ChatAttachmentRead,
  ChatMessage,
  ChatSessionEventRead,
  ChatSession,
  ChatTurnResponse,
  HumanHandoffRead,
  KnowledgeCitation,
  ModelConfigRead,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
  TurnTraceRead,
  UIConfigRead,
} from '../types';

type SessionSlot = {
  serverMessages: ChatMessage[];
  realtimeMessages: ChatMessage[];
};

type StreamSlot = {
  loading: boolean;
  phase: string;
  timer: number | null;
  accumulated: string;
  turnId: string | null;
  cancelledTurnId: string | null;
  abortController: AbortController | null;
};

type TraceSkill = {
  skillId: string;
  name?: string;
  stepId?: string;
  state?: string;
};

type TraceTool = {
  toolId: string;
  toolCallId?: string;
  toolName: string;
  rawToolName?: string;
  success?: boolean;
  isError?: boolean;
  content?: unknown;
};

type TraceLine = {
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
};

type TurnTrace = {
  lines: TraceLine[];
  startedAt: number;
  completedAt?: number;
};

type ComposerAttachment = ChatAttachmentRead & {
  uploadStatus: 'uploading' | 'ready' | 'error';
  uploadKey: string;
};
type ComposerInteractionMode = 'normal' | 'scheduled_task';
type DraftScheduleType = 'once' | 'daily' | 'weekly' | 'monthly';
type CotTraceIconName = 'advance' | 'execute' | 'generated' | 'judge' | 'loading' | 'select' | 'tool';
const MODEL_CONFIG_STORAGE_PREFIX = 'skill_agent_selected_model_config';
const SESSION_READ_STORAGE_PREFIX = 'skill_agent_session_read_at';
const RUNNING_EVENT_RECOVERY_WINDOW_MS = 5 * 60 * 1000;
const CHAT_STREAM_IDLE_TIMEOUT_MS = 90 * 1000;
const CHAT_TRACE_RECOVERY_WINDOW_MS = 10 * 60 * 1000;
const STREAM_TERMINAL_EVENTS = new Set(['complete', 'done', 'stream_end', 'stream_cancelled', 'error']);
const HIDDEN_GENERAL_SKILL_TRACE_PHASES = new Set(['replying']);
const DRAFT_SCHEDULE_TYPES = new Set<DraftScheduleType>(['once', 'daily', 'weekly', 'monthly']);
const DRAFT_SCHEDULE_TYPE_LABELS: Record<DraftScheduleType, string> = {
  once: '一次性',
  daily: '每天',
  weekly: '每周',
  monthly: '每月',
};
const DRAFT_WEEKDAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
function sessionReadStorageKey(userId: string): string {
  return `${SESSION_READ_STORAGE_PREFIX}:${userId || 'anonymous'}`;
}

function loadSessionReadTimes(userId: string): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(sessionReadStorageKey(userId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function persistSessionReadTimes(userId: string, values: Record<string, string>): void {
  window.localStorage.setItem(sessionReadStorageKey(userId), JSON.stringify(values));
}

function isScheduledSession(session: ChatSession): boolean {
  return (session.title || '').startsWith('定时任务：');
}

function sessionHasUnreadReply(session: ChatSession, readTimes: Record<string, string>, activeSessionId?: string): boolean {
  if (session.id === activeSessionId) return false;
  const summary = session.summary || session.last_agent_question || '';
  if (!summary) return false;
  if (summary.includes('正在') || summary.includes('执行中')) return false;
  const updatedAt = Date.parse(session.updated_at || '');
  const readAt = Date.parse(readTimes[session.id] || '');
  return Number.isFinite(updatedAt) && (!Number.isFinite(readAt) || updatedAt > readAt + 1000);
}

function draftConversationKey(agentId: string): string {
  return `draft:${agentId}`;
}

function isDraftConversationKey(id: string): boolean {
  return id.startsWith('draft:');
}

function isMissingChatSessionError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404 && error.message === 'Session not found';
}

function modelStorageKey(tenantId: string): string {
  return `${MODEL_CONFIG_STORAGE_PREFIX}:${tenantId}`;
}

function modelDisplayName(model: ModelConfigRead): string {
  return (model.name || model.model || '模型').trim();
}

function modelDetailText(model: ModelConfigRead): string {
  const detail = model.model && model.model !== model.name ? model.model : model.provider;
  return model.is_default ? `${detail} · 默认` : detail;
}

function createEmptySlot(): SessionSlot {
  return { serverMessages: [], realtimeMessages: [] };
}

function createStreamSlot(): StreamSlot {
  return {
    loading: false,
    phase: '',
    timer: null,
    accumulated: '',
    turnId: null,
    cancelledTurnId: null,
    abortController: null,
  };
}

function createTurnTrace(): TurnTrace {
  return { lines: [], startedAt: Date.now() };
}

function normalizeMessageText(value?: string): string {
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : '';
}

function renderInlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const pattern = /(`[^`]*`|\*\*[^*]+?\*\*|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g;
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
      const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
      if (link) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer">
            {link[1]}
          </a>,
        );
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
  const cellClassName = (cellIndex: number) => `md-table-cell align-${aligns[cellIndex] || 'left'}`;
  const renderCells = (cells: string[], rowKey: string) =>
    Array.from({ length: columnCount }, (_, cellIndex) => (
      <td key={`${rowKey}-${cellIndex}`} className={cellClassName(cellIndex)}>
        {renderInlineMarkdown(cells[cellIndex] || '', `${rowKey}-${cellIndex}`)}
      </td>
    ));

  return {
    nextIndex: index,
    node: (
      <div key={key} className="md-table-scroll">
        <table className="md-table">
          <thead>
            <tr>
              {Array.from({ length: columnCount }, (_, cellIndex) => (
                <th key={`${key}-head-${cellIndex}`} className={cellClassName(cellIndex)}>
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
    /^#{1,6}\s+/.test(trimmed) ||
    /^[-*]\s+/.test(trimmed) ||
    /^\d+[.)]\s+/.test(trimmed)
  );
}

function renderMarkdownBlocks(content: string): ReactNode[] {
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

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4) as 1 | 2 | 3 | 4;
      const Tag = `h${level}` as keyof JSX.IntrinsicElements;
      blocks.push(<Tag key={key}>{renderInlineMarkdown(heading[2], key)}</Tag>);
      index += 1;
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
    while (index < lines.length && lines[index].trim() && !isBlockBoundary(lines[index]) && !isMarkdownTableStart(lines, index)) {
      paragraphLines.push(lines[index]);
      index += 1;
    }
    blocks.push(<p key={key}>{renderInlineLines(paragraphLines, key)}</p>);
    blockIndex += 1;
  }

  return blocks;
}

function MarkdownMessage({ content }: { content: string }) {
  return <div className="assistant-answer markdown-message">{renderMarkdownBlocks(content)}</div>;
}

function CotTraceIcon({ name, className = '' }: { name: CotTraceIconName; className?: string }) {
  return (
    <span
      className={`cot-trace-icon cot-trace-icon-${name}${className ? ` ${className}` : ''}`}
      aria-hidden="true"
    />
  );
}

function traceSummaryIconName(summary: { state: TraceLine['state'] }): CotTraceIconName {
  void summary;
  return 'execute';
}

function traceLineIconName(line: TraceLine): CotTraceIconName {
  const primaryText = line.text || '';
  const text = `${primaryText} ${line.detail || ''}`;
  if (/判断意图|意图识别|识别意图|router|routing/i.test(primaryText)) return 'judge';
  if (/重新分析|重新生成|重新校验|重新尝试|重新规划|反思|重试|修复|repair|retry|review/i.test(primaryText)) return 'loading';
  if (line.kind === 'tool' || /工具|调用/.test(primaryText)) return 'tool';
  if (line.kind === 'code' || line.code || line.output || /生成代码|代码生成|已生成|执行代码|运行代码|Bash|Python|脚本/i.test(primaryText)) return 'generated';
  if (line.kind === 'skill' || /推进|下一步|下一节点|继续|流转|路由|步骤|技能|SOP/i.test(text)) return 'advance';
  if (line.kind === 'knowledge') return 'advance';
  if (line.kind === 'thinking' || /思考|加载|处理中/.test(text)) return 'loading';
  return 'advance';
}

function parseMessageTime(value?: string): number {
  if (!value) return 0;
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const time = Date.parse(normalized);
  return Number.isFinite(time) ? time : 0;
}

function latestUserMessageForTurn(slot: SessionSlot, turnId?: string | null): ChatMessage | undefined {
  const scoped = [...slot.serverMessages, ...slot.realtimeMessages].filter(
    (messageItem) => messageItem.role === 'user' && (!turnId || messageItem.turnId === turnId),
  );
  const candidates = scoped.length
    ? scoped
    : [...slot.serverMessages, ...slot.realtimeMessages].filter((messageItem) => messageItem.role === 'user');
  return candidates.sort((left, right) => parseMessageTime(right.created_at) - parseMessageTime(left.created_at))[0];
}

function timestampAfterMessage(messageItem?: ChatMessage): string {
  const baseTime = messageItem ? parseMessageTime(messageItem.created_at) : 0;
  return new Date((baseTime > 0 ? baseTime : Date.now()) + 1).toISOString();
}

function hasServerMessageForTurn(messageItem: ChatMessage, serverMessages: ChatMessage[]): boolean {
  if (!messageItem.turnId) return false;
  return serverMessages.some(
    (serverMessage) => serverMessage.turnId === messageItem.turnId && serverMessage.role === messageItem.role,
  );
}

function hasAssistantMessageForTurn(slot: SessionSlot, turnId: string): boolean {
  if (!turnId) return false;
  const messages = [...slot.serverMessages, ...slot.realtimeMessages];
  return messages.some((messageItem) => (
    messageItem.role === 'assistant'
    && !messageItem.isStreaming
    && (
      explicitMessageTurnId(messageItem) === turnId
      || messageItem.id === turnId
    )
    && Boolean(normalizeMessageText(messageItem.content))
  ));
}

function upsertStreamingTracePlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const streamId = `__streaming_${sessionId}`;
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

function upsertTraceStatusPlaceholder(slot: SessionSlot, sessionId: string, turnId: string): boolean {
  if (!turnId) return false;
  const traceId = `__trace_${sessionId}_${turnId}`;
  const streamId = `__streaming_${sessionId}`;
  const traceMessage: ChatMessage = {
    id: traceId,
    turnId,
    role: 'assistant',
    content: '',
    created_at: timestampAfterMessage(latestUserMessageForTurn(slot, turnId)),
    isStreaming: false,
  };
  const index = slot.realtimeMessages.findIndex((item) => item.id === traceId);
  if (index >= 0) {
    const current = slot.realtimeMessages[index];
    if (current.turnId === traceMessage.turnId && current.content === traceMessage.content) return false;
    slot.realtimeMessages = [...slot.realtimeMessages];
    slot.realtimeMessages[index] = { ...current, ...traceMessage, created_at: current.created_at || traceMessage.created_at };
    return true;
  }
  const streamingIndex = slot.realtimeMessages.findIndex((item) => item.id === streamId && item.turnId === turnId);
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

function explicitMessageTurnId(messageItem: ChatMessage): string | undefined {
  const camelTurnId = typeof messageItem.turnId === 'string' ? messageItem.turnId.trim() : '';
  if (camelTurnId) return camelTurnId;
  const snakeTurnId = typeof messageItem.turn_id === 'string' ? messageItem.turn_id.trim() : '';
  return snakeTurnId || undefined;
}

function effectiveMessageTurnId(messageItem: ChatMessage): string | undefined {
  return explicitMessageTurnId(messageItem) || (messageItem.role === 'user' ? messageItem.id : undefined);
}

function explicitStreamTurnId(data: Record<string, unknown>, fallbackTurnId: string): string {
  const turnId = typeof data.turn_id === 'string' ? data.turn_id.trim() : '';
  if (turnId) return turnId;
  const userMessageId = typeof data.user_message_id === 'string' ? data.user_message_id.trim() : '';
  if (userMessageId) return userMessageId;
  return fallbackTurnId;
}

function eventTraceTurnId(event: ChatSessionEventRead): string {
  const data = isPlainRecord(event.data) ? event.data : {};
  const explicit = explicitStreamTurnId(data, '');
  if (explicit) return explicit;
  if (event.event === 'user_message_received') {
    return typeof data.message_id === 'string' ? data.message_id.trim() : '';
  }
  return '';
}

function normalizeSessionEventForStream(event: ChatSessionEventRead): ChatStreamEvent {
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

function isTerminalSessionEvent(event: ChatSessionEventRead, isTerminalStreamEvent: (event: ChatSessionEventRead) => boolean): boolean {
  if (event.event === 'assistant_message_created') return true;
  return isTerminalStreamEvent(event);
}

function attachTurnIdsToServerMessages(
  serverMessages: ChatMessage[],
  realtimeMessages: ChatMessage[],
): ChatMessage[] {
  const realtimeTurnIdsByServerId = new Map(
    realtimeMessages
      .filter((item) => item.turnId && item.serverMessageId)
      .map((item) => [item.serverMessageId as string, item.turnId as string]),
  );

  const normalized = serverMessages.map((messageItem) => {
    const turnId = explicitMessageTurnId(messageItem) || realtimeTurnIdsByServerId.get(messageItem.id);
    if (turnId) return { ...messageItem, turnId };
    if (messageItem.role === 'user') return { ...messageItem, turnId: messageItem.id };
    return messageItem;
  });

  const pendingUserTurnIds: string[] = [];
  const assignedAssistantTurnIds = new Set(
    normalized
      .filter((item) => item.role === 'assistant' && explicitMessageTurnId(item))
      .map((item) => explicitMessageTurnId(item) as string),
  );

  return normalized.map((messageItem) => {
    const turnId = explicitMessageTurnId(messageItem);
    if (messageItem.role === 'user') {
      const userTurnId = turnId || messageItem.id;
      pendingUserTurnIds.push(userTurnId);
      return turnId ? messageItem : { ...messageItem, turnId: userTurnId };
    }
    if (turnId || messageItem.role !== 'assistant') return messageItem;
    const nextTurnId = pendingUserTurnIds.find((candidate) => !assignedAssistantTurnIds.has(candidate));
    if (!nextTurnId) return messageItem;
    assignedAssistantTurnIds.add(nextTurnId);
    return { ...messageItem, turnId: nextTurnId };
  });
}

function shouldKeepRealtimeMessage(
  messageItem: ChatMessage,
  serverMessages: ChatMessage[],
  latestServerTime: number,
  activeTurnId?: string | null,
): boolean {
  if (messageItem.isStreaming) {
    return !messageItem.turnId || !activeTurnId || messageItem.turnId === activeTurnId;
  }
  if (hasServerMessageForTurn(messageItem, serverMessages)) return false;
  if (messageItem.serverMessageId && serverMessages.some((serverMessage) => serverMessage.id === messageItem.serverMessageId)) {
    return false;
  }
  if (messageItem.turnId && activeTurnId && messageItem.turnId === activeTurnId) return true;
  if (!latestServerTime) return true;
  return parseMessageTime(messageItem.created_at) > latestServerTime;
}

function computeMergedMessages(slot: SessionSlot, activeTurnId?: string | null): ChatMessage[] {
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
  const turnStarts = new Map<string, number>();
  combined.forEach(({ messageItem }) => {
    if (messageItem.role !== 'user') return;
    const turnId = effectiveMessageTurnId(messageItem);
    if (!turnId) return;
    const createdAt = parseMessageTime(messageItem.created_at);
    const previous = turnStarts.get(turnId);
    if (previous === undefined || createdAt < previous) {
      turnStarts.set(turnId, createdAt);
    }
  });
  combined.forEach(({ messageItem }) => {
    const turnId = effectiveMessageTurnId(messageItem);
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
      const leftTurnId = effectiveMessageTurnId(left.messageItem);
      const rightTurnId = effectiveMessageTurnId(right.messageItem);
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
    const turnId = effectiveMessageTurnId(entry.messageItem);
    if (!turnId) return;
    const previous = selectedAssistantByTurn.get(turnId);
    if (!previous || assistantRank(entry) >= assistantRank(previous)) {
      selectedAssistantByTurn.set(turnId, entry);
    }
  });

  return sorted
    .filter((entry) => {
      if (entry.messageItem.role !== 'assistant') return true;
      const turnId = effectiveMessageTurnId(entry.messageItem);
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

type RecoverableTraceProgress = {
  id?: string;
  kind?: string;
  text?: string;
  detail?: string | null;
  code?: string | null;
  output?: string | null;
  state?: string;
};

function isBareInitialRoutingLine(line: RecoverableTraceProgress): boolean {
  const id = String(line.id || '');
  const text = String(line.text || '').trim();
  return (
    (id === 'decision_router' || /router|routing/i.test(id))
    && /^(判断意图|正在判断用户意图|意图识别|识别意图)$/.test(text)
    && !line.detail
    && !line.code
    && !line.output
    && (!line.state || line.state === 'running')
  );
}

function hasRecoverableTraceProgress(lines: RecoverableTraceProgress[]): boolean {
  return lines.some((line) => {
    if (!line) return false;
    if (isBareInitialRoutingLine(line)) return false;
    if (line.detail || line.code || line.output) return true;
    if (line.state === 'completed' || line.state === 'failed') return true;
    if (line.kind && line.kind !== 'decision') return true;
    const text = String(line.text || '').trim();
    if (!text) return false;
    return !/^(判断意图|正在判断用户意图|意图识别|识别意图|执行记录|正在思考|已收到消息)$/.test(text);
  });
}

function hasRecoverableEventProgress(events: ChatSessionEventRead[]): boolean {
  return events.some((event) => {
    const data = isPlainRecord(event.data) ? event.data : {};
    if (event.event === 'user_message_received' || event.event === 'memory_recalled') return false;
    if (event.event === 'stream_status') {
      const phase = typeof data.phase === 'string' ? data.phase : '';
      return phase !== 'received' && phase !== 'routing';
    }
    if (event.event === 'status') {
      const phase = typeof data.phase === 'string' ? data.phase : '';
      return phase !== 'received' && phase !== 'routing';
    }
    if (event.event === 'router_decision_created') {
      const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
      const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
      const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
      return Boolean(intent || reason || decision);
    }
    return true;
  });
}

function isRecoverableRunningTrace(row: TurnTraceRead): boolean {
  if (row.completed_at) return false;
  if (!hasRecoverableTraceProgress(row.lines || [])) return false;
  const startedAt = parseMessageTime(row.started_at);
  if (startedAt <= 0) return false;
  return Date.now() - startedAt <= CHAT_TRACE_RECOVERY_WINDOW_MS;
}

const KNOWLEDGE_TRACE_PHASES = new Set([
  'knowledge',
  'okf_route',
  'okf_only',
  'document_route',
  'document_route_fallback',
  'bucket_route',
  'bucket_route_fallback',
  'section_expand',
  'read_chunks',
  'evidence_pack',
  'no_visible_knowledge',
  'no_documents',
  'no_buckets',
]);

function isKnowledgeTracePhase(phase: string): boolean {
  return KNOWLEDGE_TRACE_PHASES.has(phase);
}

function knowledgeTraceText(data: Record<string, unknown>): string {
  const raw = typeof data.message === 'string'
    ? data.message
    : typeof data.text === 'string'
      ? data.text
      : '';
  if (!raw) return '检索知识库';
  return raw.replace(/知识/g, '知识库');
}

function knowledgeTraceDetail(data: Record<string, unknown>): string | undefined {
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

function knowledgeResultTraceDetail(data: Record<string, unknown>): string | undefined {
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

function normalizeTraceSkill(value: unknown): TraceSkill | null {
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

function streamSkillLabel(data: Record<string, unknown>, skill: TraceSkill): string {
  if (skill.state === 'suspended') return '挂起技能';
  const decision = typeof data.runtimeDecision === 'string' ? data.runtimeDecision : '';
  const fromSkillId = typeof data.fromSkillId === 'string' ? data.fromSkillId : '';
  const toSkillId = typeof data.toSkillId === 'string' ? data.toSkillId : '';
  if (decision === 'start_skill') return '选择技能';
  if (decision === 'suspend_current_and_start_new_skill') return '切换技能';
  if (
    (decision === 'answer_related_question_then_resume' || decision === 'answer_chitchat_then_resume')
    && fromSkillId
    && toSkillId
    && fromSkillId !== toSkillId
  ) return '切换技能';
  if (decision === 'exit_current_skill') return '恢复技能';
  return '推进技能';
}

function normalizeTraceTool(value: unknown): TraceTool | null {
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

function toolTraceDetail(tool: TraceTool): string | undefined {
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

function reflectionTraceDetail(data: Record<string, unknown>): string | undefined {
  const parts = [
    typeof data.reason === 'string' ? data.reason : '',
    typeof data.target_tool_name === 'string' ? `工具 ${data.target_tool_name}` : '',
    typeof data.target_skill_id === 'string' ? `技能 ${data.target_skill_id}` : '',
    typeof data.target_step_id === 'string' ? `步骤 ${data.target_step_id}` : '',
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

function routerDecisionTraceLine(data: Record<string, unknown>): TraceLine {
  const intent = typeof data.user_intent === 'string' ? data.user_intent.trim() : '';
  const decision = typeof data.decision === 'string' ? data.decision.trim() : '';
  const skillId = typeof data.target_skill_id === 'string' ? data.target_skill_id.trim() : '';
  const stepId = typeof data.target_step_id === 'string' ? data.target_step_id.trim() : '';
  const reason = typeof data.reason === 'string' ? data.reason.trim() : '';
  const detail = [reason, skillId ? `目标技能 ${skillId}` : '', stepId ? `目标节点 ${stepId}` : '']
    .filter(Boolean)
    .join(' · ');
  return {
    id: 'decision_router',
    kind: 'decision',
    text: intent ? `判断意图 ${intent}` : decision ? `判断意图 ${decision}` : '判断意图',
    detail: detail || undefined,
    state: 'completed',
  };
}

function stepResultTraceLine(data: Record<string, unknown>): TraceLine {
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
    };
  }
  if (knowledgeQueryText) {
    return {
      id: 'decision_step_knowledge',
      kind: 'decision',
      text: '决定查询知识库',
      detail: detail || undefined,
      state: 'running',
    };
  }
  return {
    id: 'decision_step_result',
    kind: 'decision',
    text: nextStepId ? '决定下一步' : '完成步骤判断',
    detail: detail || undefined,
    state: 'completed',
  };
}

function mergeTraceLine(existing: TraceLine, incoming: TraceLine): TraceLine {
  const keepExistingText =
    incoming.id === 'decision_router' &&
    incoming.text === '判断意图' &&
    existing.text !== '判断意图';
  const nextState =
    existing.state !== 'running' && incoming.state === 'running'
      ? existing.state
      : incoming.state;
  return {
    ...existing,
    ...incoming,
    text: keepExistingText ? existing.text : incoming.text || existing.text,
    detail: incoming.detail ?? existing.detail,
    code: incoming.code ?? existing.code,
    language: incoming.language ?? existing.language,
    output: incoming.output ?? existing.output,
    outputLanguage: incoming.outputLanguage ?? existing.outputLanguage,
    outputTitle: incoming.outputTitle ?? existing.outputTitle,
    state: nextState,
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

function isPlainRecord(value: unknown): value is Record<string, unknown> {
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

function generalSkillTraceDetail(data: Record<string, unknown>, phase: string): string | undefined {
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

function generalSkillTraceOutput(data: Record<string, unknown>, phase: string, accumulatedText?: string): {
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

function traceLineAllowed(line: TraceLine, config: UIConfigRead): boolean {
  if (line.kind === 'thinking' || line.kind === 'decision') return config.show_thinking_trace;
  if (line.kind === 'code') return config.show_thinking_trace;
  if (line.kind === 'skill') return config.show_skill_trace;
  if (line.kind === 'tool') return config.show_tool_trace;
  return true;
}

function traceSummary(trace: TurnTrace, lines: TraceLine[]): { text: string; state: TraceLine['state'] } {
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

function traceDetails(lines: TraceLine[]): TraceLine[] {
  const hiddenPlaceholders = new Set([
    '正在思考',
    '已完成思考',
    '正在执行',
    '执行记录',
    '生成回复',
    '正在生成回复',
    '正在根据运行结果生成回复',
  ]);
  const details = lines.filter((line) => {
    if (line.kind === 'thinking') return false;
    if (/生成回复|组织回复|根据运行结果生成回复/.test(line.text) && !line.code && !line.output) return false;
    if (hiddenPlaceholders.has(line.text) && !line.detail && !line.code && !line.output) return false;
    return true;
  });
  return details.length > 0
    ? details
    : lines.filter((line) => !hiddenPlaceholders.has(line.text) || Boolean(line.detail || line.code || line.output));
}

function canRateMessage(item: ChatMessage): boolean {
  return (
    item.role === 'assistant'
    && !item.isStreaming
    && !item.isError
    && !item.id.startsWith('__')
    && !item.id.startsWith('text_')
    && !item.id.startsWith('error_')
  );
}

function stripTrailingCitationSummary(content: string): string {
  if (!content) return content;
  return content
    .replace(/(?:\n|\s){0,3}(?:参考资料|引用来源|资料来源)\s*[:：]\s*(?:\[\d+\]\s*)+$/u, '')
    .trimEnd();
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

function knowledgeCitations(item: ChatMessage, content: string): KnowledgeCitation[] {
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
    )
      .replace(/\/\s*evidence\s*\d+/i, '')
      .split(/在第\s*\d+\s*章第\s*\d+\s*节/)[0]
      .split('。')[0];
    const key = normalizeMessageText(identity).toLowerCase();
    if (!key || seen.has(key)) return;
    seen.add(key);
    result.push({ ...citation, label: `[${labelNumber}]` });
  });
  return result.slice(0, 4).sort((a, b) => citationLabelNumber(a, 0) - citationLabelNumber(b, 0));
}

function scheduledDraftForMessage(item: ChatMessage): ScheduledTaskDraftRead | null {
  const draft = item.metadata?.scheduled_task_draft;
  if (!isPlainRecord(draft) || draft.should_create === false) return null;
  if (typeof draft.title !== 'string' || typeof draft.prompt !== 'string' || typeof draft.agent_id !== 'string') {
    return null;
  }
  return draft as unknown as ScheduledTaskDraftRead;
}

function createdScheduledTaskForMessage(item: ChatMessage): ScheduledTaskRead | undefined {
  const task = item.metadata?.scheduled_task_created;
  if (!isPlainRecord(task)) return undefined;
  if (typeof task.id !== 'string' || typeof task.title !== 'string' || typeof task.prompt !== 'string') {
    return undefined;
  }
  return task as unknown as ScheduledTaskRead;
}

function isScheduledTaskPrompt(item: ChatMessage): boolean {
  return item.role === 'user' && item.metadata?.interaction_mode === 'scheduled_task';
}

function ScheduledDraftCard({
  draft,
  createdTask,
  onConfirm,
  onDismiss,
}: {
  draft: ScheduledTaskDraftRead;
  createdTask?: ScheduledTaskRead;
  onConfirm: (draft: ScheduledTaskDraftRead) => void;
  onDismiss: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [editableDraft, setEditableDraft] = useState<ScheduledTaskDraftRead>(draft);
  const created = Boolean(createdTask);
  const displayDraft = createdTask
    ? ({
      ...draft,
      title: createdTask.title,
      prompt: createdTask.prompt,
      description: createdTask.description || draft.description,
      schedule_type: createdTask.schedule_type,
      schedule: createdTask.schedule,
      timezone: createdTask.timezone,
      rrule: createdTask.rrule || draft.rrule,
    } as ScheduledTaskDraftRead)
    : editableDraft;

  useEffect(() => {
    setEditableDraft(draft);
    setEditing(false);
  }, [
    draft.agent_id,
    draft.title,
    draft.prompt,
    draft.description,
    draft.schedule_type,
    draft.timezone,
    draft.rrule,
    JSON.stringify(draft.schedule || {}),
    createdTask?.id,
  ]);

  const updateDraft = (patch: Partial<ScheduledTaskDraftRead>) => {
    setEditableDraft((current) => ({ ...current, ...patch }));
  };
  const scheduleValue = scheduleEditValue(editableDraft);
  const validateDraft = (nextDraft: ScheduledTaskDraftRead) => {
    if (!nextDraft.title.trim()) {
      message.warning('请输入定时任务名称');
      return false;
    }
    if (!nextDraft.prompt.trim()) {
      message.warning('请输入执行内容');
      return false;
    }
    if (!scheduleEditValue(nextDraft).trim()) {
      message.warning('请输入执行计划');
      return false;
    }
    return true;
  };
  const updateScheduleType = (value: ScheduledTaskDraftRead['schedule_type']) => {
    setEditableDraft((current) => {
      const scheduleType = normalizeDraftScheduleType(value);
      const schedule = draftScheduleForType(current.schedule || {}, scheduleType);
      return { ...current, schedule_type: scheduleType, schedule };
    });
  };
  const updateScheduleValue = (value: string) => {
    setEditableDraft((current) => ({ ...current, schedule: scheduleFromEditValue(current, value) }));
  };
  const completeEdit = () => {
    if (!validateDraft(editableDraft)) return;
    setEditing(false);
  };
  const confirmDraft = () => {
    if (created) return;
    if (!validateDraft(editableDraft)) return;
    onConfirm(editableDraft);
  };

  return (
    <div className={`scheduled-draft-card ${editing ? 'editing' : ''}${created ? ' created' : ''}`}>
      <div className="scheduled-draft-header">
        <div className="scheduled-draft-identity">
          <div className="scheduled-draft-icon">{created ? <StaffdeckIcon name="check" /> : <StaffdeckIcon name="clock" />}</div>
          <div className="scheduled-draft-title-block">
            <div className="scheduled-draft-kicker">{created ? '定时任务已创建' : '定时任务草案'}</div>
            {editing ? (
              <Input
                size="small"
                className="scheduled-draft-title-input"
                value={editableDraft.title}
                onChange={(event) => updateDraft({ title: event.target.value })}
              />
            ) : (
              <strong>{displayDraft.title}</strong>
            )}
          </div>
        </div>
        <div className="scheduled-draft-top-actions">
          {created ? (
            <span className="scheduled-draft-created-badge">
              <StaffdeckIcon name="check" />
              已创建
            </span>
          ) : editing ? (
            <>
              <Button size="small" type="primary" onClick={completeEdit}>完成</Button>
              <Button size="small" type="text" onClick={() => { setEditableDraft(draft); setEditing(false); }}>取消</Button>
            </>
          ) : (
            <>
              <Button size="small" type="text" icon={<StaffdeckIcon name="edit" />} onClick={() => setEditing(true)}>编辑</Button>
              <Button size="small" type="text" onClick={onDismiss}>忽略</Button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <div className="scheduled-draft-editor">
          <label>
            <span>计划类型</span>
            <Select
              size="small"
              value={editableDraft.schedule_type}
              onChange={updateScheduleType}
              options={[
                { value: 'once', label: '一次性' },
                { value: 'daily', label: '每天' },
                { value: 'weekly', label: '每周' },
                { value: 'monthly', label: '每月' },
              ]}
            />
          </label>
          <label>
            <span>执行计划</span>
            <Input
              size="small"
              value={scheduleValue}
              placeholder={editableDraft.schedule_type === 'once' ? 'YYYY-MM-DDTHH:mm:ss+08:00' : 'HH:mm'}
              onChange={(event) => updateScheduleValue(event.target.value)}
            />
          </label>
          <label>
            <span>时区</span>
            <Input
              size="small"
              value={editableDraft.timezone || 'Asia/Shanghai'}
              onChange={(event) => updateDraft({ timezone: event.target.value })}
            />
          </label>
          <label className="scheduled-draft-editor-full">
            <span>执行内容</span>
            <Input.TextArea
              autoSize={{ minRows: 3, maxRows: 6 }}
              value={editableDraft.prompt}
              onChange={(event) => updateDraft({ prompt: event.target.value })}
            />
          </label>
          <label className="scheduled-draft-editor-full">
            <span>说明</span>
            <Input.TextArea
              autoSize={{ minRows: 2, maxRows: 4 }}
              value={editableDraft.description || ''}
              placeholder="可补充任务目的、范围或结果要求"
              onChange={(event) => updateDraft({ description: event.target.value })}
            />
          </label>
        </div>
      ) : (
        <div className="scheduled-draft-body">
          <div className="scheduled-draft-meta-grid">
            <div className="scheduled-draft-meta-item">
              <span>计划</span>
              <strong>{formatDraftSchedule(displayDraft)}</strong>
            </div>
            <div className="scheduled-draft-meta-item">
              <span>类型</span>
              <strong>{scheduleTypeLabel(displayDraft.schedule_type)}</strong>
            </div>
            <div className="scheduled-draft-meta-item">
              <span>时区</span>
              <strong>{displayDraft.timezone || 'Asia/Shanghai'}</strong>
            </div>
          </div>
          <div className="scheduled-draft-prompt">
            <span>执行内容</span>
            <p>{displayDraft.prompt}</p>
          </div>
          {displayDraft.description && (
            <div className="scheduled-draft-description">
              <span>说明</span>
              <p>{displayDraft.description}</p>
            </div>
          )}
        </div>
      )}
      {!created && (
        <div className="scheduled-draft-footer">
          {editing && <Button size="small" type="text" onClick={onDismiss}>忽略</Button>}
          <Button size="small" type="primary" onClick={confirmDraft}>确认创建</Button>
        </div>
      )}
    </div>
  );
}

function citationKindLabel(citation: KnowledgeCitation): string {
  if (citation.kind === 'concept') return '知识图谱';
  if (citation.kind === 'okf') return '知识图谱引用';
  return '引用来源';
}

function citationDisplayTitle(citation: KnowledgeCitation): string {
  const raw = citation.title || citation.section_path || citation.source_path || citation.concept_id || '知识引用';
  const title = raw
    .replace(/\/\s*evidence\s*\d+/i, '')
    .split('用于统一')[0]
    .split('。服务人员')[0]
    .trim();
  return title || raw;
}

function citationSourceLabel(citation: KnowledgeCitation): string {
  const raw = citation.source_path || '';
  if (!raw) return '';
  return raw.replace(/\/\s*evidence\s*\d+/i, '').split(' / ')[0].trim() || raw;
}

function citationSectionLabel(citation: KnowledgeCitation): string {
  const raw = citation.section_path || citation.title || '';
  if (!raw) return '';
  return citationDisplayTitle({ ...citation, title: raw });
}

const MAX_PASTED_REMOTE_IMAGES = 6;

type ClipboardImageItem = {
  types: readonly string[];
  getType: (type: string) => Promise<Blob>;
};

function clipboardContainsComposerImage(clipboardData: DataTransfer): boolean {
  if (Array.from(clipboardData.files || []).some((file) => file.type.startsWith('image/'))) {
    return true;
  }
  if (Array.from(clipboardData.items || []).some((item) => item.kind === 'file' && item.type.startsWith('image/'))) {
    return true;
  }
  return extractImageSourceUrls(clipboardData.getData('text/html')).length > 0
    || extractImageSourceUrls(clipboardData.getData('text/plain')).length > 0;
}

async function extractPastedComposerFiles(clipboardData: DataTransfer): Promise<File[]> {
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

export default function ChatWindowPage() {
  const { sessionId, draftAgentId } = useParams<{ sessionId?: string; draftAgentId?: string }>();
  const navigate = useNavigate();
  const [auth] = useState(() => getAuthSession());
  const tenantId = auth?.user.tenant_id || TENANT_ID;
  const userId = auth?.user.id || '';
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionReadTimes, setSessionReadTimes] = useState<Record<string, string>>(() => loadSessionReadTimes(userId));
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem('skill_agent_selected_agent') || '');
  const [sessionAgentFilter, setSessionAgentFilter] = useState('all');
  const [modelConfigs, setModelConfigs] = useState<ModelConfigRead[]>([]);
  const [selectedModelConfigId, setSelectedModelConfigId] = useState(
    () => window.localStorage.getItem(modelStorageKey(tenantId)) || '',
  );
  const [input, setInput] = useState('');
  const [composerAttachments, setComposerAttachments] = useState<ComposerAttachment[]>([]);
  const [composerDragActive, setComposerDragActive] = useState(false);
  const [composerPlusOpen, setComposerPlusOpen] = useState(false);
  const [composerIntent, setComposerIntent] = useState<Exclude<ComposerInteractionMode, 'normal'> | null>(null);
  const [lastTurn, setLastTurn] = useState<ChatTurnResponse | null>(null);
  const [renameSession, setRenameSession] = useState<ChatSession | null>(null);
  const [renameTitle, setRenameTitle] = useState('');
  const [storeTick, setStoreTick] = useState(0);
  const [streamTick, setStreamTick] = useState(0);
  const [traceTick, setTraceTick] = useState(0);
  const [feedbackTick, setFeedbackTick] = useState(0);
  const [expandedTraceIds, setExpandedTraceIds] = useState<string[]>([]);
  const [collapsedTraceIds, setCollapsedTraceIds] = useState<string[]>([]);
  const [scheduledDrafts, setScheduledDrafts] = useState<Record<string, ScheduledTaskDraftRead>>({});
  const [createdScheduledTasks, setCreatedScheduledTasks] = useState<Record<string, ScheduledTaskRead>>({});
  const [dismissedDraftMessageIds, setDismissedDraftMessageIds] = useState<string[]>([]);
  const [activeCitation, setActiveCitation] = useState<KnowledgeCitation | null>(null);
  const [handoffs, setHandoffs] = useState<HumanHandoffRead[]>([]);
  const [handoffsLoading, setHandoffsLoading] = useState(false);
  const [showHandoffInbox, setShowHandoffInbox] = useState(false);
  const [handoffReplies, setHandoffReplies] = useState<Record<string, string>>({});
  const [isComposing, setIsComposing] = useState(false);
  const [runningTurn, setRunningTurn] = useState<{ sessionId: string; turnId: string } | null>(null);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => (
    window.localStorage.getItem('skill_agent_sidebar_collapsed') === 'true'
  ));
  const [uiConfig, setUiConfig] = useState<UIConfigRead>({
    tenant_id: tenantId,
    show_thinking_trace: true,
    show_skill_trace: true,
    show_tool_trace: true,
    reflection_max_rounds: 1,
    agent_loop_max_actions: 6,
    updated_at: '',
  });
  const chatMessagesRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const isChatProgrammaticScrollRef = useRef(false);
  const isChatStickyToBottomRef = useRef(true);
  const lastActiveConversationIdRef = useRef<string | null>(null);
  const storeRef = useRef(new Map<string, SessionSlot>());
  const streamRef = useRef(new Map<string, StreamSlot>());
  const turnTraceRef = useRef(new Map<string, TurnTrace>());
  const locallyCancelledSessionIdsRef = useRef(new Set<string>());
  const scheduledEventIdsRef = useRef(new Set<string>());
  const knownSessionIdsRef = useRef(new Set<string>());
  const optimisticSessionIdsRef = useRef(new Set<string>());
  const sessionsInitializedRef = useRef(false);
  const autoOpenedSessionIdsRef = useRef(new Set<string>());
  const loadErrorNoticeRef = useRef<Record<string, number>>({});
  const uploadControllersRef = useRef(new Map<string, AbortController>());

  const notifyStore = useCallback(() => setStoreTick((value) => value + 1), []);
  const notifyStream = useCallback(() => setStreamTick((value) => value + 1), []);
  const notifyTrace = useCallback(() => setTraceTick((value) => value + 1), []);
  const notifyFeedback = useCallback(() => setFeedbackTick((value) => value + 1), []);
  useEffect(() => () => {
    uploadControllersRef.current.forEach((controller) => controller.abort());
    uploadControllersRef.current.clear();
  }, []);

  const updateChatStickiness = useCallback(() => {
    const element = chatMessagesRef.current;
    if (!element) return;
    const remainingScroll = element.scrollHeight - element.clientHeight - element.scrollTop;
    isChatStickyToBottomRef.current = remainingScroll <= 96;
  }, []);

  const finishProgrammaticChatScroll = useCallback(() => {
    window.requestAnimationFrame(() => {
      updateChatStickiness();
      isChatProgrammaticScrollRef.current = false;
    });
  }, [updateChatStickiness]);

  const handleChatMessagesScroll = useCallback(() => {
    if (isChatProgrammaticScrollRef.current) return;
    updateChatStickiness();
  }, [updateChatStickiness]);

  const notifyRequestError = useCallback((scope: string, error: unknown, fallback: string) => {
    if (isAuthError(error)) {
      clearAuthSession();
      navigate('/login', { replace: true });
      return true;
    }
    const rawMessage = error instanceof Error ? error.message : fallback;
    const isNetworkError = rawMessage === 'Failed to fetch' || rawMessage.includes('NetworkError');
    const noticeKey = isNetworkError ? 'chat-network-error' : `chat-${scope}-error`;
    const now = Date.now();
    const lastShownAt = loadErrorNoticeRef.current[noticeKey] || 0;
    if (now - lastShownAt < 12000) return false;
    loadErrorNoticeRef.current[noticeKey] = now;
    message.open({
      type: 'error',
      key: noticeKey,
      content: isNetworkError ? '接口连接失败，请检查本地服务或稍后重试' : (rawMessage || fallback),
      duration: 3,
    });
    return false;
  }, [navigate]);
  const scrollChatToBottom = useCallback((options?: { preserveShortContentTop?: boolean; force?: boolean }) => {
    const element = chatMessagesRef.current;
    if (!element) return;
    if (!options?.force && !isChatStickyToBottomRef.current) return;
    const targetScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
    const shortContentGuard = Math.min(520, element.clientHeight * 0.72);
    isChatProgrammaticScrollRef.current = true;
    if (options?.preserveShortContentTop && targetScrollTop <= shortContentGuard) {
      element.scrollTop = 0;
      finishProgrammaticChatScroll();
      return;
    }
    window.requestAnimationFrame(() => {
      element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
      window.requestAnimationFrame(() => {
        element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
        finishProgrammaticChatScroll();
      });
    });
  }, [finishProgrammaticChatScroll]);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem('skill_agent_sidebar_collapsed', String(next));
      return next;
    });
  }, []);

  const markSessionRead = useCallback((id: string, timestamp?: string) => {
    if (!id) return;
    const value = timestamp || new Date().toISOString();
    setSessionReadTimes((current) => {
      const currentTime = Date.parse(current[id] || '');
      const nextTime = Date.parse(value);
      if (Number.isFinite(currentTime) && Number.isFinite(nextTime) && currentTime >= nextTime) {
        return current;
      }
      const next = { ...current, [id]: value };
      persistSessionReadTimes(userId, next);
      return next;
    });
  }, [userId]);

  const activeDraftAgentId = draftAgentId || '';
  const activeConversationId = sessionId || (activeDraftAgentId ? draftConversationKey(activeDraftAgentId) : '');
  const isDraftConversation = Boolean(activeDraftAgentId && !sessionId);
  const currentSession = sessionId ? sessions.find((item) => item.id === sessionId) || null : null;
  const availableAgents = visibleChatEmployees(agents, auth?.user);
  const defaultAgent = availableAgents.find((agent) => agent.id === selectedAgentId) || availableAgents[0] || null;
  const draftAgent = activeDraftAgentId
    ? agents.find((agent) => agent.id === activeDraftAgentId) || null
    : null;
  const sessionAgent = currentSession?.agent_id
    ? agents.find((agent) => agent.id === currentSession.agent_id) || null
    : null;
  const displayedAgent = sessionAgent || draftAgent || defaultAgent;
  const displayedProfile = displayedAgent ? employeeProfile(displayedAgent) : null;
  const emptyProfileTags = displayedProfile?.workStyles.length
    ? displayedProfile.workStyles.slice(0, 3)
    : ['结构化整理', '可追溯', '可追溯'];
  const emptyRoleSummary = displayedProfile
    ? `#角色：${displayedProfile.roleName}「${displayedAgent ? employeeDisplayName(displayedAgent) : '小知'}」一名经验丰富的${displayedProfile.roleName}，`
    : '#角色：知识运营官「小知」一名经验丰富的知识运营官，';
  const emptyStats = displayedAgent
    ? [
      { label: '资料', value: agentResourceCount(displayedAgent, 'knowledge_base') },
      { label: '技能', value: agentResourceCount(displayedAgent, 'general_skill') },
      { label: 'SOP', value: agentResourceCount(displayedAgent, 'skill') },
    ]
    : [
      { label: '资料', value: 0 },
      { label: '技能', value: 0 },
      { label: 'SOP', value: 0 },
    ];
  const sessionFilterOptions = useMemo(() => {
    const counts = new Map<string, number>();
    sessions.forEach((session) => {
      if (!session.agent_id) return;
      counts.set(session.agent_id, (counts.get(session.agent_id) || 0) + 1);
    });
    const rows = Array.from(counts.keys())
      .map((agentId) => availableAgents.find((agent) => agent.id === agentId))
      .filter((agent): agent is AgentProfileRead => Boolean(agent))
      .sort((a, b) => employeeDisplayName(a).localeCompare(employeeDisplayName(b), 'zh-Hans-CN'));
    return [
      { value: 'all', label: `全部会话 · ${sessions.length}` },
      ...rows.map((agent) => ({
        value: agent.id,
        label: `${employeeDisplayName(agent)} · ${counts.get(agent.id) || 0}`,
      })),
    ];
  }, [availableAgents, sessions]);
  const visibleSidebarSessions = useMemo(() => (
    sessionAgentFilter === 'all'
      ? sessions
      : sessions.filter((session) => session.agent_id === sessionAgentFilter)
  ), [sessionAgentFilter, sessions]);
  const enabledModelConfigs = useMemo(() => modelConfigs.filter((item) => item.enabled), [modelConfigs]);
  const selectedModelConfig = (
    enabledModelConfigs.find((item) => item.id === selectedModelConfigId)
    || enabledModelConfigs.find((item) => item.is_default)
    || enabledModelConfigs[0]
    || null
  );

  const changeModelConfig = useCallback((value: string) => {
    setSelectedModelConfigId(value);
    if (value) {
      window.localStorage.setItem(modelStorageKey(tenantId), value);
    } else {
      window.localStorage.removeItem(modelStorageKey(tenantId));
    }
  }, [tenantId]);

  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/chat/agents?tenant_id=${tenantId}`)
          .then((rows) => {
        setAgents(rows);
        setSelectedAgentId((current) => {
          const employeeRows = visibleChatEmployees(rows, auth?.user);
          if (current && employeeRows.some((item) => item.id === current)) return current;
          const next = employeeRows[0]?.id || '';
          if (next) window.localStorage.setItem('skill_agent_selected_agent', next);
          return next;
        });
      })
      .catch(() => setAgents([]));
  }, [auth?.user, tenantId]);

  useEffect(() => {
    if (!activeDraftAgentId) return;
    setSelectedAgentId(activeDraftAgentId);
    window.localStorage.setItem('skill_agent_selected_agent', activeDraftAgentId);
  }, [activeDraftAgentId]);

  useEffect(() => {
    if (!auth) return;
    api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${tenantId}`)
      .then((rows) => {
        setModelConfigs(rows);
        setSelectedModelConfigId((current) => {
          const enabledRows = rows.filter((item) => item.enabled);
          const stored = window.localStorage.getItem(modelStorageKey(tenantId)) || '';
          if (current && enabledRows.some((item) => item.id === current)) return current;
          if (stored && enabledRows.some((item) => item.id === stored)) return stored;
          const next = enabledRows.find((item) => item.is_default)?.id || enabledRows[0]?.id || '';
          if (next) {
            window.localStorage.setItem(modelStorageKey(tenantId), next);
          }
          return next;
        });
      })
      .catch((error) => {
        if (isAuthError(error)) {
          clearAuthSession();
          navigate('/login', { replace: true });
          return;
        }
        setModelConfigs([]);
      });
  }, [auth, navigate, tenantId]);
  const toggleTrace = useCallback((turnId: string, isExpanded = false) => {
    if (isExpanded) {
      setCollapsedTraceIds((current) => (
        current.includes(turnId)
          ? current
          : [...current, turnId]
      ));
      setExpandedTraceIds((current) => current.filter((item) => item !== turnId));
      return;
    }
    setCollapsedTraceIds((current) => current.filter((item) => item !== turnId));
    setExpandedTraceIds((current) => (
      current.includes(turnId)
        ? current.filter((item) => item !== turnId)
        : [...current, turnId]
    ));
  }, []);

  const getSlot = useCallback((id: string): SessionSlot => {
    const store = storeRef.current;
    if (!store.has(id)) {
      store.set(id, createEmptySlot());
    }
    return store.get(id)!;
  }, []);

  const getStreamSlot = useCallback((id: string): StreamSlot => {
    const store = streamRef.current;
    if (!store.has(id)) {
      store.set(id, createStreamSlot());
    }
    return store.get(id)!;
  }, []);

  const getTurnTrace = useCallback((id: string): TurnTrace => {
    const store = turnTraceRef.current;
    if (!store.has(id)) {
      store.set(id, createTurnTrace());
    }
    return store.get(id)!;
  }, []);

  const forgetMissingSession = useCallback((id: string) => {
    knownSessionIdsRef.current.delete(id);
    optimisticSessionIdsRef.current.delete(id);
    storeRef.current.delete(id);
    streamRef.current.delete(id);
    locallyCancelledSessionIdsRef.current.delete(id);
    setSessions((current) => current.filter((item) => item.id !== id));
    setScheduledDrafts((current) => {
      if (!current[id]) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    setCreatedScheduledTasks((current) => {
      const key = `session:${id}`;
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    notifyStore();
    notifyStream();
  }, [notifyStore, notifyStream]);

  const upsertOptimisticSession = useCallback((session: ChatSession) => {
    optimisticSessionIdsRef.current.add(session.id);
    knownSessionIdsRef.current.add(session.id);
    setSessions((current) => {
      const existing = current.find((item) => item.id === session.id);
      const nextSession = existing
        ? { ...existing, ...session, updated_at: session.updated_at || existing.updated_at }
        : session;
      return [nextSession, ...current.filter((item) => item.id !== session.id)];
    });
  }, []);

  const applySessionTitleSummary = useCallback((targetSessionId: string, rawTitle: unknown) => {
    const title = typeof rawTitle === 'string' ? rawTitle.trim() : '';
    if (!targetSessionId || !title) return;
    setSessions((current) => current.map((item) => (
      item.id === targetSessionId ? { ...item, title } : item
    )));
  }, []);

  const upsertTraceLine = useCallback((turnId: string, line: TraceLine) => {
    const trace = getTurnTrace(turnId);
    const nextLine = trace.completedAt && line.state === 'running'
      ? { ...line, state: 'completed' as const }
      : line;
    const index = trace.lines.findIndex((item) => item.id === line.id);
    if (index >= 0) {
      trace.lines = [...trace.lines];
      trace.lines[index] = mergeTraceLine(trace.lines[index], nextLine);
    } else {
      trace.lines = [...trace.lines, nextLine].slice(-80);
    }
    notifyTrace();
  }, [getTurnTrace, notifyTrace]);

  const finishTrace = useCallback((turnId: string, failed = false) => {
    const trace = getTurnTrace(turnId);
    trace.completedAt = Date.now();
    trace.lines = trace.lines.map((line) => ({
      ...line,
      state: failed && line.state === 'running' ? 'failed' : line.state === 'running' ? 'completed' : line.state,
    }));
    notifyTrace();
  }, [getTurnTrace, notifyTrace]);

  const pruneRealtime = useCallback((id: string) => {
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    const latestServerTime = Math.max(0, ...slot.serverMessages.map((item) => parseMessageTime(item.created_at)));
    slot.realtimeMessages = slot.realtimeMessages.filter((item) => {
      if (slot.serverMessages.some((serverMessage) => serverMessage.id === item.id)) return false;
      return shouldKeepRealtimeMessage(item, slot.serverMessages, latestServerTime, stream.turnId);
    });
  }, [getSlot, getStreamSlot]);

  const clearStreamSlot = useCallback((id: string, removeStreamingMessage = false) => {
    const stream = getStreamSlot(id);
    if (stream.timer) {
      window.clearTimeout(stream.timer);
      stream.timer = null;
    }
    stream.loading = false;
    stream.phase = '';
    stream.accumulated = '';
    stream.turnId = null;
    stream.abortController = null;
    if (removeStreamingMessage) {
      const slot = getSlot(id);
      const streamId = `__streaming_${id}`;
      const nextRealtime = slot.realtimeMessages.filter((item) => item.id !== streamId);
      if (nextRealtime.length !== slot.realtimeMessages.length) {
        slot.realtimeMessages = nextRealtime;
        notifyStore();
      }
    }
    notifyStream();
  }, [getSlot, getStreamSlot, notifyStore, notifyStream]);

  const rekeyTurnTrace = useCallback((fromTurnId: string, toTurnId: string) => {
    if (!fromTurnId || !toTurnId || fromTurnId === toTurnId) return;
    const source = turnTraceRef.current.get(fromTurnId);
    if (!source) return;
    const target = turnTraceRef.current.get(toTurnId);
    if (!target) {
      turnTraceRef.current.set(toTurnId, source);
    } else {
      const nextLines = [...target.lines];
      source.lines.forEach((line) => {
        const index = nextLines.findIndex((item) => item.id === line.id);
        if (index >= 0) {
          nextLines[index] = mergeTraceLine(nextLines[index], line);
        } else {
          nextLines.push(line);
        }
      });
      target.lines = nextLines.slice(-80);
      target.startedAt = Math.min(target.startedAt, source.startedAt);
      target.completedAt = target.completedAt || source.completedAt;
    }
    turnTraceRef.current.delete(fromTurnId);
    setExpandedTraceIds((current) => {
      if (!current.includes(fromTurnId)) return current;
      const next = current.filter((item) => item !== fromTurnId);
      return next.includes(toTurnId) ? next : [...next, toTurnId];
    });
    setCollapsedTraceIds((current) => (
      current.includes(fromTurnId)
        ? current.map((item) => (item === fromTurnId ? toTurnId : item))
        : current
    ));
    notifyTrace();
  }, [notifyTrace]);

  const bindRealtimeUserToServerMessage = useCallback((id: string, turnId: string, serverMessageId: string) => {
    if (!turnId || !serverMessageId) return;
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    let changed = false;
    slot.realtimeMessages = slot.realtimeMessages.map((item) => {
      if (item.turnId !== turnId) return item;
      changed = true;
      return {
        ...item,
        id: item.role === 'user' ? serverMessageId : item.id,
        serverMessageId: item.role === 'user' ? serverMessageId : item.serverMessageId,
        turnId: serverMessageId,
      };
    });
    slot.serverMessages = slot.serverMessages.map((item) => {
      if (item.id !== serverMessageId) return item;
      changed = true;
      return {
        ...item,
        turnId: serverMessageId,
      };
    });
    if (stream.turnId === turnId) {
      stream.turnId = serverMessageId;
      changed = true;
    }
    if (stream.cancelledTurnId === turnId) {
      stream.cancelledTurnId = serverMessageId;
      changed = true;
    }
    setRunningTurn((current) => (
      current?.sessionId === id && current.turnId === turnId
        ? { sessionId: id, turnId: serverMessageId }
        : current
    ));
    rekeyTurnTrace(turnId, serverMessageId);
    if (changed) notifyStore();
  }, [getSlot, getStreamSlot, notifyStore, rekeyTurnTrace]);

  const displayedMessages = useMemo(() => {
    if (!activeConversationId) return [];
    void storeTick;
    void streamTick;
    void feedbackTick;
    return computeMergedMessages(getSlot(activeConversationId), getStreamSlot(activeConversationId).turnId);
  }, [activeConversationId, feedbackTick, getSlot, getStreamSlot, storeTick, streamTick]);

  const currentStream = useMemo(() => {
    void streamTick;
    return activeConversationId ? getStreamSlot(activeConversationId) : createStreamSlot();
  }, [activeConversationId, getStreamSlot, streamTick]);
  const currentTraceRunning = Boolean(
    currentStream.loading
    || (activeConversationId && runningTurn?.sessionId === activeConversationId),
  );
  const activeRunningTraceId = currentTraceRunning
    ? (currentStream.turnId || (runningTurn?.sessionId === activeConversationId ? runningTurn.turnId : '') || '')
    : '';
  const hasCurrentTurnAssistantMessage = useMemo(() => {
    void traceTick;
    if (!activeRunningTraceId) return false;
    return displayedMessages.some((item) => (
      item.role === 'assistant'
      && (item.turnId || item.id) === activeRunningTraceId
      && (
        Boolean(normalizeMessageText(item.content))
        || Boolean(turnTraceRef.current.get(item.turnId || item.id)?.lines.length)
        || messageAttachments(item).length > 0
        || Boolean(scheduledDraftForMessage(item))
        || Boolean(createdScheduledTaskForMessage(item))
      )
    ));
  }, [activeRunningTraceId, displayedMessages, traceTick]);
  const hasRunningDisplayedTrace = useMemo(() => {
    void traceTick;
    return displayedMessages.some((item) => {
      if (item.role !== 'assistant' || !item.isStreaming) return false;
      const trace = turnTraceRef.current.get(item.turnId || item.id);
      return Boolean(trace?.lines.some((line) => line.state === 'running'));
    });
  }, [displayedMessages, traceTick]);
  const currentSessionRunning = Boolean(
    currentStream.loading
    || (activeConversationId && runningTurn?.sessionId === activeConversationId)
    || (currentStream.loading && hasRunningDisplayedTrace),
  );
  const readyComposerAttachments = useMemo(
    () => composerAttachments.filter((item) => item.uploadStatus === 'ready'),
    [composerAttachments],
  );
  const uploadingComposerAttachment = composerAttachments.some((item) => item.uploadStatus === 'uploading');
  const hasStreamingAssistantMessage = useMemo(() => (
    displayedMessages.some((item) => (
      item.role === 'assistant'
      && item.isStreaming
      && (
        Boolean(normalizeMessageText(item.content))
        || Boolean(turnTraceRef.current.get(item.turnId || item.id)?.lines.length)
        || Boolean(currentStream.turnId && turnTraceRef.current.get(currentStream.turnId)?.lines.length)
      )
    ))
  ), [currentStream.turnId, displayedMessages, traceTick]);
  const hasInlineCurrentTrace = useMemo(() => {
    void traceTick;
    if (!activeRunningTraceId) return false;
    return displayedMessages.some((item) => {
      if (item.role !== 'assistant') return false;
      const traceTurnId = item.isStreaming ? activeRunningTraceId : (item.turnId || item.id);
      if (traceTurnId !== activeRunningTraceId) return false;
      const trace = turnTraceRef.current.get(traceTurnId);
      return Boolean(trace?.lines.length);
    });
  }, [activeRunningTraceId, displayedMessages, traceTick]);
  const composerActive = Boolean(
    input.trim()
    || composerAttachments.length > 0
    || displayedMessages.length > 0
    || currentSessionRunning,
  );
  const showComposerAvatar = Boolean(activeConversationId && displayedProfile);
  const isCurrentStreamingTrace = useCallback((traceTurnId: string, item?: ChatMessage) => Boolean(
    currentTraceRunning
    && traceTurnId
    && (
      traceTurnId === activeRunningTraceId
      || traceTurnId === currentStream.turnId
      || (item?.role === 'assistant' && item.isStreaming)
    )
  ), [activeRunningTraceId, currentStream.turnId, currentTraceRunning]);
  const renderAssistantTrace = (
    traceTurnId: string,
    summary: { text: string; state: TraceLine['state'] },
    details: TraceLine[],
    expanded: boolean,
  ) => (
    <div className="assistant-trace">
      <button
        type="button"
        className={`turn-trace-summary ${summary.state}`}
        onClick={() => toggleTrace(traceTurnId, expanded)}
      >
        <span className="trace-icon-slot"><CotTraceIcon name={traceSummaryIconName(summary)} /></span>
        <span className="trace-primary-text" data-text={summary.text}>{summary.text}</span>
        {details.length > 0 && (
          <span className="trace-chevron-slot">
            <StaffdeckIcon name="arrow" className={`trace-chevron-icon${expanded ? ' is-expanded' : ''}`} size={14} />
          </span>
        )}
      </button>
      {expanded && details.length > 0 && (
        <div className="turn-trace-details">
          {details.map((line) => (
            <div key={line.id} className={`turn-trace-line ${line.kind} ${line.state}`}>
              <span className="trace-icon-slot">
                <CotTraceIcon name={traceLineIconName(line)} />
              </span>
              <span className="turn-trace-content">
                <span className="trace-primary-text" data-text={line.text}>{line.text}</span>
                {line.detail && <span className="turn-trace-detail">{line.detail}</span>}
                {line.code && (
                  <details className="turn-trace-code-wrap" open>
                    <summary>查看代码</summary>
                    <CodeBlock className="turn-trace-code" code={line.code} language={line.language || 'python'} />
                  </details>
                )}
                {line.output && (
                  <details className="turn-trace-code-wrap turn-trace-output-wrap" open>
                    <summary>{line.outputTitle || '查看输出'}</summary>
                    <CodeBlock className="turn-trace-code" code={line.output} language={line.outputLanguage || 'text'} />
                  </details>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
  const fallbackRunningTraceId = activeRunningTraceId;
  const fallbackRunningTrace = fallbackRunningTraceId ? turnTraceRef.current.get(fallbackRunningTraceId) : undefined;
  const fallbackTraceLines = fallbackRunningTrace?.lines || [];
  const fallbackAllowedTrace = fallbackTraceLines.filter((line) => traceLineAllowed(line, uiConfig));
  const fallbackVisibleTrace = fallbackAllowedTrace.length > 0 ? fallbackAllowedTrace : fallbackTraceLines;
  const fallbackTraceSummary = fallbackVisibleTrace.length > 0
    ? traceSummary(fallbackRunningTrace || { lines: fallbackVisibleTrace, startedAt: Date.now() }, fallbackVisibleTrace)
    : null;
  const fallbackTraceDetails = traceDetails(fallbackVisibleTrace);
  const fallbackTraceActive = isCurrentStreamingTrace(fallbackRunningTraceId);
  const fallbackTraceSummaryForRender = fallbackTraceSummary && fallbackTraceActive && !fallbackRunningTrace?.completedAt
    ? { ...fallbackTraceSummary, state: 'running' as const }
    : fallbackTraceSummary;
  const fallbackTraceDefaultExpanded = Boolean(fallbackTraceActive || fallbackTraceSummaryForRender?.state === 'running');
  const fallbackTraceExpanded = Boolean(
    (fallbackRunningTraceId && expandedTraceIds.includes(fallbackRunningTraceId))
    || (fallbackTraceDefaultExpanded && !collapsedTraceIds.includes(fallbackRunningTraceId))
  );
  const showFallbackRunningStatus = Boolean(
    false
    && currentTraceRunning
    && fallbackTraceSummaryForRender
    && !hasCurrentTurnAssistantMessage
    && !hasInlineCurrentTrace
    && !hasStreamingAssistantMessage
  );
  const modelMenuItems = useMemo(() => {
    if (!enabledModelConfigs.length) {
      return [
        {
          key: 'empty',
          disabled: true,
          label: <span className="composer-model-menu-empty">暂无可用模型</span>,
        },
      ];
    }
    return enabledModelConfigs.map((model) => ({
      key: model.id,
      label: (
        <span className="composer-model-menu-item">
          <span className="composer-model-menu-copy">
            <span className="composer-model-menu-name">{modelDisplayName(model)}</span>
            <span className="composer-model-menu-detail">{modelDetailText(model)}</span>
          </span>
          {selectedModelConfig?.id === model.id && <StaffdeckIcon name="check" />}
        </span>
      ),
    }));
  }, [enabledModelConfigs, selectedModelConfig?.id]);
  useEffect(() => {
    if (sessionAgentFilter === 'all') return;
    if (!sessionFilterOptions.some((item) => item.value === sessionAgentFilter)) {
      setSessionAgentFilter('all');
    }
  }, [sessionAgentFilter, sessionFilterOptions]);
  const currentScheduledDraft = activeConversationId ? scheduledDrafts[activeConversationId] : undefined;
  const hasVisibleMessageScheduledDraft = displayedMessages.some((item) => (
    item.role === 'assistant'
    && !dismissedDraftMessageIds.includes(item.id)
    && Boolean(scheduledDraftForMessage(item))
  ));

  const loadSessions = useCallback(() => {
    api
      .get<ChatSession[]>(`/api/chat/sessions?tenant_id=${tenantId}`)
      .then((rows) => {
        const previousIds = new Set(knownSessionIdsRef.current);
        const initialized = sessionsInitializedRef.current;
        if (!initialized) {
          const initialReads = loadSessionReadTimes(userId);
          const nextReads = { ...initialReads };
          if (Object.keys(initialReads).length === 0) {
            rows.forEach((row) => {
              nextReads[row.id] = row.updated_at || new Date().toISOString();
            });
          }
          setSessionReadTimes(nextReads);
          persistSessionReadTimes(userId, nextReads);
          sessionsInitializedRef.current = true;
        }
        rows.forEach((row) => {
          knownSessionIdsRef.current.add(row.id);
          optimisticSessionIdsRef.current.delete(row.id);
        });
        const persistedIds = new Set(rows.map((row) => row.id));
        setSessions((current) => [
          ...current.filter((row) => optimisticSessionIdsRef.current.has(row.id) && !persistedIds.has(row.id)),
          ...rows,
        ]);
        if (!initialized) return;
        const newScheduledSession = rows.find((row) => (
          !previousIds.has(row.id)
          && isScheduledSession(row)
          && !autoOpenedSessionIdsRef.current.has(row.id)
        ));
        if (!newScheduledSession) return;
        autoOpenedSessionIdsRef.current.add(newScheduledSession.id);
        if (!input.trim()) {
          getSlot(newScheduledSession.id);
          navigate(`/${newScheduledSession.id}`);
        }
      })
      .catch((error) => {
        notifyRequestError('sessions', error, '会话加载失败');
      });
  }, [getSlot, input, navigate, notifyRequestError, tenantId, userId]);

  const loadMessages = useCallback((id: string) => {
    return api
      .get<ChatMessage[]>(`/api/chat/sessions/${id}/messages?tenant_id=${tenantId}`)
      .then((rows) => {
        const slot = getSlot(id);
        slot.serverMessages = attachTurnIdsToServerMessages(rows, slot.realtimeMessages);
        const stream = getStreamSlot(id);
        if (stream.loading) {
          const latestUserTime = Math.max(
            0,
            ...slot.serverMessages
              .filter((messageItem) => messageItem.role === 'user')
              .map((messageItem) => parseMessageTime(messageItem.created_at)),
          );
          const hasCompletedAssistant = slot.serverMessages.some((messageItem) => (
            messageItem.role === 'assistant'
            && (
              (stream.turnId && messageItem.turnId === stream.turnId)
              || parseMessageTime(messageItem.created_at) > latestUserTime
            )
          ));
          if (hasCompletedAssistant) {
            clearStreamSlot(id, false);
          }
        }
        pruneRealtime(id);
        notifyStore();
      })
      .catch((error) => {
        if (isMissingChatSessionError(error)) {
          forgetMissingSession(id);
          loadSessions();
          return;
        }
        notifyRequestError('messages', error, '消息加载失败');
      });
  }, [clearStreamSlot, forgetMissingSession, getSlot, getStreamSlot, loadSessions, notifyRequestError, notifyStore, pruneRealtime, tenantId]);

  const loadTraces = useCallback((id: string) => {
    return api
      .get<TurnTraceRead[]>(`/api/chat/sessions/${id}/trace?tenant_id=${tenantId}`)
      .then((rows) => {
        const slot = getSlot(id);
        const stream = getStreamSlot(id);
        const locallyCancelled = locallyCancelledSessionIdsRef.current.has(id);
        let recoveredRunningTurnId = '';
        let storeChanged = false;
        let streamChanged = false;
        rows.forEach((row) => {
          const hasFinalAssistant = hasAssistantMessageForTurn(slot, row.turn_id);
          const recoverableRunningTrace = !locallyCancelled && isRecoverableRunningTrace(row) && !hasFinalAssistant;
          const staleOpenTrace = !row.completed_at && row.lines.length > 0 && !recoverableRunningTrace && !hasFinalAssistant;
          const traceLines = row.lines.map((line) => ({
            id: line.id,
            kind: line.kind,
            text: line.text,
            detail: line.detail || undefined,
            code: line.code || undefined,
            language: line.language || undefined,
            output: line.output || undefined,
            outputLanguage: line.outputLanguage || undefined,
            outputTitle: line.outputTitle || undefined,
            state: staleOpenTrace && line.state === 'running' ? 'completed' as const : line.state,
            collapsible: Boolean(line.collapsible || line.code || line.output),
          }));
          turnTraceRef.current.set(row.turn_id, {
            lines: traceLines,
            startedAt: parseMessageTime(row.started_at) || Date.now(),
            completedAt: row.completed_at
              ? parseMessageTime(row.completed_at)
              : staleOpenTrace
                ? parseMessageTime(row.started_at) || Date.now()
                : undefined,
          });
          if (recoverableRunningTrace) {
            recoveredRunningTurnId = row.turn_id;
          } else if ((row.completed_at || staleOpenTrace) && row.lines.length > 0 && !hasFinalAssistant) {
            storeChanged = upsertTraceStatusPlaceholder(slot, id, row.turn_id) || storeChanged;
            if (staleOpenTrace) {
              setExpandedTraceIds((expanded) => (
                expanded.includes(row.turn_id) ? expanded : [...expanded, row.turn_id]
              ));
              setCollapsedTraceIds((collapsed) => collapsed.filter((item) => item !== row.turn_id));
            }
          }
        });
        if (recoveredRunningTurnId) {
          streamChanged = streamChanged
            || stream.turnId !== recoveredRunningTurnId
            || !stream.loading
            || !stream.phase;
          stream.turnId = recoveredRunningTurnId;
          stream.loading = true;
          stream.phase = stream.phase || '正在思考';
          storeChanged = upsertStreamingTracePlaceholder(slot, id, recoveredRunningTurnId) || storeChanged;
          setExpandedTraceIds((expanded) => (
            expanded.includes(recoveredRunningTurnId) ? expanded : [...expanded, recoveredRunningTurnId]
          ));
          setCollapsedTraceIds((collapsed) => collapsed.filter((item) => item !== recoveredRunningTurnId));
        } else if (stream.turnId && !stream.loading) {
          streamChanged = true;
          stream.turnId = null;
          stream.phase = '';
        }
        setRunningTurn((current) => {
          if (recoveredRunningTurnId) {
            const next = { sessionId: id, turnId: recoveredRunningTurnId };
            return current?.sessionId === next.sessionId && current.turnId === next.turnId ? current : next;
          }
          return current?.sessionId === id ? null : current;
        });
        if (storeChanged) {
          notifyStore();
        }
        if (storeChanged || streamChanged) {
          notifyStream();
        }
        notifyTrace();
      })
      .catch((error) => {
        if (isMissingChatSessionError(error)) {
          forgetMissingSession(id);
          loadSessions();
          return;
        }
        notifyRequestError('trace', error, '轨迹加载失败');
      });
  }, [forgetMissingSession, getSlot, getStreamSlot, loadSessions, notifyRequestError, notifyStore, notifyStream, notifyTrace, tenantId]);

  const loadHandoffs = useCallback(() => {
    if (!auth) return Promise.resolve();
    setHandoffsLoading(true);
    return api
      .get<HumanHandoffRead[]>(`/api/chat/handoffs?tenant_id=${tenantId}&status=pending`)
      .then(setHandoffs)
      .catch((error) => {
        notifyRequestError('handoffs', error, '待回答加载失败');
      })
      .finally(() => setHandoffsLoading(false));
  }, [auth, notifyRequestError, tenantId]);

  const submitHandoffReply = useCallback((handoff: HumanHandoffRead) => {
    const reply = (handoffReplies[handoff.id] || '').trim();
    if (!reply) {
      message.warning('请输入回复内容');
      return;
    }
    api
      .post<HumanHandoffRead>(`/api/chat/handoffs/${handoff.id}/reply`, {
        tenant_id: tenantId,
        reply,
      })
      .then(() => {
        message.success('已回复，原会话会继续执行');
        setHandoffs((rows) => rows.filter((item) => item.id !== handoff.id));
        setHandoffReplies((prev) => {
          const next = { ...prev };
          delete next[handoff.id];
          return next;
        });
        loadSessions();
        getSlot(handoff.session_id);
        void loadMessages(handoff.session_id);
        void loadTraces(handoff.session_id);
      })
      .catch((error) => {
        if (isAuthError(error)) {
          clearAuthSession();
          navigate('/login', { replace: true });
          return;
        }
        message.error(error instanceof Error ? error.message : '回复失败');
      });
  }, [getSlot, handoffReplies, loadMessages, loadSessions, loadTraces, navigate, tenantId]);

  const appendRealtime = useCallback((id: string, messageItem: ChatMessage) => {
    const slot = getSlot(id);
    slot.realtimeMessages = [...slot.realtimeMessages, messageItem].slice(-200);
    notifyStore();
  }, [getSlot, notifyStore]);

  const updateMessageFeedback = useCallback((
    id: string,
    messageId: string,
    rating: ChatMessage['feedback_rating'],
  ) => {
    const slot = getSlot(id);
    const update = (item: ChatMessage) => (
      item.id === messageId ? { ...item, feedback_rating: rating } : item
    );
    slot.serverMessages = slot.serverMessages.map(update);
    slot.realtimeMessages = slot.realtimeMessages.map(update);
    notifyFeedback();
  }, [getSlot, notifyFeedback]);

  const updateStreaming = useCallback((id: string, text: string, turnId?: string, allowEmpty = false) => {
    const slot = getSlot(id);
    const stream = getStreamSlot(id);
    const streamId = `__streaming_${id}`;
    const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (!text && index < 0 && !allowEmpty) return;
    const previousMessage = index >= 0 ? slot.realtimeMessages[index] : undefined;
    const activeTurnId = turnId || stream.turnId || undefined;
    const previousCreatedAt = previousMessage && previousMessage.turnId === activeTurnId
      ? previousMessage.created_at
      : undefined;
    const streamingMessage: ChatMessage = {
      id: streamId,
      turnId: activeTurnId,
      role: 'assistant',
      content: text,
      created_at: previousCreatedAt || timestampAfterMessage(latestUserMessageForTurn(slot, activeTurnId)),
      isStreaming: true,
    };
    if (index >= 0) {
      const previous = slot.realtimeMessages[index];
      if (
        previous.turnId === streamingMessage.turnId
        && previous.content === streamingMessage.content
        && previous.isStreaming === streamingMessage.isStreaming
      ) {
        return;
      }
      slot.realtimeMessages = [...slot.realtimeMessages];
      slot.realtimeMessages[index] = streamingMessage;
    } else {
      slot.realtimeMessages = [...slot.realtimeMessages, streamingMessage];
    }
    notifyStore();
  }, [getSlot, getStreamSlot, notifyStore]);

  const ensureStreamingTraceMessage = useCallback((id: string, turnId?: string | null) => {
    if (!turnId) return;
    const stream = getStreamSlot(id);
    if (!stream.loading) return;
    updateStreaming(id, stream.accumulated || '', turnId, true);
  }, [getStreamSlot, updateStreaming]);

  const flushStreaming = useCallback((id: string) => {
    const stream = getStreamSlot(id);
    if (stream.timer) {
      window.clearTimeout(stream.timer);
      stream.timer = null;
    }
    if (stream.accumulated) {
      updateStreaming(id, stream.accumulated);
    }
  }, [getStreamSlot, updateStreaming]);

  const finalizeStreaming = useCallback((id: string) => {
    flushStreaming(id);
    const slot = getSlot(id);
    const streamId = `__streaming_${id}`;
    const index = slot.realtimeMessages.findIndex((item) => item.id === streamId);
    if (index >= 0) {
      const streamMessage = slot.realtimeMessages[index];
      slot.realtimeMessages = [...slot.realtimeMessages];
      slot.realtimeMessages[index] = {
        ...streamMessage,
        id: `text_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        isStreaming: false,
      };
    }
    const stream = getStreamSlot(id);
    stream.accumulated = '';
    stream.turnId = null;
    notifyStore();
  }, [flushStreaming, getSlot, getStreamSlot, notifyStore]);

  useEffect(() => {
    if (!auth) {
      navigate('/login', { replace: true });
      return;
    }
    loadSessions();
  }, [auth, loadSessions, navigate]);

  useEffect(() => {
    if (!auth) return;
    const timer = window.setInterval(loadSessions, 2500);
    return () => window.clearInterval(timer);
  }, [auth, loadSessions]);

  useEffect(() => {
    if (!auth) return;
    void loadHandoffs();
    const timer = window.setInterval(() => {
      void loadHandoffs();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [auth, loadHandoffs]);

  useEffect(() => {
    if (!auth) return;
    api
      .get<UIConfigRead>(`/api/chat/ui-config?tenant_id=${tenantId}`)
      .then(setUiConfig)
      .catch(() => undefined);
  }, [auth, tenantId]);

  useEffect(() => {
    if (!sessionId) return;
    void loadMessages(sessionId).finally(() => {
      void loadTraces(sessionId);
    });
  }, [loadMessages, loadTraces, sessionId]);

  useEffect(() => {
    if (!sessionId || runningTurn?.sessionId !== sessionId) return;
    const timer = window.setInterval(() => {
      void loadMessages(sessionId).finally(() => {
        void loadTraces(sessionId);
      });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [loadMessages, loadTraces, runningTurn?.sessionId, sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const session = sessions.find((item) => item.id === sessionId);
    if (session) {
      markSessionRead(sessionId, session.updated_at);
    }
  }, [markSessionRead, sessionId, sessions]);

  useLayoutEffect(() => {
    const conversationChanged = activeConversationId !== lastActiveConversationIdRef.current;
    lastActiveConversationIdRef.current = activeConversationId;
    if (conversationChanged) {
      isChatStickyToBottomRef.current = true;
      scrollChatToBottom({ preserveShortContentTop: true, force: true });
      return;
    }
    scrollChatToBottom();
  }, [activeConversationId, displayedMessages.length, scrollChatToBottom, traceTick]);

  useEffect(() => {
    if (!currentTraceRunning) return;
    scrollChatToBottom();
  }, [currentTraceRunning, currentStream.phase, scrollChatToBottom, streamTick, traceTick]);

  useEffect(() => {
    return () => {
      streamRef.current.forEach((slot) => {
        if (slot.timer) {
          window.clearTimeout(slot.timer);
        }
      });
    };
  }, []);

  function openRename(event: MouseEvent<HTMLElement>, session: ChatSession) {
    event.stopPropagation();
    setRenameSession(session);
    setRenameTitle(session.title || session.summary || session.last_agent_question || '');
  }

  async function saveRename() {
    if (!renameSession) return;
    const title = renameTitle.trim();
    if (!title) {
      message.warning('请输入会话名称');
      return;
    }
    const updated = await api.put<ChatSession>(`/api/chat/sessions/${renameSession.id}`, {
      tenant_id: tenantId,
      title,
    });
    setSessions((items) => items.map((item) => (item.id === updated.id ? updated : item)));
    setRenameSession(null);
    setRenameTitle('');
    message.success('已重命名');
  }

  function confirmDelete(event: MouseEvent<HTMLElement>, target: ChatSession) {
    event.stopPropagation();
    Modal.confirm({
      title: '删除会话',
      content: `确定删除「${target.title || target.summary || target.last_agent_question || '新会话'}」吗？此操作会同时删除该会话的消息记录。`,
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        const stream = getStreamSlot(target.id);
        stream.abortController?.abort();
        streamRef.current.delete(target.id);
        storeRef.current.delete(target.id);
        await api.delete(`/api/chat/sessions/${target.id}?tenant_id=${tenantId}`);
        forgetMissingSession(target.id);
        if (target.id === sessionId) {
          navigate('/');
        }
        message.success('已删除');
      },
    });
  }

  function abortStream() {
    if (!activeConversationId) return;
    const stream = getStreamSlot(activeConversationId);
    if (!stream.loading && stream.cancelledTurnId) return;
    const cancelledTurnId = stream.turnId || (
      runningTurn?.sessionId === activeConversationId ? runningTurn.turnId : null
    );
    const controller = stream.abortController;
    locallyCancelledSessionIdsRef.current.add(activeConversationId);
    const cancelRequest = cancelledTurnId && !isDraftConversationKey(activeConversationId)
      ? api.post(`/api/chat/sessions/${activeConversationId}/cancel`, {
          tenant_id: tenantId,
          turn_id: cancelledTurnId,
        }).catch(() => undefined)
      : Promise.resolve();
    if (cancelledTurnId) {
      upsertTraceLine(cancelledTurnId, {
        id: 'generation_stopped',
        kind: 'decision',
        text: '已停止生成',
        state: 'failed',
      });
      finishTrace(cancelledTurnId, true);
    }
    clearStreamSlot(activeConversationId, false);
    if (cancelledTurnId) {
      upsertTraceStatusPlaceholder(getSlot(activeConversationId), activeConversationId, cancelledTurnId);
      notifyStore();
    }
    const stoppedStream = getStreamSlot(activeConversationId);
    stoppedStream.cancelledTurnId = cancelledTurnId;
    setRunningTurn((current) => (current?.sessionId === activeConversationId ? null : current));
    notifyStream();
    const abortAfterCancel = () => {
      controller?.abort();
    };
    if (controller) {
      const cancelDeadline = new Promise<void>((resolve) => {
        window.setTimeout(resolve, 300);
      });
      void Promise.race([cancelRequest, cancelDeadline]).then(abortAfterCancel, abortAfterCancel);
    }
  }

  async function rateMessage(item: ChatMessage, rating: 'up' | 'down') {
    if (!sessionId || !canRateMessage(item)) return;
    const previous = item.feedback_rating || null;
    const next = previous === rating ? null : rating;
    updateMessageFeedback(sessionId, item.id, next);
    try {
      if (next) {
        await api.post(`/api/chat/messages/${item.id}/feedback`, {
          tenant_id: tenantId,
          rating: next,
        });
      } else {
        await api.delete(`/api/chat/messages/${item.id}/feedback?tenant_id=${tenantId}`);
      }
    } catch (error) {
      updateMessageFeedback(sessionId, item.id, previous);
      if (isAuthError(error)) {
        clearAuthSession();
        navigate('/login', { replace: true });
        return;
      }
      message.error(error instanceof Error ? error.message : '反馈提交失败');
    }
  }

  async function confirmScheduledTask(draft: ScheduledTaskDraftRead, draftKey?: string) {
    if (!sessionId) return;
    try {
      const saved = await api.post<ScheduledTaskRead>('/api/chat/scheduled-tasks', {
        tenant_id: tenantId,
        agent_id: draft.agent_id,
        title: draft.title,
        prompt: draft.prompt,
        description: draft.description,
        schedule_type: draft.schedule_type,
        schedule: draft.schedule,
        timezone: draft.timezone || 'Asia/Shanghai',
        rrule: draft.rrule,
        status: 'active',
        concurrency_policy: 'forbid',
        misfire_policy: 'coalesce',
        source_session_id: draft.source_session_id || sessionId,
        metadata: {
          created_from: 'chat_confirmation',
          confidence: draft.confidence,
          reason: draft.reason,
        },
      });
      const createdKey = draftKey || `session:${sessionId}`;
      setCreatedScheduledTasks((prev) => ({ ...prev, [createdKey]: saved }));
      setScheduledDrafts((prev) => {
        const next = { ...prev };
        delete next[sessionId];
        return next;
      });
      message.success(`定时任务「${saved.title}」已启用`);
    } catch (error) {
      if (isAuthError(error)) {
        clearAuthSession();
        navigate('/login', { replace: true });
        return;
      }
      message.error(error instanceof Error ? error.message : '创建定时任务失败');
    }
  }

  function dismissScheduledTaskDraft(messageId?: string) {
    if (!sessionId) return;
    setScheduledDrafts((prev) => {
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
    if (messageId) {
      setDismissedDraftMessageIds((prev) => (prev.includes(messageId) ? prev : [...prev, messageId]));
    }
  }

  const handleStreamEvent = useCallback((item: ChatStreamEvent, baseSessionId: string, turnId: string) => {
    const eventSessionId = String(item.data.sessionId || baseSessionId);
    const traceTurnId = explicitStreamTurnId(item.data, turnId);
    if (item.event === 'session_created') {
      return;
    }
    if (item.event === 'session_title_summarized') {
      applySessionTitleSummary(eventSessionId, item.data.title);
      return;
    }
    if (item.event === 'scheduled_task_draft') {
      const draft = item.data as unknown as ScheduledTaskDraftRead;
      if (draft.should_create) {
        setScheduledDrafts((prev) => ({ ...prev, [eventSessionId]: draft }));
      }
      return;
    }
    if (traceTurnId && !STREAM_TERMINAL_EVENTS.has(item.event)) {
      const eventStream = getStreamSlot(eventSessionId);
      if (eventStream.turnId !== traceTurnId) {
        eventStream.turnId = traceTurnId;
      }
      ensureStreamingTraceMessage(eventSessionId, traceTurnId);
    }
    if (item.event === 'router_decision') {
      upsertTraceLine(traceTurnId, routerDecisionTraceLine(item.data));
      return;
    }
    if (item.event === 'step_result') {
      upsertTraceLine(traceTurnId, stepResultTraceLine(item.data));
      return;
    }
    if (item.event === 'skill_state') {
      const skills = Array.isArray(item.data.currentSkills) ? item.data.currentSkills : [];
      skills
        .map((entry) => normalizeTraceSkill(entry))
        .filter((entry): entry is TraceSkill => Boolean(entry))
        .forEach((skill) => {
          const label = streamSkillLabel(item.data, skill);
          upsertTraceLine(traceTurnId, {
            id: `skill_${skill.skillId}_${skill.state || 'active'}`,
            kind: 'skill',
            text: `${label} ${skill.name || skill.skillId}`,
            detail: skill.stepId ? `当前步骤 ${skill.stepId}` : undefined,
            state: skill.state === 'suspended' ? 'completed' : 'running',
          });
      });
      return;
    }
    if (item.event === 'general_skill_state') {
      const skillName = typeof item.data.skillName === 'string' ? item.data.skillName : '';
      const skillSlug = typeof item.data.skillSlug === 'string' ? item.data.skillSlug : '';
      upsertTraceLine(traceTurnId, {
        id: `general_skill_${skillSlug || skillName || 'selected'}`,
        kind: 'skill',
        text: `选择技能 ${skillName || skillSlug || ''}`.trim(),
        detail: skillSlug || undefined,
        state: 'running',
      });
      return;
    }
    if (item.event === 'general_skill_trace') {
      const phase = typeof item.data.phase === 'string' ? item.data.phase : 'trace';
      if (HIDDEN_GENERAL_SKILL_TRACE_PHASES.has(phase)) {
        notifyStream();
        return;
      }
      const text = typeof item.data.message === 'string' ? item.data.message : '执行技能';
      const code = typeof item.data.code === 'string' ? item.data.code : '';
      const runtime = typeof item.data.runtime === 'string' ? item.data.runtime : '';
      const attempt = typeof item.data.attempt === 'number' || typeof item.data.attempt === 'string'
        ? String(item.data.attempt)
        : '';
      const trace = getTurnTrace(traceTurnId);
      const sequence = trace.lines.length;
      const isOutputChunk = phase === 'stdout_chunk' || phase === 'stderr_chunk';
      const id = isOutputChunk
        ? `general_skill_trace_${phase}_${attempt || 'current'}`
        : `general_skill_trace_${phase}_${attempt || sequence}`;
      const rawDetail = generalSkillTraceDetail(item.data, phase);
      const existing = trace.lines.find((line) => line.id === id);
      const previousOutput = existing?.output || existing?.detail || '';
      const detail = isOutputChunk && previousOutput && rawDetail
        ? `${previousOutput}${rawDetail}`
        : rawDetail;
      const outputInfo = generalSkillTraceOutput(item.data, phase, detail);
      const codePhases = new Set([
        'plan_created',
        'attempt_started',
        'running_code',
        'stdout_chunk',
        'stderr_chunk',
        'code_finished',
        'code_timeout',
      ]);
      const runningPhases = new Set([
        'planning',
        'repair_planning',
        'attempt_started',
        'running_code',
        'reflection_reviewing',
        'replying',
      ]);
      upsertTraceLine(traceTurnId, {
        id,
        kind: codePhases.has(phase) ? 'code' : 'decision',
        text,
        detail: outputInfo.output ? undefined : detail,
        code: code || undefined,
        language: code ? (runtime === 'bash' ? 'bash' : 'python') : undefined,
        output: outputInfo.output,
        outputLanguage: outputInfo.language,
        outputTitle: outputInfo.title,
        state: runningPhases.has(phase) ? 'running' : phase.includes('failed') || phase === 'code_timeout' ? 'failed' : 'completed',
        collapsible: Boolean(code || outputInfo.output),
      });
      return;
    }
    if (item.event === 'knowledge_result') {
      upsertTraceLine(traceTurnId, {
        id: 'knowledge_lookup',
        kind: 'knowledge',
        text: '读取知识库',
        detail: knowledgeResultTraceDetail(item.data),
        state: 'completed',
      });
      return;
    }
    if (item.event === 'tool_result') {
      const tool = normalizeTraceTool(item.data);
      if (tool) {
        upsertTraceLine(traceTurnId, {
          id: `tool_${tool.toolCallId || tool.rawToolName || tool.toolId}`,
          kind: 'tool',
          text: `${tool.isError ? '工具调用失败' : '调用工具'} ${tool.toolName}`,
          detail: toolTraceDetail(tool),
          state: tool.isError ? 'failed' : 'completed',
        });
      }
      return;
    }
    if (item.event === 'agent_loop_continued' || item.event === 'agent_loop_completed') {
      const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
        ? String(item.data.iteration)
        : '1';
      const targetTool = typeof item.data.target_tool_name === 'string' ? item.data.target_tool_name : '';
      upsertTraceLine(traceTurnId, {
        id: `decision_stepping_tool_continuation_${iteration}`,
        kind: 'decision',
        text: '重新分析',
        detail: item.event === 'agent_loop_continued'
          ? (targetTool ? `决定继续调用 ${targetTool}` : '决定继续调用工具')
          : '判断无需继续调用工具',
        state: 'completed',
      });
      return;
    }
    if (item.event === 'reflection_decision') {
      const needsRetry = item.data.needs_retry === true;
      const skipped = item.data.skipped === true;
      upsertTraceLine(traceTurnId, {
        id: 'reflection',
        kind: 'decision',
        text: skipped ? '反思已关闭' : needsRetry ? '反思后继续尝试' : '反思通过',
        detail: reflectionTraceDetail(item.data),
        state: 'completed',
      });
      return;
    }
    if (item.event === 'status') {
      const eventStream = getStreamSlot(eventSessionId);
      if (traceTurnId && eventStream.turnId !== traceTurnId) {
        eventStream.turnId = traceTurnId;
      }
      const phase = typeof item.data.phase === 'string' ? item.data.phase : 'thinking';
      if (phase === 'responding') {
        notifyStream();
        return;
      }
      eventStream.phase = publicStreamPhase(item.data);
      if (phase === 'tool' && typeof item.data.tool_name === 'string') {
        const toolCallId = typeof item.data.tool_call_id === 'string' ? item.data.tool_call_id : item.data.tool_name;
        upsertTraceLine(traceTurnId, {
          id: `tool_${toolCallId}`,
          kind: 'tool',
          text: `正在调用 ${item.data.tool_name}`,
          state: 'running',
        });
      } else if (phase === 'routing') {
        upsertTraceLine(traceTurnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
      } else if (isKnowledgeTracePhase(phase)) {
        upsertTraceLine(traceTurnId, {
          id: 'knowledge_lookup',
          kind: 'knowledge',
          text: knowledgeTraceText(item.data),
          detail: knowledgeTraceDetail(item.data),
          state: phase === 'evidence_pack' || phase.startsWith('no_') || phase === 'okf_only' ? 'completed' : 'running',
        });
      } else if (phase === 'stepping') {
        const repairReason = typeof item.data.repair_reason === 'string' ? item.data.repair_reason : 'main';
        const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
          ? `_${item.data.iteration}`
          : '';
        upsertTraceLine(traceTurnId, {
          id: `decision_stepping_${repairReason}${iteration}`,
          kind: 'decision',
          text: repairReason === 'main' ? '决定下一步' : '重新分析',
          state: 'running',
        });
      } else if (phase === 'reflecting') {
        upsertTraceLine(traceTurnId, { id: 'reflection', kind: 'decision', text: '正在反思', state: 'running' });
      } else if (phase === 'scheduled_task_draft') {
        upsertTraceLine(traceTurnId, {
          id: 'scheduled_task_draft',
          kind: 'decision',
          text: '生成定时任务草案',
          detail: '来自本条消息的定时任务，等待用户确认后启用',
          state: 'completed',
        });
      } else if (phase !== 'received') {
        upsertTraceLine(traceTurnId, {
          id: `decision_status_${phase}`,
          kind: 'decision',
          text: eventStream.phase,
          state: 'running',
        });
      }
      notifyStream();
      return;
    }
    if (item.event === 'stream_replace') {
      const next = typeof item.data.content === 'string' ? item.data.content : '';
      const eventStream = getStreamSlot(eventSessionId);
      if (eventStream.timer) {
        window.clearTimeout(eventStream.timer);
        eventStream.timer = null;
      }
      eventStream.accumulated = next;
      updateStreaming(eventSessionId, next, getStreamSlot(eventSessionId).turnId || traceTurnId);
      notifyStream();
      return;
    }
    if (item.event === 'stream_delta' || item.event === 'token') {
      const piece = typeof item.data.content === 'string' ? item.data.content : '';
      if (!piece) return;
      const eventStream = getStreamSlot(eventSessionId);
      const wasEmpty = !eventStream.accumulated;
      eventStream.accumulated += piece;
      if (wasEmpty) {
        updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
        notifyStream();
        return;
      }
      if (!eventStream.timer) {
        eventStream.timer = window.setTimeout(() => {
          eventStream.timer = null;
          updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
        }, 100);
      }
      return;
    }
    if (item.event === 'stream_end') {
      finishTrace(traceTurnId);
      upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
      finalizeStreaming(eventSessionId);
      const eventStream = getStreamSlot(eventSessionId);
      eventStream.loading = false;
      eventStream.phase = '';
      eventStream.abortController = null;
      notifyStream();
      return;
    }
    if (item.event === 'stream_cancelled') {
      const cancelledStreamTurnId = getStreamSlot(eventSessionId).turnId || traceTurnId;
      finishTrace(traceTurnId, true);
      clearStreamSlot(eventSessionId, false);
      upsertTraceStatusPlaceholder(getSlot(eventSessionId), eventSessionId, traceTurnId);
      notifyStore();
      setRunningTurn((current) => (
        current?.sessionId === eventSessionId && (current.turnId === traceTurnId || current.turnId === cancelledStreamTurnId)
          ? null
          : current
      ));
      notifyStream();
      return;
    }
    if (item.event === 'complete' || item.event === 'done') {
      const result = item.data as ChatTurnResponse;
      const userIntent = typeof result.router_decision?.user_intent === 'string' ? result.router_decision.user_intent : '';
      const decisionReason = typeof result.router_decision?.reason === 'string' ? result.router_decision.reason : '';
      if (userIntent || decisionReason) {
        upsertTraceLine(traceTurnId, {
          id: 'decision_router',
          kind: 'decision',
          text: userIntent ? `判断意图 ${userIntent}` : '完成技能判断',
          detail: decisionReason || undefined,
          state: 'completed',
        });
      }
      finishTrace(traceTurnId);
      upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
      finalizeStreaming(eventSessionId);
      setLastTurn(result);
      const eventStream = getStreamSlot(eventSessionId);
      eventStream.loading = false;
      eventStream.phase = '';
      eventStream.abortController = null;
      notifyStream();
      loadSessions();
      window.setTimeout(() => {
        loadMessages(eventSessionId);
        loadTraces(eventSessionId);
      }, 250);
    }
    if (item.event === 'error') {
      const eventStream = getStreamSlot(eventSessionId);
      eventStream.loading = false;
      eventStream.phase = '';
      eventStream.abortController = null;
      finishTrace(eventStream.turnId || traceTurnId, true);
      appendRealtime(eventSessionId, {
        id: `scheduled_error_${Date.now()}`,
        turnId: eventStream.turnId || traceTurnId,
        role: 'assistant',
        content: typeof item.data.message === 'string' ? item.data.message : '定时任务执行失败。',
        created_at: new Date().toISOString(),
        isError: true,
      });
      notifyStream();
    }
  }, [
    appendRealtime,
    applySessionTitleSummary,
    clearStreamSlot,
    finalizeStreaming,
    finishTrace,
    getStreamSlot,
    getTurnTrace,
    loadMessages,
    loadSessions,
    loadTraces,
    notifyStream,
    ensureStreamingTraceMessage,
    updateStreaming,
    upsertTraceLine,
    getSlot,
    notifyStore,
  ]);

  const eventTextPayload = useCallback((event: ChatSessionEventRead): string => {
    const data = event.data || {};
    if (typeof data.content === 'string') return data.content;
    if (typeof data.text === 'string') return data.text;
    return '';
  }, []);

  const isTerminalEvent = useCallback((event: ChatSessionEventRead) => (
    event.event === 'complete' ||
    event.event === 'done' ||
    event.event === 'stream_end' ||
    event.event === 'stream_cancelled' ||
    event.event === 'error'
  ), []);

  const eventTime = useCallback((event: ChatSessionEventRead) => {
    const time = parseMessageTime(event.created_at);
    return time || 0;
  }, []);

  const hydrateRunningSessionFromEvents = useCallback((id: string, events: ChatSessionEventRead[]) => {
    if (locallyCancelledSessionIdsRef.current.has(id)) return false;
    const traceEvents = events.filter((event) => Boolean(eventTraceTurnId(event)));
    if (!traceEvents.length) return false;
    const slot = getSlot(id);
    if (slot.serverMessages.length === 0) return false;
    const stream = getStreamSlot(id);
    if (stream.loading) return false;

    const groups = new Map<string, ChatSessionEventRead[]>();
    traceEvents.forEach((event) => {
      const turnId = eventTraceTurnId(event);
      if (!turnId) return;
      const key = `${id}:${turnId}`;
      const bucket = groups.get(key) || [];
      bucket.push(event);
      groups.set(key, bucket);
    });

    const runningGroup = [...groups.values()]
      .map((group) => [...group].sort((left, right) => eventTime(left) - eventTime(right)))
      .sort((left, right) => eventTime(right[right.length - 1]) - eventTime(left[left.length - 1]))
      .find((group) => !group.some((event) => isTerminalSessionEvent(event, isTerminalEvent)));

    if (!runningGroup?.length) return false;

    const turnId = eventTraceTurnId(runningGroup[0]);
    if (!turnId) return false;
    const runningUserMessage = slot.serverMessages.find((messageItem) => (
      messageItem.role === 'user'
      && (effectiveMessageTurnId(messageItem) === turnId || messageItem.id === turnId)
    ));
    if (!runningUserMessage) return false;
    const latestRunningEventTime = eventTime(runningGroup[runningGroup.length - 1]);
    if (
      latestRunningEventTime <= 0
      || Date.now() - latestRunningEventTime > RUNNING_EVENT_RECOVERY_WINDOW_MS
    ) {
      clearStreamSlot(id, false);
      return false;
    }
    if (!hasRecoverableEventProgress(runningGroup)) {
      clearStreamSlot(id, false);
      return false;
    }
    if (hasAssistantMessageForTurn(slot, turnId)) {
      clearStreamSlot(id, false);
      return false;
    }

    let text = '';
    runningGroup.forEach((event) => {
      const payloadText = eventTextPayload(event);
      if (event.event === 'stream_replace') {
        text = payloadText;
      } else if (event.event === 'stream_delta' || event.event === 'token') {
        text += payloadText;
      }
    });

    stream.turnId = turnId;
    stream.loading = true;
    stream.phase = '执行中';
    stream.accumulated = text;
    updateStreaming(id, text, turnId, true);

    runningGroup.forEach((event) => {
      scheduledEventIdsRef.current.add(event.id);
      const streamEvent = normalizeSessionEventForStream(event);
      if (streamEvent.event === 'stream_replace' || streamEvent.event === 'stream_delta' || streamEvent.event === 'token') return;
      handleStreamEvent(streamEvent, id, turnId);
    });

    notifyStream();
    return true;
  }, [
    clearStreamSlot,
    eventTextPayload,
    eventTime,
    getSlot,
    getStreamSlot,
    handleStreamEvent,
    isTerminalEvent,
    notifyStream,
    updateStreaming,
  ]);

  const pollScheduledSessionEvents = useCallback((id: string) => {
    if (locallyCancelledSessionIdsRef.current.has(id)) return Promise.resolve();
    return api
      .get<ChatSessionEventRead[]>(`/api/chat/sessions/${id}/events?tenant_id=${tenantId}`)
      .then((events) => {
        const traceEvents = events.filter((event) => Boolean(eventTraceTurnId(event)));
        if (!traceEvents.length) return;
        hydrateRunningSessionFromEvents(id, traceEvents);
        const slot = getSlot(id);
        if (slot.serverMessages.length === 0) return;
        const latestLoadedMessageTime = Math.max(
          0,
          ...slot.serverMessages.map((messageItem) => parseMessageTime(messageItem.created_at)),
        );
        const now = Date.now();
        const stream = getStreamSlot(id);
        const unseenEvents = traceEvents.filter((event) => {
          if (scheduledEventIdsRef.current.has(event.id)) return false;
          const timestamp = eventTime(event);
          return (
            timestamp > 0
            && (
              timestamp >= latestLoadedMessageTime
              || now - timestamp <= RUNNING_EVENT_RECOVERY_WINDOW_MS
            )
          );
        });
        if (!unseenEvents.length) return;
        const sessionRow = sessions.find((item) => item.id === id);
        const hasTerminalEvent = unseenEvents.some((event) => isTerminalSessionEvent(event, isTerminalEvent));
        if (!stream.loading && Boolean(sessionRow?.summary) && hasTerminalEvent) {
          unseenEvents.forEach((event) => scheduledEventIdsRef.current.add(event.id));
          return;
        }
        const unseenEventsByTurn = new Map<string, ChatSessionEventRead[]>();
        unseenEvents.forEach((event) => {
          const turnId = eventTraceTurnId(event);
          if (!turnId) return;
          const bucket = unseenEventsByTurn.get(turnId) || [];
          bucket.push(event);
          unseenEventsByTurn.set(turnId, bucket);
        });
        unseenEvents.forEach((event) => {
          scheduledEventIdsRef.current.add(event.id);
          const turnId = eventTraceTurnId(event);
          if (!turnId) return;
          if (!stream.turnId) {
            stream.turnId = turnId;
          }
          const streamEvent = normalizeSessionEventForStream(event);
          if (!stream.loading && !isTerminalSessionEvent(event, isTerminalEvent)) {
            const turnEvents = unseenEventsByTurn.get(turnId) || [event];
            if (!hasRecoverableEventProgress(turnEvents)) {
              handleStreamEvent(streamEvent, id, turnId);
              return;
            }
            stream.loading = true;
            stream.phase = '执行中';
            updateStreaming(id, stream.accumulated || '', turnId, true);
            notifyStream();
          }
          handleStreamEvent(streamEvent, id, turnId);
        });
      })
      .catch((error) => {
        if (isAuthError(error)) {
          clearAuthSession();
          navigate('/login', { replace: true });
        }
      });
  }, [
    getStreamSlot,
    handleStreamEvent,
    hydrateRunningSessionFromEvents,
    isTerminalEvent,
    navigate,
    notifyStream,
    sessions,
    tenantId,
    updateStreaming,
  ]);

  function uploadComposerFiles(files: File[]) {
    const validFiles = files.filter((file) => file.size > 0);
    if (!validFiles.length) return;
    if (currentSessionRunning) {
      message.warning('当前对话正在执行，结束后再上传文件');
      return;
    }
    validFiles.forEach((file) => {
      const uploadKey = `upload_${Date.now()}_${Math.random().toString(16).slice(2)}`;
      const controller = new AbortController();
      uploadControllersRef.current.set(uploadKey, controller);
      setComposerAttachments((current) => [
        ...current,
        {
          id: uploadKey,
          uploadKey,
          filename: file.name || '剪贴板文件',
          content_type: file.type || 'application/octet-stream',
          size: file.size,
          kind: file.type.startsWith('image/') ? 'image' : 'binary',
          uploadStatus: 'uploading',
        },
      ]);
      uploadChatAttachments<ChatAttachmentRead[]>(tenantId, [file], controller.signal)
        .then((items) => {
          const parsed = items[0];
          if (!parsed) throw new Error('文件解析结果为空');
          setComposerAttachments((current) =>
            current.map((item) =>
              item.uploadKey === uploadKey ? { ...parsed, uploadKey, uploadStatus: 'ready' } : item,
            ),
          );
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setComposerAttachments((current) =>
            current.map((item) =>
              item.uploadKey === uploadKey
                ? {
                    ...item,
                    uploadStatus: 'error',
                    error: error instanceof Error ? error.message : '上传失败',
                  }
                : item,
            ),
          );
        })
        .finally(() => {
          uploadControllersRef.current.delete(uploadKey);
        });
    });
  }

  function removeComposerAttachment(uploadKey: string) {
    uploadControllersRef.current.get(uploadKey)?.abort();
    uploadControllersRef.current.delete(uploadKey);
    setComposerAttachments((current) => current.filter((item) => item.uploadKey !== uploadKey));
  }

  function handleComposerPaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    if (!clipboardContainsComposerImage(event.clipboardData)) return;
    event.preventDefault();
    void extractPastedComposerFiles(event.clipboardData).then((files) => {
      if (files.length) {
        uploadComposerFiles(files);
        return;
      }
      message.warning('无法读取剪贴板图片，请保存后上传');
    });
  }

  function handleComposerFileChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    uploadComposerFiles(files);
    event.target.value = '';
  }

  function handleComposerDragEnter(event: DragEvent<HTMLFormElement>) {
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    setComposerDragActive(true);
  }

  function handleComposerDragOver(event: DragEvent<HTMLFormElement>) {
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    setComposerDragActive(true);
  }

  function handleComposerDragLeave(event: DragEvent<HTMLFormElement>) {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
      setComposerDragActive(false);
    }
  }

  function handleComposerDrop(event: DragEvent<HTMLFormElement>) {
    const files = Array.from(event.dataTransfer.files || []);
    if (!files.length) return;
    event.preventDefault();
    setComposerDragActive(false);
    uploadComposerFiles(files);
  }

  useEffect(() => {
    if (!auth) return;
    const pollBackgroundSessions = () => {
      const ids = new Set<string>();
      sessions.forEach((session) => {
        const looksRunning = (
          session.status === 'running'
          || session.status === 'executing'
          || (session.summary || '').includes('执行中')
          || (session.last_agent_question || '').includes('执行中')
        );
        if (looksRunning) ids.add(session.id);
      });
      streamRef.current.forEach((slot, id) => {
        if (slot.loading && !isDraftConversationKey(id)) ids.add(id);
      });
      Array.from(ids).slice(0, 8).forEach((id) => {
        void pollScheduledSessionEvents(id);
      });
    };
    pollBackgroundSessions();
    const timer = window.setInterval(pollBackgroundSessions, 1800);
    return () => window.clearInterval(timer);
  }, [auth, pollScheduledSessionEvents, sessionId, sessions, streamTick]);

  async function send(interactionMode?: ComposerInteractionMode) {
    const resolvedInteractionMode = interactionMode || composerIntent || 'normal';
    if (!activeConversationId) return;
    if (resolvedInteractionMode === 'scheduled_task' && !input.trim()) {
      message.warning('请输入要创建的定时任务内容');
      return;
    }
    if (!input.trim() && readyComposerAttachments.length === 0) return;
    if (uploadingComposerAttachment) {
      message.warning('文件还在解析中，请稍后发送');
      return;
    }
    const currentConversationId = activeConversationId;
    const activeSession = sessionId ? sessions.find((item) => item.id === sessionId) || null : null;
    if (!isDraftConversation && !activeSession) {
      message.warning('任务信息还在加载，请稍后再发送');
      return;
    }
    const sessionAgentId = activeSession?.agent_id || activeDraftAgentId || selectedAgentId || displayedAgent?.id || '';
    if (!sessionAgentId) {
      message.warning('该任务没有绑定数字员工，请新建任务后再发送');
      return;
    }
    const stream = getStreamSlot(currentConversationId);
    if (stream.loading || currentSessionRunning) return;
    const userText = input.trim();
    const outgoingAttachments = readyComposerAttachments.map(toRequestAttachment);
    const turnId = `turn_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    let liveConversationId = currentConversationId;
    let createdSessionId = '';
    locallyCancelledSessionIdsRef.current.delete(currentConversationId);
    setInput('');
    setComposerAttachments([]);
    setComposerIntent(null);
    stream.accumulated = '';
    stream.cancelledTurnId = null;
    stream.turnId = turnId;
    appendRealtime(currentConversationId, {
      id: `local_${turnId}`,
      turnId,
      role: 'user',
      content: userText,
      metadata: {
        ...(outgoingAttachments.length ? { attachments: outgoingAttachments } : {}),
        ...(resolvedInteractionMode === 'scheduled_task' ? { interaction_mode: 'scheduled_task' } : {}),
      },
      created_at: new Date().toISOString(),
    });
    upsertTraceLine(turnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
    setCollapsedTraceIds((current) => current.filter((item) => item !== turnId));
    setExpandedTraceIds((current) => (current.includes(turnId) ? current : [...current, turnId]));
    stream.loading = true;
    stream.phase = '正在思考';
    updateStreaming(currentConversationId, '', turnId, true);
    setRunningTurn({ sessionId: currentConversationId, turnId });
    notifyStream();

    const controller = new AbortController();
    stream.abortController = controller;
    let receivedTerminalEvent = false;
    let streamWatchdog: number | null = null;

    const clearRunningTurn = (targetId = liveConversationId) => {
      setRunningTurn((current) => (
        current?.turnId === turnId && (current.sessionId === targetId || current.sessionId === currentConversationId)
          ? null
          : current
      ));
    };

    const clearStreamWatchdog = () => {
      if (streamWatchdog !== null) {
        window.clearTimeout(streamWatchdog);
        streamWatchdog = null;
      }
    };

    const appendInterruptedResponse = (reason: string) => {
      const activeTurnId = getStreamSlot(liveConversationId).turnId || turnId;
      clearStreamSlot(liveConversationId, true);
      appendRealtime(liveConversationId, {
        id: `stream_interrupted_${activeTurnId}_${Date.now()}`,
        turnId: activeTurnId,
        role: 'assistant',
        content: reason,
        created_at: new Date().toISOString(),
        isError: true,
      });
      finishTrace(activeTurnId, true);
      clearRunningTurn(liveConversationId);
      notifyStream();
      window.setTimeout(() => {
        loadMessages(liveConversationId);
        loadTraces(liveConversationId);
        loadSessions();
      }, 250);
    };

    const failStalledStream = () => {
      if (receivedTerminalEvent || controller.signal.aborted) return;
      const activeStream = getStreamSlot(liveConversationId);
      if (
        activeStream.abortController !== controller
        || activeStream.cancelledTurnId === turnId
        || (activeStream.turnId && activeStream.cancelledTurnId === activeStream.turnId)
      ) {
        return;
      }
      controller.abort();
      appendInterruptedResponse('本次响应等待时间过长，已停止等待。请重试发送。');
    };

    const armStreamWatchdog = () => {
      clearStreamWatchdog();
      streamWatchdog = window.setTimeout(failStalledStream, CHAT_STREAM_IDLE_TIMEOUT_MS);
    };

    const markStreamTerminal = () => {
      receivedTerminalEvent = true;
      clearStreamWatchdog();
    };

    const promoteDraftConversation = (nextSessionId: string) => {
      if (!isDraftConversation || !nextSessionId || nextSessionId === liveConversationId) return;
      const previousId = liveConversationId;
      const draftSlot = storeRef.current.get(previousId);
      if (draftSlot) {
        const previousStreamMessageId = `__streaming_${previousId}`;
        const nextStreamMessageId = `__streaming_${nextSessionId}`;
        draftSlot.realtimeMessages = draftSlot.realtimeMessages.map((item) => (
          item.id === previousStreamMessageId ? { ...item, id: nextStreamMessageId } : item
        ));
        storeRef.current.set(nextSessionId, draftSlot);
        storeRef.current.delete(previousId);
      }
      const draftStream = streamRef.current.get(previousId);
      if (draftStream) {
        streamRef.current.set(nextSessionId, draftStream);
        streamRef.current.delete(previousId);
      }
      if (locallyCancelledSessionIdsRef.current.has(previousId)) {
        locallyCancelledSessionIdsRef.current.delete(previousId);
        locallyCancelledSessionIdsRef.current.add(nextSessionId);
      }
      setScheduledDrafts((prev) => {
        if (!prev[previousId]) return prev;
        const next = { ...prev, [nextSessionId]: prev[previousId] };
        delete next[previousId];
        return next;
      });
      setCreatedScheduledTasks((prev) => {
        const previousKey = `session:${previousId}`;
        if (!prev[previousKey]) return prev;
        const next = { ...prev, [`session:${nextSessionId}`]: prev[previousKey] };
        delete next[previousKey];
        return next;
      });
      const now = new Date().toISOString();
      upsertOptimisticSession({
        id: nextSessionId,
        tenant_id: tenantId,
        user_id: userId,
        agent_id: sessionAgentId,
        status: 'active',
        summary: userText || undefined,
        last_agent_question: userText || undefined,
        updated_at: now,
      });
      liveConversationId = nextSessionId;
      setRunningTurn((current) => (
        current?.sessionId === previousId && current.turnId === turnId
          ? { sessionId: nextSessionId, turnId }
          : current
      ));
      notifyStore();
      notifyStream();
      navigate(`/${nextSessionId}`, { replace: true });
      loadSessions();
    };

    try {
      const requestBody: Record<string, unknown> = {
        tenant_id: tenantId,
        user_id: userId,
        agent_id: sessionAgentId,
        message: userText,
        client_turn_id: turnId,
        attachments: outgoingAttachments,
        channel: 'web',
        interaction_mode: resolvedInteractionMode,
        model_config_id: selectedModelConfig?.id,
      };
      if (!isDraftConversation) {
        requestBody.session_id = currentConversationId;
      }
      armStreamWatchdog();
      await streamChatTurn(requestBody, (item) => {
        if (!controller.signal.aborted) {
          armStreamWatchdog();
        }
        if (item.event === 'session_created') {
          createdSessionId = String(item.data.newSessionId || item.data.sessionId || '');
          return;
        }
        const eventSessionId = isDraftConversation
          ? currentConversationId
          : String(item.data.sessionId || liveConversationId);
        const eventStream = getStreamSlot(eventSessionId);
        const traceTurnId = explicitStreamTurnId(item.data, eventStream.turnId || turnId);
        if (
          controller.signal.aborted
          || eventStream.cancelledTurnId === turnId
          || Boolean(traceTurnId && eventStream.cancelledTurnId === traceTurnId)
        ) {
          return;
        }
        if (item.event === 'session_created') {
          return;
        }
        if (item.event === 'user_message_received') {
          const serverMessageId = typeof item.data.message_id === 'string' ? item.data.message_id : '';
          bindRealtimeUserToServerMessage(eventSessionId, turnId, serverMessageId);
          return;
        }
        if (item.event === 'session_title_summarized') {
          applySessionTitleSummary(eventSessionId, item.data.title);
          return;
        }
        if (traceTurnId && eventStream.turnId !== traceTurnId) {
          eventStream.turnId = traceTurnId;
        }
        if (traceTurnId && !STREAM_TERMINAL_EVENTS.has(item.event)) {
          ensureStreamingTraceMessage(eventSessionId, traceTurnId);
        }
        if (item.event === 'scheduled_task_draft') {
          const draft = item.data as unknown as ScheduledTaskDraftRead;
          if (draft.should_create) {
            setScheduledDrafts((prev) => ({ ...prev, [eventSessionId]: draft }));
          }
          return;
        }
        if (item.event === 'router_decision') {
          upsertTraceLine(traceTurnId, routerDecisionTraceLine(item.data));
          return;
        }
        if (item.event === 'step_result') {
          upsertTraceLine(traceTurnId, stepResultTraceLine(item.data));
          return;
        }
        if (item.event === 'skill_state') {
          const skills = Array.isArray(item.data.currentSkills) ? item.data.currentSkills : [];
          skills
            .map((entry) => normalizeTraceSkill(entry))
            .filter((entry): entry is TraceSkill => Boolean(entry))
            .forEach((skill) => {
              const label = streamSkillLabel(item.data, skill);
              upsertTraceLine(traceTurnId, {
                id: `skill_${skill.skillId}_${skill.state || 'active'}`,
                kind: 'skill',
                text: `${label} ${skill.name || skill.skillId}`,
                detail: skill.stepId ? `当前步骤 ${skill.stepId}` : undefined,
                state: skill.state === 'suspended' ? 'completed' : 'running',
              });
          });
          return;
        }
        if (item.event === 'general_skill_state') {
          const skillName = typeof item.data.skillName === 'string' ? item.data.skillName : '';
          const skillSlug = typeof item.data.skillSlug === 'string' ? item.data.skillSlug : '';
          upsertTraceLine(traceTurnId, {
            id: `general_skill_${skillSlug || skillName || 'selected'}`,
            kind: 'skill',
            text: `选择技能 ${skillName || skillSlug || ''}`.trim(),
            detail: skillSlug || undefined,
            state: 'running',
          });
          return;
        }
        if (item.event === 'general_skill_trace') {
          const phase = typeof item.data.phase === 'string' ? item.data.phase : 'trace';
          if (HIDDEN_GENERAL_SKILL_TRACE_PHASES.has(phase)) {
            notifyStream();
            return;
          }
          const text = typeof item.data.message === 'string' ? item.data.message : '执行技能';
          const code = typeof item.data.code === 'string' ? item.data.code : '';
          const runtime = typeof item.data.runtime === 'string' ? item.data.runtime : '';
          const attempt = typeof item.data.attempt === 'number' || typeof item.data.attempt === 'string'
            ? String(item.data.attempt)
            : '';
          const trace = getTurnTrace(traceTurnId);
          const sequence = trace.lines.length;
          const isOutputChunk = phase === 'stdout_chunk' || phase === 'stderr_chunk';
          const id = isOutputChunk
            ? `general_skill_trace_${phase}_${attempt || 'current'}`
            : `general_skill_trace_${phase}_${attempt || sequence}`;
          const rawDetail = generalSkillTraceDetail(item.data, phase);
          const existing = trace.lines.find((line) => line.id === id);
          const previousOutput = existing?.output || existing?.detail || '';
          const detail = isOutputChunk && previousOutput && rawDetail
            ? `${previousOutput}${rawDetail}`
            : rawDetail;
          const outputInfo = generalSkillTraceOutput(item.data, phase, detail);
          const codePhases = new Set([
            'plan_created',
            'attempt_started',
            'running_code',
            'stdout_chunk',
            'stderr_chunk',
            'code_finished',
            'code_timeout',
          ]);
          const runningPhases = new Set([
            'planning',
            'repair_planning',
            'attempt_started',
            'running_code',
            'reflection_reviewing',
            'replying',
          ]);
          upsertTraceLine(traceTurnId, {
            id,
            kind: codePhases.has(phase) ? 'code' : 'decision',
            text,
            detail: outputInfo.output ? undefined : detail,
            code: code || undefined,
            language: code ? (runtime === 'bash' ? 'bash' : 'python') : undefined,
            output: outputInfo.output,
            outputLanguage: outputInfo.language,
            outputTitle: outputInfo.title,
            state: runningPhases.has(phase) ? 'running' : phase.includes('failed') || phase === 'code_timeout' ? 'failed' : 'completed',
            collapsible: Boolean(code || outputInfo.output),
          });
          return;
        }
        if (item.event === 'knowledge_result') {
          upsertTraceLine(traceTurnId, {
            id: 'knowledge_lookup',
            kind: 'knowledge',
            text: '读取知识库',
            detail: knowledgeResultTraceDetail(item.data),
            state: 'completed',
          });
          return;
        }
        if (item.event === 'tool_result') {
          const tool = normalizeTraceTool(item.data);
          if (tool) {
            upsertTraceLine(traceTurnId, {
              id: `tool_${tool.toolCallId || tool.rawToolName || tool.toolId}`,
              kind: 'tool',
              text: `${tool.isError ? '工具调用失败' : '调用工具'} ${tool.toolName}`,
              detail: toolTraceDetail(tool),
              state: tool.isError ? 'failed' : 'completed',
            });
          }
          return;
        }
        if (item.event === 'agent_loop_continued' || item.event === 'agent_loop_completed') {
          const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
            ? String(item.data.iteration)
            : '1';
          const targetTool = typeof item.data.target_tool_name === 'string' ? item.data.target_tool_name : '';
          upsertTraceLine(traceTurnId, {
            id: `decision_stepping_tool_continuation_${iteration}`,
            kind: 'decision',
            text: '重新分析',
            detail: item.event === 'agent_loop_continued'
              ? (targetTool ? `决定继续调用 ${targetTool}` : '决定继续调用工具')
              : '判断无需继续调用工具',
            state: 'completed',
          });
          return;
        }
        if (item.event === 'reflection_decision') {
          const needsRetry = item.data.needs_retry === true;
          const skipped = item.data.skipped === true;
          upsertTraceLine(traceTurnId, {
            id: 'reflection',
            kind: 'decision',
            text: skipped ? '反思已关闭' : needsRetry ? '反思后继续尝试' : '反思通过',
            detail: reflectionTraceDetail(item.data),
            state: 'completed',
          });
          return;
        }
        if (item.event === 'status') {
          const eventStream = getStreamSlot(eventSessionId);
          const phase = typeof item.data.phase === 'string' ? item.data.phase : 'thinking';
          if (phase === 'responding') {
            notifyStream();
            return;
          }
          eventStream.phase = publicStreamPhase(item.data);
          if (phase === 'tool' && typeof item.data.tool_name === 'string') {
            const toolCallId = typeof item.data.tool_call_id === 'string' ? item.data.tool_call_id : item.data.tool_name;
            upsertTraceLine(traceTurnId, {
              id: `tool_${toolCallId}`,
              kind: 'tool',
              text: `正在调用 ${item.data.tool_name}`,
              state: 'running',
            });
          } else if (phase === 'routing') {
            upsertTraceLine(traceTurnId, { id: 'decision_router', kind: 'decision', text: '判断意图', state: 'running' });
          } else if (isKnowledgeTracePhase(phase)) {
            upsertTraceLine(traceTurnId, {
              id: 'knowledge_lookup',
              kind: 'knowledge',
              text: knowledgeTraceText(item.data),
              detail: knowledgeTraceDetail(item.data),
              state: phase === 'evidence_pack' || phase.startsWith('no_') || phase === 'okf_only' ? 'completed' : 'running',
            });
          } else if (phase === 'stepping') {
            const repairReason = typeof item.data.repair_reason === 'string' ? item.data.repair_reason : 'main';
            const iteration = typeof item.data.iteration === 'number' || typeof item.data.iteration === 'string'
              ? `_${item.data.iteration}`
              : '';
            upsertTraceLine(traceTurnId, {
              id: `decision_stepping_${repairReason}${iteration}`,
              kind: 'decision',
              text: repairReason === 'main' ? '决定下一步' : '重新分析',
              state: 'running',
            });
          } else if (phase === 'reflecting') {
            upsertTraceLine(traceTurnId, { id: 'reflection', kind: 'decision', text: '正在反思', state: 'running' });
          } else if (phase === 'scheduled_task_draft') {
            upsertTraceLine(traceTurnId, {
              id: 'scheduled_task_draft',
              kind: 'decision',
              text: '生成定时任务草案',
              detail: '来自本条消息的定时任务，等待用户确认后启用',
              state: 'completed',
            });
          } else if (phase !== 'received') {
            upsertTraceLine(traceTurnId, {
              id: `decision_status_${phase}`,
              kind: 'decision',
              text: eventStream.phase,
              state: 'running',
            });
          }
          notifyStream();
          return;
        }
        if (item.event === 'stream_replace') {
          const next = typeof item.data.content === 'string' ? item.data.content : '';
          if (eventStream.timer) {
            window.clearTimeout(eventStream.timer);
            eventStream.timer = null;
          }
          eventStream.accumulated = next;
          updateStreaming(eventSessionId, next, eventStream.turnId || traceTurnId);
          notifyStream();
          return;
        }
        if (item.event === 'stream_delta' || item.event === 'token') {
          const piece = typeof item.data.content === 'string' ? item.data.content : '';
          if (!piece) return;
          const wasEmpty = !eventStream.accumulated;
          eventStream.accumulated += piece;
          if (wasEmpty) {
            updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
            notifyStream();
            return;
          }
          if (!eventStream.timer) {
            eventStream.timer = window.setTimeout(() => {
              eventStream.timer = null;
              updateStreaming(eventSessionId, eventStream.accumulated, eventStream.turnId || traceTurnId);
            }, 100);
          }
          return;
        }
        if (item.event === 'stream_end') {
          markStreamTerminal();
          finishTrace(traceTurnId);
          upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
          finalizeStreaming(eventSessionId);
          const eventStream = getStreamSlot(eventSessionId);
          eventStream.loading = false;
          eventStream.phase = '';
          eventStream.abortController = null;
          setRunningTurn((current) => (
            current?.sessionId === eventSessionId && (current.turnId === turnId || current.turnId === traceTurnId) ? null : current
          ));
          notifyStream();
          return;
        }
        if (item.event === 'stream_cancelled') {
          markStreamTerminal();
          finishTrace(traceTurnId, true);
          clearStreamSlot(eventSessionId, false);
          upsertTraceStatusPlaceholder(getSlot(eventSessionId), eventSessionId, traceTurnId);
          notifyStore();
          setRunningTurn((current) => (
            current?.sessionId === eventSessionId && (current.turnId === turnId || current.turnId === traceTurnId) ? null : current
          ));
          notifyStream();
          return;
        }
        if (item.event === 'complete' || item.event === 'done') {
          markStreamTerminal();
          const result = item.data as ChatTurnResponse;
          const completedSessionId = result.session_id || createdSessionId || String(item.data.sessionId || '');
          const userIntent = typeof result.router_decision?.user_intent === 'string' ? result.router_decision.user_intent : '';
          const decisionReason = typeof result.router_decision?.reason === 'string' ? result.router_decision.reason : '';
          if (userIntent || decisionReason) {
            upsertTraceLine(traceTurnId, {
              id: 'decision_router',
              kind: 'decision',
              text: userIntent ? `判断意图 ${userIntent}` : '完成技能判断',
              detail: decisionReason || undefined,
              state: 'completed',
            });
          }
          finishTrace(traceTurnId);
          upsertTraceLine(traceTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
          finalizeStreaming(eventSessionId);
          setLastTurn(result);
          eventStream.loading = false;
          eventStream.phase = '';
          eventStream.abortController = null;
          setRunningTurn((current) => (
            current?.sessionId === eventSessionId && (current.turnId === turnId || current.turnId === traceTurnId) ? null : current
          ));
          notifyStream();
          if (isDraftConversation && completedSessionId) {
            promoteDraftConversation(completedSessionId);
          } else {
            loadSessions();
          }
          window.setTimeout(() => {
            const persistedSessionId = completedSessionId || eventSessionId;
            loadMessages(persistedSessionId);
            loadTraces(persistedSessionId);
          }, 250);
        }
      }, controller.signal);
      clearStreamWatchdog();
      if (!receivedTerminalEvent && !controller.signal.aborted) {
        const activeStream = getStreamSlot(liveConversationId);
        const activeTurnId = activeStream.turnId || turnId;
        if (activeStream.accumulated) {
          finishTrace(activeTurnId);
          upsertTraceLine(activeTurnId, { id: 'thinking', kind: 'thinking', text: '执行记录', state: 'completed' });
          finalizeStreaming(liveConversationId);
          activeStream.loading = false;
          activeStream.phase = '';
          activeStream.abortController = null;
          clearRunningTurn(liveConversationId);
          notifyStream();
          loadSessions();
          window.setTimeout(() => {
            loadMessages(liveConversationId);
            loadTraces(liveConversationId);
          }, 250);
        } else {
          appendInterruptedResponse('本次响应中断，未收到模型回复。请重试发送。');
        }
      }
    } catch (error) {
      clearStreamWatchdog();
      if (controller.signal.aborted) {
        return;
      }
      if (isAuthError(error)) {
        clearAuthSession();
        navigate('/login', { replace: true });
        finishTrace(getStreamSlot(liveConversationId).turnId || turnId, true);
        clearStreamSlot(liveConversationId, true);
        clearRunningTurn();
        notifyStream();
        return;
      }
      const activeTurnId = getStreamSlot(liveConversationId).turnId || turnId;
      clearStreamSlot(liveConversationId, true);
      appendRealtime(liveConversationId, {
        id: `error_${Date.now()}`,
        turnId: activeTurnId,
        role: 'assistant',
        content: '发送失败，请稍后重试',
        created_at: new Date().toISOString(),
        isError: true,
      });
      notifyRequestError('send', error, '发送失败');
      finishTrace(activeTurnId, true);
      clearRunningTurn();
      notifyStream();
    } finally {
      clearStreamWatchdog();
      if (stream.abortController === controller) {
        stream.abortController = null;
        stream.loading = false;
        stream.phase = '';
        clearRunningTurn();
        notifyStream();
      }
    }
  }

  function handleComposerPlusAction(action: 'upload' | 'scheduled_task') {
    setComposerPlusOpen(false);
    if (action === 'upload') {
      fileInputRef.current?.click();
      return;
    }
    setComposerIntent('scheduled_task');
  }

  return (
    <div className={`chat-layout ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
      <aside className="session-pane">
        <div className="sidebar-head">
          <Button
            className="icon-button"
            icon={<StaffdeckIcon name="calculator" style={{ transform: 'rotate(-90deg)' }} />}
            aria-label={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
            onClick={toggleSidebar}
          />
          <div className="brand-block">
            <span className="brand-mark">SD</span>
            <div>
              <div className="brand-title">Modelbest</div>
              <div className="brand-subtitle">UltraRAG4</div>
            </div>
          </div>
          <div className="sidebar-actions">
            <Button
              className="icon-button sidebar-logout"
              icon={<StaffdeckIcon name="logout" />}
              onClick={() => {
                clearAuthSession();
                navigate('/login', { replace: true });
              }}
            />
          </div>
        </div>
        {!sidebarCollapsed && (
          <div className="sidebar-workspace-panel">
            <button type="button" className="sidebar-gallery-entry" onClick={() => navigate('/employees')}>
              <span className="sidebar-gallery-entry-icon"><StaffdeckIcon name="globe" /></span>
              <span className="sidebar-gallery-entry-copy">
                <strong>数字员工广场</strong>
                <span>选择数字员工</span>
              </span>
              <StaffdeckIcon name="arrow" />
            </button>
            <div className="session-filter-bar">
              <span className="session-filter-label">员工会话</span>
              <Select
                size="small"
                className="session-filter-select"
                value={sessionAgentFilter}
                options={sessionFilterOptions}
                onChange={setSessionAgentFilter}
              />
            </div>
          </div>
        )}
        <div className="session-list-scroll chat-agent-list">
          <div className="session-section-label">{sidebarCollapsed ? '会话' : '员工会话'}</div>
          {visibleSidebarSessions.length === 0 ? (
            <div className="session-list-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史会话" />
            </div>
          ) : visibleSidebarSessions.map((session) => {
            const sessionAgent = session.agent_id ? agents.find((agent) => agent.id === session.agent_id) || null : null;
            const profile = sessionAgent ? employeeProfile(sessionAgent) : null;
            const title = staffdeckDisplayText(session.title || session.summary || session.last_agent_question || '新会话');
            const summary = staffdeckDisplayText(session.summary || session.last_agent_question || '新会话');
            const hasUnread = sessionHasUnreadReply(session, sessionReadTimes, sessionId);
            const openSession = () => {
              navigate(`/${session.id}`);
            };
            return (
              <div
                key={session.id}
                role="button"
                tabIndex={0}
                data-session-id={session.id}
                className={`session-card chat-agent-card ${session.id === sessionId ? 'active' : ''} ${hasUnread ? 'unread' : ''}`}
                onClick={openSession}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    openSession();
                  }
                }}
              >
                <div className="session-card-content">
                  <span className="session-title-icon session-title-avatar">
                    {profile ? (
                      <EmployeeAvatarMark
                        profile={profile}
                        fallback={sessionAgent ? employeeDisplayName(sessionAgent).slice(0, 1) : '员'}
                        className="session-agent-avatar"
                      />
                    ) : (
                      <StaffdeckIcon name="chat" />
                    )}
                  </span>
                  <div className="session-meta">
                    <div className="session-title" title={title}>
                      <span className="session-title-text">{title}</span>
                    </div>
                    <div className="session-summary" title={summary}>
                      {summary}
                    </div>
                  </div>
                  {hasUnread && <span className="session-unread-dot" aria-label="未读回复" />}
                  <div className="session-actions">
                    <Button
                      className="session-action"
                      size="small"
                      type="text"
                      icon={<StaffdeckIcon name="edit" />}
                      aria-label="重命名会话"
                      onClick={(event) => openRename(event, session)}
                    />
                    <Button
                      className="session-action danger"
                      size="small"
                      type="text"
                      icon={<StaffdeckIcon name="trash" />}
                      aria-label="删除会话"
                      onClick={(event) => confirmDelete(event, session)}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <button type="button" className="sidebar-bottom-link" onClick={() => { window.location.href = '/enterprise/dashboard'; }}>
          <StaffdeckIcon name="chat" />
          <span>管理端</span>
          <StaffdeckIcon name="arrow" />
        </button>
      </aside>
      <main className="chat-main">
        <div className="chat-header">
          <div className="chat-title-stack">
            <span className="chat-title-name">
              {displayedProfile?.roleName || (displayedAgent ? employeeDisplayName(displayedAgent) : '研发')}
            </span>
            <StaffdeckIcon name="edit" />
            <span className="chat-title-meta">在线客服</span>
          </div>
          <div className="chat-header-actions">
            <ThemeToggleButton />
            <Button
              className="icon-button"
              icon={<StaffdeckIcon name="logout" />}
              aria-label="退出聊天"
              onClick={() => { window.location.href = '/enterprise/dashboard'; }}
            />
          </div>
        </div>
        <div className="chat-messages" ref={chatMessagesRef} onScroll={handleChatMessagesScroll}>
          {displayedMessages.length === 0 && (
            <div className="chat-empty-state staffdeck-empty-state">
              <EmployeeAvatarMark profile={displayedProfile} fallback="SD" className="staffdeck-empty-avatar" />
              <div className="staffdeck-empty-copy">
                <strong>Hello {displayedAgent ? employeeDisplayName(displayedAgent) : 'Jessie'}!</strong>
                <span>我们来做什么？</span>
              </div>
              <div className="staffdeck-empty-profile-card">
                <div className="staffdeck-empty-bio">
                  <p>{emptyRoleSummary}</p>
                  <div>
                    {emptyProfileTags.map((tag) => (
                      <span key={tag}>{tag}</span>
                    ))}
                  </div>
                </div>
                <div className="staffdeck-empty-stats">
                  {emptyStats.map((item) => (
                    <span key={item.label}><b>{item.value}</b>{item.label}</span>
                  ))}
                </div>
              </div>
            </div>
          )}
          <div className="message-stack">
            {displayedMessages.map((item, itemIndex) => {
              const turnId = item.turnId || item.id;
              const fallbackTraceId = item.role === 'assistant' && item.isStreaming
                ? (currentStream.turnId || runningTurn?.turnId || '')
                : '';
              const primaryTrace = item.role === 'assistant' ? turnTraceRef.current.get(turnId) : undefined;
              const fallbackTrace = fallbackTraceId ? turnTraceRef.current.get(fallbackTraceId) : undefined;
              const trace = primaryTrace || fallbackTrace;
              const traceTurnId = primaryTrace ? turnId : (fallbackTrace ? fallbackTraceId : turnId);
              const traceLines = trace?.lines || [];
              const allowedTrace = traceLines.filter((line) => traceLineAllowed(line, uiConfig));
              const forceRunningTrace = Boolean(
                item.role === 'assistant'
                && item.isStreaming
                && allowedTrace.length === 0
                && traceLines.some((line) => line.state === 'running')
              );
              const visibleTrace = forceRunningTrace ? traceLines : allowedTrace;
              const summary = trace && visibleTrace.length > 0 ? traceSummary(trace, visibleTrace) : null;
              const details = traceDetails(visibleTrace);
              const traceActive = isCurrentStreamingTrace(traceTurnId, item);
              const summaryForRender = summary && traceActive && !trace?.completedAt
                ? { ...summary, state: 'running' as const }
                : summary;
              const traceOnlyMessage = Boolean(
                item.role === 'assistant'
                && !normalizeMessageText(item.content)
                && details.length > 0
              );
              const latestAssistantTrace = Boolean(
                item.role === 'assistant'
                && details.length > 0
                && !displayedMessages.slice(itemIndex + 1).some((later) => (
                  later.role === 'assistant'
                  && Boolean(turnTraceRef.current.get(later.turnId || later.id)?.lines.length)
                ))
              );
              const recentlyStartedTrace = Boolean(trace?.startedAt && Date.now() - trace.startedAt <= CHAT_TRACE_RECOVERY_WINDOW_MS);
              const defaultExpanded = Boolean(
                traceActive
                || summaryForRender?.state === 'running'
                || traceOnlyMessage
                || (latestAssistantTrace && recentlyStartedTrace)
              );
              const expanded = Boolean(
                expandedTraceIds.includes(traceTurnId)
                || (defaultExpanded && !collapsedTraceIds.includes(traceTurnId))
              );
              const visibleContent = staffdeckDisplayText(item.role === 'assistant'
                ? stripTrailingCitationSummary(item.content)
                : item.content);
              const citations = item.role === 'assistant' ? knowledgeCitations(item, visibleContent) : [];
              const scheduledTaskPrompt = isScheduledTaskPrompt(item);
              const scheduledDraft = item.role === 'assistant' && !dismissedDraftMessageIds.includes(item.id)
                ? scheduledDraftForMessage(item)
                : null;
              const persistedCreatedTask = item.role === 'assistant'
                ? createdScheduledTaskForMessage(item)
                : undefined;
              const stoppedStatusOnly = Boolean(
                item.role === 'system'
                && item.id.startsWith('local_interrupt_')
                && visibleContent === '已停止生成'
              );
              const attachments = messageAttachments(item);
              const statusOnly = stoppedStatusOnly;
              const statusOnlyText = visibleContent;
              const showInlineTrace = Boolean(summaryForRender && !stoppedStatusOnly);
              if (
                item.role === 'assistant'
                && !visibleContent
                && !showInlineTrace
                && !statusOnly
                && !scheduledDraft
                && !persistedCreatedTask
                && citations.length === 0
                && !item.isStreaming
              ) {
                return null;
              }
              void traceTick;
              const messageNode = (
                <div key={`${item.id}:message`} className={`message-item ${item.role}${statusOnly ? ' status-only-item' : ''}`}>
                  <div className={`message-row ${item.role} ${item.isError ? 'error' : ''}`}>
                    <div className={`bubble ${showInlineTrace ? 'has-trace' : ''}${statusOnly ? ' status-only' : ''}`}>
                      {statusOnly ? (
                        <div className="assistant-running-status">{statusOnlyText}</div>
                      ) : showInlineTrace && summaryForRender && (
                        renderAssistantTrace(traceTurnId, summaryForRender, details, expanded)
                      )}
                      {!statusOnly && visibleContent ? (
                        item.role === 'assistant' ? (
                          <MarkdownMessage content={visibleContent} />
                        ) : (
                          <div className="plain-answer">
                            {scheduledTaskPrompt && (
                              <span className="message-mode-chip">
                                <StaffdeckIcon name="clock" />
                                定时任务
                              </span>
                            )}
                            <span>{visibleContent}</span>
                          </div>
                        )
                      ) : !statusOnly && item.role === 'assistant' && item.isStreaming && !summary ? (
                        <span className="typing-caret" />
                      ) : null}
                      {!statusOnly && attachments.length > 0 && (
                        <div className="chat-attachment-list">
                          {attachments.map((attachment) => (
                            <div className={`chat-attachment-card ${attachment.kind}`} key={attachment.id}>
                              {attachment.kind === 'image' && attachment.data_url ? (
                                <img src={attachment.data_url} alt={attachment.filename} />
                              ) : (
                                <span className="chat-attachment-file-icon">
                                  <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} />
                                </span>
                              )}
                              <span className="chat-attachment-copy">
                                <span className="chat-attachment-name">{attachment.filename}</span>
                                <span className="chat-attachment-meta">
                                  {attachmentTypeLabel(attachment)}
                                  {attachment.error ? ` · ${attachment.error}` : ''}
                                </span>
                              </span>
                            </div>
                          ))}
                        </div>
                      )}
                      {item.role === 'assistant' && citations.length > 0 && (
                        <div className="message-citations" aria-label="知识引用">
                          <div className="citation-heading">
                            <StaffdeckIcon name="file" />
                            <span>知识来源</span>
                          </div>
                          <div className="citation-list">
                            {citations.map((citation) => (
                              <button
                                key={citation.id}
                                type="button"
                                className="citation-chip"
                                onClick={() => setActiveCitation(citation)}
                              >
                                <span className="citation-index">{citation.label || citation.id}</span>
                                <span className="citation-title">{citationDisplayTitle(citation)}</span>
                              </button>
                            ))}
                          </div>
                        </div>
                      )}
                      {scheduledDraft && (
                        <ScheduledDraftCard
                          draft={scheduledDraft}
                          createdTask={createdScheduledTasks[item.id] || persistedCreatedTask}
                          onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
                          onDismiss={() => dismissScheduledTaskDraft(item.id)}
                        />
                      )}
                      {canRateMessage(item) && (
                        <div className="message-feedback">
                          <Button
                            type="text"
                            size="small"
                            className={item.feedback_rating === 'up' ? 'active' : ''}
                            icon={<StaffdeckIcon name="thumb-up" />}
                            aria-label="点赞"
                            onClick={() => rateMessage(item, 'up')}
                          />
                          <Button
                            type="text"
                            size="small"
                            className={item.feedback_rating === 'down' ? 'active danger' : ''}
                            icon={<StaffdeckIcon name="thumb-down" />}
                            aria-label="点踩"
                            onClick={() => rateMessage(item, 'down')}
                          />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
              return messageNode;
            })}
            {showFallbackRunningStatus && (
              <div className="message-item assistant running-trace-item">
                <div className="message-row assistant">
                  <div className="bubble has-trace">
                    {fallbackTraceSummaryForRender && (
                      renderAssistantTrace(
                        fallbackRunningTraceId,
                        fallbackTraceSummaryForRender,
                        fallbackTraceDetails,
                        fallbackTraceExpanded,
                      )
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
          {currentScheduledDraft && !hasVisibleMessageScheduledDraft && (
            <ScheduledDraftCard
              draft={currentScheduledDraft}
              createdTask={activeConversationId ? createdScheduledTasks[`session:${activeConversationId}`] : undefined}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft)}
              onDismiss={() => dismissScheduledTaskDraft()}
            />
          )}
          {SHOW_DEBUG && lastTurn && <pre className="debug-panel">{JSON.stringify(lastTurn.session_state, null, 2)}</pre>}
        </div>
        <div className="chat-input">
          <div className="composer-stage">
            {showComposerAvatar && displayedProfile && (
              <EmployeeAvatarMark profile={displayedProfile} fallback="SD" className="chat-composer-avatar" />
            )}
            <form
              className={`composer-v2${composerActive ? ' composer-active' : ''}${composerDragActive ? ' drag-active' : ''}`}
              onDragEnter={handleComposerDragEnter}
              onDragOver={handleComposerDragOver}
              onDragLeave={handleComposerDragLeave}
              onDrop={handleComposerDrop}
              onSubmit={(event) => {
                event.preventDefault();
                send();
              }}
            >
            <input
              ref={fileInputRef}
              className="composer-file-input"
              type="file"
              multiple
              onChange={handleComposerFileChange}
            />
            {composerDragActive && <div className="composer-drop-hint">松开上传文件</div>}
            {composerAttachments.length > 0 && (
              <div className="composer-attachment-list">
                {composerAttachments.map((attachment) => (
                  <div className={`composer-attachment-chip ${attachment.uploadStatus}`} key={attachment.uploadKey}>
                    {attachment.kind === 'image' && attachment.data_url ? (
                      <img src={attachment.data_url} alt={attachment.filename} />
                    ) : (
                      <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} />
                    )}
                    <span className="composer-attachment-copy">
                      <span className="composer-attachment-name">{attachment.filename}</span>
                      <span className="composer-attachment-status">
                        {attachment.uploadStatus === 'uploading' && '解析中'}
                        {attachment.uploadStatus === 'ready' && attachmentTypeLabel(attachment)}
                        {attachment.uploadStatus === 'error' && (attachment.error || '上传失败')}
                      </span>
                    </span>
                    <button
                      type="button"
                      className="composer-attachment-remove"
                      onClick={() => removeComposerAttachment(attachment.uploadKey)}
                      aria-label="移除附件"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
            <Input.TextArea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onPaste={handleComposerPaste}
              onPressEnter={(event) => {
                const nativeEvent = event.nativeEvent as KeyboardEvent & { isComposing?: boolean };
                if (!event.shiftKey && !isComposing && !nativeEvent.isComposing && nativeEvent.keyCode !== 229) {
                  event.preventDefault();
                  send();
                }
              }}
              onCompositionStart={() => setIsComposing(true)}
              onCompositionEnd={() => window.setTimeout(() => setIsComposing(false), 0)}
              autoSize={{ minRows: 2, maxRows: 8 }}
              placeholder="输入消息，按 Enter 发送..."
            />
            <div className="composer-toolbar">
              <div className="composer-context-row">
                <Dropdown
                  trigger={['click']}
                  placement="topLeft"
                  overlayClassName="composer-plus-dropdown"
                  open={composerPlusOpen}
                  onOpenChange={setComposerPlusOpen}
                  menu={{
                    items: [
                      {
                        key: 'upload',
                        label: (
                          <button
                            type="button"
                            className="composer-plus-menu-item"
                            data-composer-action="upload"
                            onClick={(event) => {
                              event.preventDefault();
                              handleComposerPlusAction('upload');
                            }}
                          >
                            <StaffdeckIcon name="upload" />
                            <span>上传文件</span>
                          </button>
                        ),
                      },
                      {
                        key: 'scheduled_task',
                        label: (
                          <button
                            type="button"
                            className="composer-plus-menu-item"
                            data-composer-action="scheduled_task"
                            onClick={(event) => {
                              event.preventDefault();
                              handleComposerPlusAction('scheduled_task');
                            }}
                          >
                            <StaffdeckIcon name="clock" />
                            <span>定时任务</span>
                          </button>
                        ),
                      },
                    ],
                  }}
                >
                  <Button
                    type="text"
                    htmlType="button"
                    className="composer-plus-button"
                    icon={<StaffdeckIcon name="plus" />}
                    disabled={currentSessionRunning}
                    aria-label="添加"
                    title="添加"
                  />
                </Dropdown>
                {composerIntent === 'scheduled_task' && (
                  <button
                    type="button"
                    className="composer-intent-chip"
                    onClick={() => setComposerIntent(null)}
                    aria-label="移除定时任务"
                    title="移除定时任务"
                  >
                    <span className="composer-intent-icon">
                      <StaffdeckIcon name="clock" size={14} />
                    </span>
                    <span>定时任务</span>
                  </button>
                )}
                <div className="composer-hint">Enter 发送 / Shift+Enter 换行</div>
              </div>
              <div className="composer-actions-row">
                <Dropdown
                  trigger={['click']}
                  placement="topRight"
                  overlayClassName="composer-model-dropdown"
                  menu={{
                    items: modelMenuItems,
                    onClick: ({ key }) => {
                      if (key !== 'empty') changeModelConfig(String(key));
                    },
                  }}
                >
                  <Button
                    type="text"
                    htmlType="button"
                    className="composer-model-button"
                    disabled={currentSessionRunning || !enabledModelConfigs.length}
                  >
                    <span className="composer-model-label">
                      {selectedModelConfig ? modelDisplayName(selectedModelConfig) : '默认模型'}
                    </span>
                    <StaffdeckIcon name="arrow" style={{ transform: 'rotate(90deg)' }} />
                  </Button>
                </Dropdown>
                <Button
                  type="primary"
                  htmlType={currentSessionRunning ? 'button' : 'submit'}
                  icon={<StaffdeckIcon name={currentSessionRunning ? 'stop' : 'send'} />}
                  onMouseDown={currentSessionRunning ? (event) => {
                    event.preventDefault();
                    abortStream();
                  } : undefined}
                  onClick={currentSessionRunning ? abortStream : undefined}
                  className={`composer-send-button${currentSessionRunning ? ' stop-button' : ''}`}
                  disabled={!currentSessionRunning && ((!input.trim() && readyComposerAttachments.length === 0) || uploadingComposerAttachment)}
                  aria-label={currentSessionRunning ? '停止生成' : '发送'}
                />
              </div>
            </div>
            </form>
          </div>
        </div>
      </main>
      <Modal
        className="handoff-inbox-modal"
        title="待回答"
        width="min(920px, calc(100vw - 40px))"
        open={showHandoffInbox}
        footer={null}
        onCancel={() => setShowHandoffInbox(false)}
      >
        {handoffs.length === 0 ? (
          <Empty description={handoffsLoading ? '正在加载待回答消息' : '暂无待回答消息'} />
        ) : (
          <div className="handoff-inbox-list">
            {handoffs.map((handoff) => {
              const handoffAgent = handoff.agent_id
                ? agents.find((item) => item.id === handoff.agent_id) || null
                : displayedAgent;
              const profile = handoffAgent ? employeeProfile(handoffAgent) : displayedProfile;
              return (
                <article className="handoff-inbox-card" key={handoff.id}>
                  <div className="handoff-inbox-card-head">
                    {profile ? <EmployeeAvatarMark profile={profile} className="handoff-inbox-avatar" /> : null}
                    <div>
                      <strong>{handoffAgent ? employeeDisplayName(handoffAgent) : '数字员工'}</strong>
                      <span>需要人工接续</span>
                    </div>
                  </div>
                  <div className="handoff-inbox-block">
                    <span>上下文摘要</span>
                    <p>{handoff.context_summary || '暂无上下文摘要'}</p>
                  </div>
                  <div className="handoff-inbox-block">
                    <span>这一步需要你处理</span>
                    <p>{handoff.pending_question || '请根据当前会话补充人工回复。'}</p>
                  </div>
                  <Input.TextArea
                    autoSize={{ minRows: 3, maxRows: 6 }}
                    value={handoffReplies[handoff.id] || ''}
                    placeholder="以当前数字员工的口吻回复。提交后，原会话会继续推进技能流程。"
                    onChange={(event) => setHandoffReplies((prev) => ({
                      ...prev,
                      [handoff.id]: event.target.value,
                    }))}
                  />
                  <div className="handoff-inbox-actions">
                    <Button onClick={() => navigate(`/${handoff.session_id}`)}>打开原会话</Button>
                    <Button type="primary" onClick={() => submitHandoffReply(handoff)}>回复并恢复</Button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Modal>
      <Modal
        className="knowledge-citation-modal"
        title="引用详情"
        width="min(1160px, calc(100vw - 40px))"
        open={Boolean(activeCitation)}
        footer={null}
        onCancel={() => setActiveCitation(null)}
      >
        {activeCitation && (
          <div className="citation-detail">
            <div className="citation-detail-eyebrow">{citationKindLabel(activeCitation)}</div>
            <h3>{citationDisplayTitle(activeCitation)}</h3>
            {(activeCitation.summary || activeCitation.excerpt) && (
              <div className="citation-detail-section">
                <span>引用内容</span>
                <p>{activeCitation.summary || activeCitation.excerpt}</p>
              </div>
            )}
            {activeCitation.summary && activeCitation.excerpt && (
              <div className="citation-detail-section">
                <span>引用来源</span>
                <blockquote>{activeCitation.excerpt}</blockquote>
              </div>
            )}
            {(activeCitation.source_path || activeCitation.section_path || activeCitation.concept_id) && (
              <div className="citation-detail-grid">
                {activeCitation.source_path && (
                  <div>
                    <span>来源</span>
                    <strong>{citationSourceLabel(activeCitation)}</strong>
                  </div>
                )}
                {activeCitation.section_path && (
                  <div>
                    <span>章节</span>
                    <strong>{citationSectionLabel(activeCitation)}</strong>
                  </div>
                )}
                {activeCitation.concept_id && (
                  <div>
                    <span>知识图谱</span>
                    <strong>{activeCitation.concept_id}</strong>
                  </div>
                )}
              </div>
            )}
            {activeCitation.confidence_reason && (
              <div className="citation-detail-note">{activeCitation.confidence_reason}</div>
            )}
          </div>
        )}
      </Modal>
      <Modal
        title="重命名"
        open={Boolean(renameSession)}
        okText="保存"
        cancelText="取消"
        onOk={saveRename}
        onCancel={() => {
          setRenameSession(null);
          setRenameTitle('');
        }}
      >
        <Input
          autoFocus
          maxLength={80}
          value={renameTitle}
          onChange={(event) => setRenameTitle(event.target.value)}
          onPressEnter={saveRename}
          placeholder="输入会话名称"
        />
      </Modal>
    </div>
  );
}

function toRequestAttachment(attachment: ComposerAttachment): ChatAttachmentRead {
  const { uploadStatus: _uploadStatus, uploadKey: _uploadKey, ...rest } = attachment;
  return rest;
}

function messageAttachments(messageItem: ChatMessage): ChatAttachmentRead[] {
  const attachments = messageItem.metadata?.attachments;
  if (!Array.isArray(attachments)) return [];
  return attachments.filter(isChatAttachment);
}

function isChatAttachment(value: unknown): value is ChatAttachmentRead {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<ChatAttachmentRead>;
  return typeof item.id === 'string' && typeof item.filename === 'string';
}

function attachmentTypeLabel(attachment: ChatAttachmentRead): string {
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

function formatDraftSchedule(draft: ScheduledTaskDraftRead): string {
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
    const date = value ? new Date(value) : null;
    return date && !Number.isNaN(date.getTime())
      ? `一次性 ${date.toLocaleString('zh-CN', { hour12: false })}`
      : '一次性';
  }
  return `每天 ${schedule.time || '09:00'}`;
}

function scheduleTypeLabel(type: ScheduledTaskDraftRead['schedule_type']): string {
  return DRAFT_SCHEDULE_TYPE_LABELS[normalizeDraftScheduleType(type)];
}

function scheduleEditValue(draft: ScheduledTaskDraftRead): string {
  const schedule = draft.schedule || {};
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') return String(schedule.run_at || '');
  return String(schedule.time || '09:00');
}

function scheduleFromEditValue(draft: ScheduledTaskDraftRead, value: string): Record<string, unknown> {
  if (normalizeDraftScheduleType(draft.schedule_type) === 'once') {
    return { ...(draft.schedule || {}), run_at: value };
  }
  return { ...(draft.schedule || {}), time: value };
}

function draftScheduleForType(schedule: Record<string, unknown>, type: DraftScheduleType): Record<string, unknown> {
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

function normalizeDraftScheduleType(value: string): DraftScheduleType {
  const scheduleType = value as DraftScheduleType;
  return DRAFT_SCHEDULE_TYPES.has(scheduleType) ? scheduleType : 'daily';
}
