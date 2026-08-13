import { describe, expect, it } from 'vitest';

import type { EnterpriseChatSessionRead, FeedbackSummaryRead } from '../../types';
import { employeeDashboardMetrics } from './employeeDashboardMetrics';

describe('employeeDashboardMetrics', () => {
  it('uses employee sessions and feedback summary as the single dashboard source', () => {
    const sessions = [{ id: 'session-1' }, { id: 'session-2' }] as EnterpriseChatSessionRead[];
    const summary = {
      total_feedback: 3,
      up_count: 2,
      down_count: 1,
      bucket_counts: [],
      status_counts: {},
      summary: '',
      top_summaries: [],
    } satisfies FeedbackSummaryRead;

    expect(employeeDashboardMetrics(sessions, summary)).toEqual({
      conversationCount: 2,
      feedbackCount: 3,
      positiveRate: 67,
      negativeRate: 33,
    });
  });

  it('keeps rates as percentages instead of exposing positive and negative counts', () => {
    const summary = {
      total_feedback: 4,
      up_count: 3,
      down_count: 1,
      bucket_counts: [],
      status_counts: {},
      summary: '',
      top_summaries: [],
    } satisfies FeedbackSummaryRead;

    const metrics = employeeDashboardMetrics([], summary);

    expect(metrics.positiveRate).toBe(75);
    expect(metrics.negativeRate).toBe(25);
    expect(metrics.positiveRate).not.toBe(summary.up_count);
  });

  it('returns stable zero values before an employee has conversations or feedback', () => {
    expect(employeeDashboardMetrics([], null)).toEqual({
      conversationCount: 0,
      feedbackCount: 0,
      positiveRate: 0,
      negativeRate: 0,
    });
  });
});
