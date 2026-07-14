import StaffdeckIcon from '@/components/StaffdeckIcon';
import { cn } from '@/lib/utils';
import type {
  ChatAttachmentRead,
  ChatMessage,
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
  CHAT_BUBBLE_STREAMING_PLACEHOLDER_CLASS,
  CHAT_CITATION_CHIP_CLASS,
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATION_INDEX_CLASS,
  CHAT_CITATION_LIST_CLASS,
  CHAT_CITATION_TITLE_CLASS,
  CHAT_CITATIONS_CLASS,
  CHAT_FEEDBACK_BTN_ACTIVE_CLASS,
  CHAT_FEEDBACK_BTN_CLASS,
  CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS,
  CHAT_FEEDBACK_CLASS,
  CHAT_MESSAGE_ITEM_CLASS,
  CHAT_MESSAGE_MODE_CHIP_CLASS,
  CHAT_PLAIN_ANSWER_CLASS,
  CHAT_QUEUED_STATUS_CLASS,
  CHAT_QUEUED_STATUS_ROW_CLASS,
  CHAT_TYPING_CARET_CLASS,
  chatBubbleClass,
  chatRowClass,
} from '../chatPageStyles';
import {
  MarkdownMessage,
  attachmentTypeLabel,
  canRateMessage,
  citationDisplayTitle,
} from '../chatHelpers';
import type { TraceLine } from '../chatTypes';
import type { UseChatSession } from '../useChatSession';
import ExecutionRecord from './ExecutionRecord';
import ScheduledDraftCard from './ScheduledDraftCard';

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
  statusOnly: boolean;
  showTypingCaret: boolean;
};

type MessageBubbleProps = {
  chat: UseChatSession;
  item: ChatMessage;
  render: MessageRender;
};

export default function MessageBubble({ chat, item, render }: MessageBubbleProps) {
  const { toggleTrace, rateMessage, setActiveCitation, confirmScheduledTask, dismissScheduledTaskDraft } = chat;
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
    statusOnly,
    showTypingCaret,
  } = render;
  const queuedMessage = item.role === 'user' && item.metadata?.queued === true;
  const compactStreamingPlaceholder = Boolean(
    item.role === 'assistant' && showTypingCaret && !showInlineTrace && !visibleContent,
  );

  return (
    <div className={CHAT_MESSAGE_ITEM_CLASS}>
      <div className={chatRowClass(item.role)}>
        <div
          className={cn(
            chatBubbleClass(item.role, item.isError),
            compactStreamingPlaceholder && CHAT_BUBBLE_STREAMING_PLACEHOLDER_CLASS,
          )}
        >
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
              <div data-i18n-ignore>
                <MarkdownMessage content={visibleContent} />
              </div>
            ) : (
              <div className={CHAT_PLAIN_ANSWER_CLASS}>
                {scheduledTaskPrompt && (
                  <span className={CHAT_MESSAGE_MODE_CHIP_CLASS}>
                    <StaffdeckIcon name="clock" size={13} />
                    定时任务
                  </span>
                )}
                <span data-i18n-ignore>{visibleContent}</span>
              </div>
            )
          ) : showTypingCaret ? (
            <span className={CHAT_TYPING_CARET_CLASS} />
          ) : null}

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

          {item.role === 'assistant' && citations.length > 0 && (
            <div className={CHAT_CITATIONS_CLASS} aria-label="知识引用">
              <div className={CHAT_CITATION_HEADING_CLASS}>
                <StaffdeckIcon name="file" size={14} />
                <span>知识来源</span>
              </div>
              <div className={CHAT_CITATION_LIST_CLASS}>
                {citations.map((citation) => (
                  <button
                    key={citation.id}
                    type="button"
                    className={CHAT_CITATION_CHIP_CLASS}
                    onClick={() => setActiveCitation(citation)}
                  >
                    <span className={CHAT_CITATION_INDEX_CLASS} data-i18n-ignore>{citation.label || citation.id}</span>
                    <span className={CHAT_CITATION_TITLE_CLASS} data-i18n-ignore>{citationDisplayTitle(citation)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {scheduledDraft && (
            <ScheduledDraftCard
              draft={scheduledDraft}
              createdTask={createdTask}
              onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft, item.id)}
              onDismiss={() => dismissScheduledTaskDraft(item.id)}
            />
          )}

          {canRateMessage(item) && (
            <div className={CHAT_FEEDBACK_CLASS}>
              <button
                type="button"
                className={cn(CHAT_FEEDBACK_BTN_CLASS, item.feedback_rating === 'up' && CHAT_FEEDBACK_BTN_ACTIVE_CLASS)}
                aria-label="点赞"
                onClick={() => rateMessage(item, 'up')}
              >
                <StaffdeckIcon name="thumb-up" size={15} />
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
                <StaffdeckIcon name="thumb-down" size={15} />
              </button>
            </div>
          )}
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
