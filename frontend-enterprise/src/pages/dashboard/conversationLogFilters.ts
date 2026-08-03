import type {
  EnterpriseChatSessionRead,
  FeedbackSessionRead,
} from '@/types';

export type ConversationLogFilter =
  | 'all'
  | 'up'
  | 'down'
  | 'unrated'
  | 'ability'
  | 'tool'
  | 'knowledge'
  | 'sop';

export type ConversationLogRow = EnterpriseChatSessionRead & {
  downFeedback?: FeedbackSessionRead;
  upFeedback?: FeedbackSessionRead;
};

export type ConversationUserOption = {
  userId: string;
  label: string;
  count: number;
};

export function matchesConversationLogFilter(
  row: ConversationLogRow,
  filter: ConversationLogFilter,
): boolean {
  if (filter === 'up') return Boolean(row.upFeedback);
  if (filter === 'down') return Boolean(row.downFeedback);
  if (filter === 'unrated') return !row.upFeedback && !row.downFeedback;
  if (filter === 'ability') return row.downFeedback?.primary_bucket === 'model_issue';
  if (filter === 'tool') return row.downFeedback?.primary_bucket === 'tool_or_system_issue';
  if (filter === 'sop') return row.downFeedback?.primary_bucket === 'skill_issue';
  if (filter === 'knowledge') return row.downFeedback?.primary_bucket === 'unknown';
  return true;
}

export function buildConversationUserOptions(
  rows: readonly ConversationLogRow[],
): ConversationUserOption[] {
  const options = new Map<string, ConversationUserOption>();
  rows.forEach((row) => {
    const userId = String(row.user_id || '').trim();
    if (!userId) return;
    const current = options.get(userId);
    options.set(userId, {
      userId,
      label: conversationUserLabel(row),
      count: (current?.count || 0) + 1,
    });
  });
  return Array.from(options.values()).sort((left, right) => (
    left.label.localeCompare(right.label, 'zh-CN')
  ));
}

function conversationUserLabel(session: EnterpriseChatSessionRead): string {
  const displayName = String(session.session_display_name || '').trim();
  const username = String(session.session_username || '').trim();
  if (displayName && username && displayName !== username) {
    return `${displayName} · ${username.startsWith('@') ? username : `@${username}`}`;
  }
  return displayName || username || '未知用户';
}
