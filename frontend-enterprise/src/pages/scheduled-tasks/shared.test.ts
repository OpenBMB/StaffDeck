import { describe, expect, it } from 'vitest';

import type { ScheduledTaskRead, ScheduledTaskRunRead } from '../../types';
import { matchesRunFilter, RUN_STATUS_BADGE, taskToFormValues } from './shared';

function run(status: string): ScheduledTaskRunRead {
  return {
    id: `run-${status}`,
    tenant_id: 'tenant-demo',
    scheduled_task_id: 'scheduled-1',
    agent_id: 'agent-1',
    user_id: 'user-1',
    scheduled_for: '2026-08-01T09:00:00',
    status,
    trace: {},
    created_at: '2026-08-01T09:00:00',
    updated_at: '2026-08-01T09:00:00',
  };
}

describe('scheduled task Harness statuses', () => {
  it.each(['queued', 'running', 'needs_input', 'incomplete'])(
    'keeps %s in the pending filter',
    (status) => {
      expect(matchesRunFilter(run(status), 'pending')).toBe(true);
    },
  );

  it('presents non-terminal Harness outcomes explicitly', () => {
    expect(RUN_STATUS_BADGE.needs_input.text).toBe('待补充信息');
    expect(RUN_STATUS_BADGE.incomplete.text).toBe('未完成');
  });
});

describe('scheduled task SOP selection', () => {
  it('restores the pinned Harness v2 SOP from task metadata', () => {
    const task = {
      id: 'scheduled-1',
      tenant_id: 'tenant-demo',
      agent_id: 'agent-1',
      created_by_user_id: 'user-1',
      title: '日报',
      prompt: '生成日报',
      schedule_type: 'daily',
      schedule: { time: '09:00' },
      timezone: 'Asia/Shanghai',
      status: 'active',
      concurrency_policy: 'forbid',
      misfire_policy: 'coalesce',
      run_count: 0,
      metadata: { sop_id: 'daily_report_v2' },
      created_at: '2026-08-01T09:00:00',
      updated_at: '2026-08-01T09:00:00',
    } satisfies ScheduledTaskRead;

    expect(taskToFormValues(task).sop_id).toBe('daily_report_v2');
  });
});
