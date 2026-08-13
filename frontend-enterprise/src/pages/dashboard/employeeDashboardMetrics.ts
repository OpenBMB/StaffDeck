import type { EnterpriseChatSessionRead, FeedbackSummaryRead } from '../../types';

export type EmployeeDashboardMetrics = {
  conversationCount: number;
  feedbackCount: number;
  positiveRate: number;
  negativeRate: number;
};

export function employeeDashboardMetrics(
  sessions: EnterpriseChatSessionRead[],
  summary: FeedbackSummaryRead | null,
): EmployeeDashboardMetrics {
  const feedbackCount = summary?.total_feedback ?? 0;
  return {
    conversationCount: sessions.length,
    feedbackCount,
    positiveRate: feedbackCount ? Math.round(((summary?.up_count ?? 0) / feedbackCount) * 100) : 0,
    negativeRate: feedbackCount ? Math.round(((summary?.down_count ?? 0) / feedbackCount) * 100) : 0,
  };
}
