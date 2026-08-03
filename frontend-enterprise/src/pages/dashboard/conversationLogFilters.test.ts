import { describe, expect, it } from 'vitest';

import type { FeedbackSessionRead } from '@/types';

import {
  buildConversationUserOptions,
  matchesConversationLogFilter,
  type ConversationLogRow,
} from './conversationLogFilters';

function feedback(
  sessionId: string,
  primaryBucket?: string,
): FeedbackSessionRead {
  return {
    session_id: sessionId,
    tenant_id: 'tenant-demo',
    status: 'completed',
    feedback_count: 1,
    latest_feedback_at: '2026-08-02T00:00:00Z',
    latest_message_id: `message-${sessionId}`,
    latest_message: 'feedback',
    primary_bucket: primaryBucket,
    updated_at: '2026-08-02T00:00:00Z',
  };
}

function row(
  id: string,
  userId: string,
  displayName: string,
  patch: Partial<ConversationLogRow> = {},
): ConversationLogRow {
  return {
    id,
    tenant_id: 'tenant-demo',
    user_id: userId,
    status: 'completed',
    session_display_name: displayName,
    session_username: `${displayName.toLowerCase()}-user`,
    created_at: '2026-08-02T00:00:00Z',
    updated_at: '2026-08-02T00:00:00Z',
    ...patch,
  };
}

describe('conversation log filters', () => {
  it('counts only users present in the current log category', () => {
    const rows = [
      row('session-a-up', 'user-a', 'Alice', { upFeedback: feedback('session-a-up') }),
      row('session-a-down', 'user-a', 'Alice', { downFeedback: feedback('session-a-down') }),
      row('session-b-up', 'user-b', 'Bob', { upFeedback: feedback('session-b-up') }),
    ];

    expect(buildConversationUserOptions(rows)).toEqual([
      { userId: 'user-a', label: 'Alice · @alice-user', count: 2 },
      { userId: 'user-b', label: 'Bob · @bob-user', count: 1 },
    ]);

    const downRows = rows.filter((item) => matchesConversationLogFilter(item, 'down'));
    expect(buildConversationUserOptions(downRows)).toEqual([
      { userId: 'user-a', label: 'Alice · @alice-user', count: 1 },
    ]);
  });

  it('maps issue tabs to their feedback buckets and omits missing user ids', () => {
    const abilityRow = row('session-ability', 'user-a', 'Alice', {
      downFeedback: feedback('session-ability', 'model_issue'),
    });
    const knowledgeRow = row('session-knowledge', 'user-b', 'Bob', {
      downFeedback: feedback('session-knowledge', 'unknown'),
    });
    const anonymousRow = row('session-anonymous', '', 'Unknown', {
      downFeedback: feedback('session-anonymous', 'model_issue'),
    });

    expect(matchesConversationLogFilter(abilityRow, 'ability')).toBe(true);
    expect(matchesConversationLogFilter(abilityRow, 'knowledge')).toBe(false);
    expect(matchesConversationLogFilter(knowledgeRow, 'knowledge')).toBe(true);
    expect(buildConversationUserOptions([anonymousRow])).toEqual([]);
  });
});
