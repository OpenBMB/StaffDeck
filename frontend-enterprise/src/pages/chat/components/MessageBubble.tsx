import { Check, Copy } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import EmployeeAvatar from '@/components/EmployeeAvatar';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import IconThumbUp from '@/assets/icons/thumb-up.svg?react';
import IconThumbDown from '@/assets/icons/thumb-down.svg?react';
import { notify } from '@/components/ui/app-toast';
import { employeeDisplayName } from '@/employee';
import { copyTextToClipboard } from '@/lib/clipboard';
import { cn } from '@/lib/utils';
import type {
  ChatAttachmentRead,
  ChatMessage,
  HarnessWorkspaceArtifact,
  KnowledgeCitation,
  ScheduledTaskDraftRead,
  ScheduledTaskRead,
} from '@/types';

import {
  CHAT_ATTACHMENT_CARD_CLASS,
  CHAT_ATTACHMENT_COPY_CLASS,
  CHAT_ATTACHMENT_FILE_ICON_CLASS,
  CHAT_ATTACHMENT_IMG_CLASS,
  CHAT_ATTACHMENT_LIST_CLASS,
  CHAT_ATTACHMENT_META_CLASS,
  CHAT_ATTACHMENT_NAME_CLASS,
  CHAT_FEEDBACK_BTN_ACTIVE_CLASS,
  CHAT_FEEDBACK_BTN_CLASS,
  CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
  CHAT_FEEDBACK_CLASS,
  CHAT_GROUP_MESSAGE_AVATAR_CLASS,
  CHAT_GROUP_MESSAGE_CONTENT_CLASS,
  CHAT_GROUP_MESSAGE_LEADER_BADGE_CLASS,
  CHAT_GROUP_MESSAGE_ROW_CLASS,
  CHAT_GROUP_MESSAGE_SENDER_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_MESSAGE_MODE_CHIP_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_SLASH_COMMAND_MESSAGE_CLASS,
  CHAT_SLASH_COMMAND_REQUEST_CLASS,
  CHAT_QUEUED_BUBBLE_CLASS,
  CHAT_QUEUED_DELETE_BTN_CLASS,
  CHAT_QUEUED_MESSAGE_ITEM_CLASS,
  CHAT_QUEUED_STATUS_CLASS,
  CHAT_QUEUED_STATUS_ROW_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';
import {
  MarkdownMessage,
  attachmentTypeLabel,
  canRateMessage,
} from '../chatHelpers';
import type { TraceLine } from '../chatTypes';
import type { UseChatSession } from '../useChatSession';
import ExecutionRecord from './ExecutionRecord';
import HarnessArtifactDownloads from './HarnessArtifactDownloads';
import KnowledgeCitationList from './KnowledgeCitationList';
import ScheduledDraftCard from './ScheduledDraftCard';
import SlashCommandChip from './SlashCommandChip';
import { slashCommandMessage } from '../slashCommands';

export type MessageRender = {
  traceTurnId: string;
  summary: { text: string; state: TraceLine['state'] } | null;
  details: TraceLine[];
  expanded: boolean;
  showInlineTrace: boolean;
  visibleContent: string;
  citations: KnowledgeCitation[];
  scheduledDraft: ScheduledTaskDraftRead | null;
  createdTask?: ScheduledTaskRead;
  scheduledTaskPrompt: boolean;
  attachments: ChatAttachmentRead[];
  harnessArtifacts: HarnessWorkspaceArtifact[];
  statusOnly: boolean;
};

type MessageBubbleProps = {
  chat: UseChatSession;
  item: ChatMessage;
  render: MessageRender;
};

function activeTeamProgress(item: ChatMessage): { phase: string; statusText: string } | null {
  const progress = item.metadata?.team_progress;
  if (!progress || typeof progress !== 'object' || Array.isArray(progress)) return null;
  const progressRecord = progress as Record<string, unknown>;
  const phase = typeof progressRecord.phase === 'string' ? progressRecord.phase : '';
  if (phase !== 'collecting' && phase !== 'synthesizing') return null;
  const statusText = typeof progressRecord.status_text === 'string'
    ? progressRecord.status_text.trim()
    : '';
  return statusText ? { phase, statusText } : null;
}

export default function MessageBubble({ chat, item, render }: MessageBubbleProps) {
  const {
    toggleTrace,
    rateMessage,
    setActiveCitation,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    removeQueuedTurn,
  } = chat;
  const {
    traceTurnId,
    summary,
    details,
    expanded,
    showInlineTrace,
    visibleContent,
    citations,
    scheduledDraft,
    createdTask,
    scheduledTaskPrompt,
    attachments,
    harnessArtifacts,
    statusOnly,
  } = render;
  const queuedMessage = item.role === 'user' && item.metadata?.queued === true;
  const sentSlashCommand = item.role === 'user'
    ? slashCommandMessage(visibleContent, chat.slashCommands)
    : null;
  const groupAssistantMessage = item.role === 'assistant' && Boolean(chat.displayedTeam);
  const groupSenderName = chat.displayedAgent
    ? employeeDisplayName(chat.displayedAgent)
    : '项目领导';
  const teamProgress = item.role === 'assistant' ? activeTeamProgress(item) : null;

  const [copied, setCopied] = useState(false);
  const copyResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    // StrictMode 开发模式会先卸载再重新挂载一次同一组件来检测副作用问题；
    // 挂载阶段必须显式把标记复位为 true，否则该周期后 mountedRef 会永久停留在 false。
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current);
    };
  }, []);

  // 复制当前消息的可见文本到剪贴板；复制失败（如浏览器限制）时提示用户手动复制。
  // 每次成功复制都重新起算「已复制」提示的显示时长，避免连续点击时提示被上一次的计时器提前收起；
  // 复制期间组件若已卸载（如切换会话），则不再更新状态或创建悬空计时器。
  async function handleCopy() {
    try {
      await copyTextToClipboard(visibleContent);
      if (!mountedRef.current) return;
      setCopied(true);
      if (copyResetTimerRef.current) clearTimeout(copyResetTimerRef.current);
      copyResetTimerRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      if (!mountedRef.current) return;
      notify.error('复制失败，请手动选择文本复制');
    }
  }

  return (
    <div className={cn('group/message', CHAT_MESSAGE_ITEM_CLASS, queuedMessage && CHAT_QUEUED_MESSAGE_ITEM_CLASS)}>
      <div className={cn(chatRowClass(item.role), groupAssistantMessage && CHAT_GROUP_MESSAGE_ROW_CLASS)}>
        {groupAssistantMessage && (
          <EmployeeAvatar
            agent={chat.displayedAgent}
            size={36}
            radius={10}
            className={CHAT_GROUP_MESSAGE_AVATAR_CLASS}
          />
        )}
        <div className={groupAssistantMessage ? CHAT_GROUP_MESSAGE_CONTENT_CLASS : 'contents'}>
          {groupAssistantMessage && (
            <span className={CHAT_GROUP_MESSAGE_SENDER_CLASS} data-i18n-ignore>
              {groupSenderName}
              <span className={CHAT_GROUP_MESSAGE_LEADER_BADGE_CLASS}>项目领导</span>
            </span>
          )}
          <div
            className={cn(
              chatBubbleClass(item.role, item.isError),
              queuedMessage && CHAT_QUEUED_BUBBLE_CLASS,
            )}
          >
          {queuedMessage && (
            <button
              type="button"
              className={CHAT_QUEUED_DELETE_BTN_CLASS}
              aria-label="删除排队消息"
              title="删除排队消息"
              onClick={() => removeQueuedTurn(item.turnId || '')}
            >
              <StaffdeckIcon name="trash" size={14} />
            </button>
          )}
          {statusOnly ? (
            <div className="text-[13px] text-[#858b9c]">{visibleContent}</div>
          ) : showInlineTrace && summary ? (
            <ExecutionRecord
              traceTurnId={traceTurnId}
              summary={summary}
              details={details}
              expanded={expanded}
              onToggle={toggleTrace}
            />
          ) : null}

          {!statusOnly && visibleContent ? (
            item.role === 'assistant' ? (
              <div aria-live={teamProgress ? 'polite' : undefined} data-i18n-ignore>
                <MarkdownMessage content={visibleContent} />
                {teamProgress && (
                  <div
                    role="status"
                    aria-label={teamProgress.statusText}
                    className="mt-[8px] flex items-center gap-[7px] text-[12px] text-[#6d7791]"
                  >
                    <span className="relative flex size-[7px]">
                      <span className="absolute inline-flex size-full animate-ping rounded-full bg-[#1a71ff] opacity-40" />
                      <span className="relative inline-flex size-[7px] rounded-full bg-[#1a71ff]" />
                    </span>
                    <span>{teamProgress.statusText}</span>
                  </div>
                )}
              </div>
            ) : (
              <div className={cn(
                CHAT_PLAIN_ANSWER_CLASS,
                sentSlashCommand && CHAT_SLASH_COMMAND_MESSAGE_CLASS,
              )}
              >
                {scheduledTaskPrompt && (
                  <span className={CHAT_MESSAGE_MODE_CHIP_CLASS}>
                    <StaffdeckIcon name="clock" size={13} />
                    定时任务
                  </span>
                )}
                {sentSlashCommand ? (
                  <>
                    <SlashCommandChip command={sentSlashCommand.command} />
                    {sentSlashCommand.requestText && (
                      <span className={CHAT_SLASH_COMMAND_REQUEST_CLASS} data-i18n-ignore>
                        {sentSlashCommand.requestText}
                      </span>
                    )}
                  </>
                ) : (
                  <span data-i18n-ignore>{visibleContent}</span>
                )}
              </div>
            )
          ) : null}

          {!statusOnly && visibleContent && item.role === 'user' && (
            <div className={CHAT_FEEDBACK_CLASS}>
              <button
                type="button"
                className={CHAT_FEEDBACK_BTN_CLASS}
                aria-label={copied ? '已复制' : '复制'}
                title={copied ? '已复制' : '复制'}
                onClick={() => void handleCopy()}
              >
                {copied ? <Check size={15} /> : <Copy size={15} />}
              </button>
            </div>
          )}

          {!statusOnly && attachments.length > 0 && (
            <div className={CHAT_ATTACHMENT_LIST_CLASS}>
              {attachments.map((attachment) => (
                <div className={CHAT_ATTACHMENT_CARD_CLASS} key={attachment.id}>
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <span className={CHAT_ATTACHMENT_FILE_ICON_CLASS}>
                      <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={18} />
                    </span>
                  )}
                  <span className={CHAT_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_ATTACHMENT_NAME_CLASS} data-i18n-ignore>{attachment.filename}</span>
                    <span className={CHAT_ATTACHMENT_META_CLASS} data-i18n-ignore>
                      {attachmentTypeLabel(attachment)}
                      {attachment.error ? ` · ${attachment.error}` : ''}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          )}

          {item.role === 'assistant' && harnessArtifacts.length > 0 && (
            <HarnessArtifactDownloads
              artifacts={harnessArtifacts}
              tenantId={chat.tenantId}
              sessionId={chat.activeConversationId}
            />
          )}

          {item.role === 'assistant' && (
            <KnowledgeCitationList citations={citations} onOpen={setActiveCitation} />
          )}

          {scheduledDraft && (
            <ScheduledDraftCard
              draft={scheduledDraft}
              createdTask={createdTask}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
              onDismiss={() => dismissScheduledTaskDraft(item.id)}
            />
          )}

          {!statusOnly && visibleContent && item.role === 'assistant' && !item.isStreaming && !teamProgress && (
            <div className={CHAT_FEEDBACK_CLASS}>
              {canRateMessage(item) && (
                <>
                  <button
                    type="button"
                    className={cn(CHAT_FEEDBACK_BTN_CLASS, item.feedback_rating === 'up' && CHAT_FEEDBACK_BTN_ACTIVE_CLASS)}
                    aria-label="点赞"
                    onClick={() => rateMessage(item, 'up')}
                  >
                    <IconThumbUp width={15} height={15} />
                  </button>
                  <button
                    type="button"
                    className={cn(
                      CHAT_FEEDBACK_BTN_CLASS,
                      item.feedback_rating === 'down' && CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
                    )}
                    aria-label="点踩"
                    onClick={() => rateMessage(item, 'down')}
                  >
                    <IconThumbDown width={15} height={15} />
                  </button>
                </>
              )}
              <button
                type="button"
                className={CHAT_FEEDBACK_BTN_CLASS}
                aria-label={copied ? '已复制' : '复制'}
                title={copied ? '已复制' : '复制'}
                onClick={() => void handleCopy()}
              >
                {copied ? <Check size={15} /> : <Copy size={15} />}
              </button>
            </div>
          )}
          </div>
        </div>
      </div>
      {queuedMessage && (
        <div className={CHAT_QUEUED_STATUS_ROW_CLASS}>
          <span className={CHAT_QUEUED_STATUS_CLASS} role="status">
            <StaffdeckIcon name="clock" size={12} />
            排队中
          </span>
        </div>
      )}
    </div>
  );
}
