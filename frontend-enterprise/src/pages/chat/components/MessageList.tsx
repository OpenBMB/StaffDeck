import {
  CHAT_DEBUG_PANEL_CLASS,
  CHAT_MESSAGES_CLASS,
  CHAT_MESSAGE_STACK_CLASS,
} from '../chatPageStyles';
import {
  CHAT_TRACE_RECOVERY_WINDOW_MS,
  a2uiFormForMessage,
  createdScheduledTaskForMessage,
  harnessWorkspaceArtifacts,
  isScheduledTaskPrompt,
  knowledgeCitations,
  messageAttachments,
  normalizeMessageText,
  placeQueuedMessagesLast,
  scheduledDraftForMessage,
  stripTrailingCitationSummary,
  traceDetails,
  traceLineAllowed,
  traceSummary,
} from '../chatHelpers';
import { staffdeckDisplayText } from '@/employee';
import type { UseChatSession } from '../useChatSession';
import ChatEmptyState from './ChatEmptyState';
import MessageBubble, { type MessageRender } from './MessageBubble';
import ScheduledDraftCard from './ScheduledDraftCard';
import TeamCollaborationPanel, {
  mergeTeamChatTimeline,
  useTeamCollaborations,
} from './TeamCollaborationPanel';

export default function MessageList({
  chat,
  emptyState,
}: {
  chat: UseChatSession;
  emptyState?: ReactNode;
}) {
  const {
    displayedMessages,
    turnTraceRef,
    uiConfig,
    currentStream,
    runningTurn,
    expandedTraceIds,
    collapsedTraceIds,
    isCurrentStreamingTrace,
    dismissedDraftMessageIds,
    createdScheduledTasks,
    activeConversationId,
    currentScheduledDraft,
    hasVisibleMessageScheduledDraft,
    confirmScheduledTask,
    dismissScheduledTaskDraft,
    chatMessagesRef,
    handleChatMessagesScroll,
    SHOW_DEBUG,
    lastTurn,
    hideKnowledgeCitations = false,
  } = chat;
  const renderMessages = placeQueuedMessagesLast(displayedMessages);
  const teamCollaborations = useTeamCollaborations(chat.displayedTeam);
  const timelineEntries = mergeTeamChatTimeline(renderMessages, teamCollaborations);

  return (
    <div className={CHAT_MESSAGES_CLASS} ref={chatMessagesRef} onScroll={handleChatMessagesScroll}>
      {timelineEntries.length === 0 && (emptyState ?? <ChatEmptyState chat={chat} />)}

      <div className={CHAT_MESSAGE_STACK_CLASS}>
        {timelineEntries.map((entry) => {
          if (entry.kind === 'collaboration') {
            return chat.displayedTeam ? (
              <TeamCollaborationPanel
                key={`${entry.conversation.session_id}:collaboration`}
                team={chat.displayedTeam}
                agents={chat.agents}
                conversation={entry.conversation}
                onOpenCitation={chat.setActiveCitation}
                hideCitations={hideKnowledgeCitations}
              />
            ) : null;
          }
          const { message: item, messageIndex: itemIndex } = entry;
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
            && traceLines.some((line) => line.state === 'running'),
          );
          const visibleTrace = forceRunningTrace ? traceLines : allowedTrace;
          const summary = trace && visibleTrace.length > 0 ? traceSummary(trace, visibleTrace) : null;
          const details = traceDetails(visibleTrace);
          const traceActive = isCurrentStreamingTrace(traceTurnId, item);
          const summaryForRender = summary && traceActive && !trace?.completedAt
            ? { ...summary, state: 'running' as const }
            : summary;
          const traceOnlyMessage = Boolean(
            item.role === 'assistant' && !normalizeMessageText(item.content) && details.length > 0,
          );
          const latestAssistantTrace = Boolean(
            item.role === 'assistant'
            && details.length > 0
            && !renderMessages.slice(itemIndex + 1).some((later) => (
              later.role === 'assistant'
              && Boolean(turnTraceRef.current.get(later.turnId || later.id)?.lines.length)
            )),
          );
          const recentlyStartedTrace = Boolean(
            trace?.startedAt && Date.now() - trace.startedAt <= CHAT_TRACE_RECOVERY_WINDOW_MS,
          );
          const defaultExpanded = Boolean(
            traceActive
            || summaryForRender?.state === 'running'
            || traceOnlyMessage
            || (latestAssistantTrace && recentlyStartedTrace),
          );
          const manuallyCollapsed = collapsedTraceIds.includes(traceTurnId);
          const expanded = Boolean(
            !manuallyCollapsed
            && (expandedTraceIds.includes(traceTurnId) || defaultExpanded),
          );
          const rawVisibleContent = staffdeckDisplayText(
            item.role === 'assistant' ? stripTrailingCitationSummary(item.content) : item.content,
          );
          const visibleContent = normalizeMessageText(rawVisibleContent) ? rawVisibleContent : '';
          const citations = item.role === 'assistant' && !hideKnowledgeCitations
            ? knowledgeCitations(item, visibleContent)
            : [];
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
            && visibleContent === '已停止生成',
          );
          const attachments = messageAttachments(item);
          const harnessArtifacts = item.role === 'assistant'
            ? harnessWorkspaceArtifacts(item)
            : [];
          // A2UI 表单只挂最新一条助手消息：历史轮次的表单早已被提交过，
          // 重新渲染出来只会诱导用户重复提交（后端也认不出旧步骤）。
          // 另外，一旦后续出现了带 ``slot_submission`` 的用户消息，说明这张
          // 表单已经提交过——刷新页面后不能再渲染出可编辑态。
          const laterMessages = renderMessages.slice(itemIndex + 1);
          const slotForm = item.role === 'assistant'
            && !laterMessages.some((later) => later.role === 'assistant')
            && !laterMessages.some((later) => Boolean(later.metadata?.slot_submission))
            ? a2uiFormForMessage(item)
            : null;
          const statusOnly = stoppedStatusOnly;
          const showInlineTrace = Boolean(summaryForRender && !stoppedStatusOnly);

          if (
            item.role === 'assistant'
            && !visibleContent
            && !showInlineTrace
            && !statusOnly
            && !scheduledDraft
            && !persistedCreatedTask
            && citations.length === 0
            && attachments.length === 0
            && harnessArtifacts.length === 0
            && !slotForm
          ) {
            return null;
          }

          const render: MessageRender = {
            traceTurnId,
            summary: summaryForRender,
            details,
            expanded,
            showInlineTrace,
            visibleContent,
            citations,
            scheduledDraft,
            createdTask: createdScheduledTasks[item.id] || persistedCreatedTask,
            scheduledTaskPrompt,
            attachments,
            harnessArtifacts,
            statusOnly,
            slotForm,
            traceTiming: item.role === 'assistant' && trace
              ? {
                startedAt: trace.startedAt,
                completedAt: trace.completedAt,
                durationMs: trace.durationMs,
              }
              : undefined,
          };

          return <MessageBubble key={`${item.id}:message`} chat={chat} item={item} render={render} />;
        })}
      </div>

      {currentScheduledDraft && !hasVisibleMessageScheduledDraft && (
        <div className={CHAT_MESSAGE_STACK_CLASS}>
          <ScheduledDraftCard
            draft={currentScheduledDraft}
            createdTask={activeConversationId ? createdScheduledTasks[`session:${activeConversationId}`] : undefined}
            onConfirm={(nextDraft) => void confirmScheduledTask(nextDraft)}
            onDismiss={() => dismissScheduledTaskDraft()}
          />
        </div>
      )}

      {SHOW_DEBUG && lastTurn && (
        <pre className={CHAT_DEBUG_PANEL_CLASS}>{JSON.stringify(lastTurn.session_state, null, 2)}</pre>
      )}
    </div>
  );
}
import type { ReactNode } from 'react';
