import type { UnderlineTabItem } from '@/components/ui';
import { formatClientDateTime, parseBackendDateTime } from '@/lib/timezone';
import type { ScheduledTaskRead, ScheduledTaskRunRead } from '../../types';

export const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';
export const TASK_PAGE_SIZE = 10;

export const WEEKDAY_OPTIONS = [
  { label: '周一', value: 0 },
  { label: '周二', value: 1 },
  { label: '周三', value: 2 },
  { label: '周四', value: 3 },
  { label: '周五', value: 4 },
  { label: '周六', value: 5 },
  { label: '周日', value: 6 },
];

export type TaskFormValues = {
  title: string;
  prompt: string;
  description?: string;
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly';
  time: string;
  run_at: string;
  weekdays: number[];
  day_of_month: number;
  status: 'active' | 'paused';
  max_runs?: number;
};

export const INITIAL_VALUES: TaskFormValues = {
  title: '',
  prompt: '',
  description: '',
  schedule_type: 'daily',
  time: '09:00',
  run_at: '',
  weekdays: [0],
  day_of_month: 1,
  status: 'active',
  max_runs: undefined,
};

export type TaskListFilter = 'all' | 'pending' | 'completed' | 'paused';
export type RunListFilter = 'all' | 'pending' | 'completed' | 'failed';

export const TASK_FILTER_TABS: UnderlineTabItem<TaskListFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '待完成', value: 'pending' },
  { label: '已完成', value: 'completed' },
  { label: '已暂停', value: 'paused' },
];
export const RUN_FILTER_TABS: UnderlineTabItem<RunListFilter>[] = [
  { label: '全部', value: 'all' },
  { label: '待完成', value: 'pending' },
  { label: '已完成', value: 'completed' },
  { label: '失败/跳过', value: 'failed' },
];

const TASK_FILTERS: Record<TaskListFilter, (row: ScheduledTaskRead) => boolean> = {
  all: () => true,
  pending: (row) => row.status === 'active',
  paused: (row) => row.status === 'paused',
  completed: (row) => row.status === 'completed',
};
const RUN_FILTERS: Record<RunListFilter, (row: ScheduledTaskRunRead) => boolean> = {
  all: () => true,
  pending: (row) =>
    row.status === 'queued' ||
    row.status === 'running' ||
    row.status === 'needs_input' ||
    row.status === 'incomplete',
  failed: (row) => row.status === 'failed' || row.status === 'skipped',
  completed: (row) => row.status === 'succeeded',
};

export function matchesTaskFilter(row: ScheduledTaskRead, filter: TaskListFilter): boolean {
  return TASK_FILTERS[filter](row);
}

export function matchesRunFilter(row: ScheduledTaskRunRead, filter: RunListFilter): boolean {
  return RUN_FILTERS[filter](row);
}

export type BadgeTone = 'blue' | 'orange' | 'green' | 'red' | 'gray';
export const BADGE_TONE_CLASS: Record<BadgeTone, string> = {
  blue: 'bg-[#e8f0ff] text-[#1a71ff]',
  orange: 'bg-[#fff2e5] text-[#ff7f00]',
  green: 'bg-[#e9f7ef] text-[#2cb360]',
  red: 'bg-[#fce7e7] text-[#d20b0b]',
  gray: 'bg-[#f2f3f7] text-[#858b9c]',
};
export const TASK_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  active: { tone: 'blue', text: '启用' },
  paused: { tone: 'orange', text: '暂停' },
  completed: { tone: 'green', text: '已完成' },
  archived: { tone: 'gray', text: '已删除' },
};
export const RUN_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  succeeded: { tone: 'green', text: '成功' },
  failed: { tone: 'red', text: '失败' },
  running: { tone: 'blue', text: '执行中' },
  needs_input: { tone: 'orange', text: '待补充信息' },
  incomplete: { tone: 'orange', text: '未完成' },
  skipped: { tone: 'gray', text: '已跳过' },
};

const SCHEDULE_TYPES = new Set<TaskFormValues['schedule_type']>(['once', 'daily', 'weekly', 'monthly']);
const SCHEDULE_BUILDERS: Record<
  TaskFormValues['schedule_type'],
  (values: TaskFormValues) => Record<string, unknown>
> = {
  once: (values) => ({ run_at: values.run_at }),
  weekly: (values) => ({
    time: values.time || '09:00',
    weekdays: values.weekdays?.length ? values.weekdays : [0],
  }),
  monthly: (values) => ({
    time: values.time || '09:00',
    day_of_month: values.day_of_month || 1,
  }),
  daily: (values) => ({ time: values.time || '09:00' }),
};
const SCHEDULE_FORMATTERS: Record<
  TaskFormValues['schedule_type'],
  (row: ScheduledTaskRead, schedule: Record<string, unknown>) => string
> = {
  once: (row, schedule) => `一次性 · ${formatTime(String(schedule.run_at || row.next_run_at || ''))}`,
  weekly: (_row, schedule) => {
    const days = Array.isArray(schedule.weekdays)
      ? schedule.weekdays
          .map((item) => WEEKDAY_OPTIONS[Number(item)]?.label)
          .filter(Boolean)
          .join('、')
      : '周一';
    return `每周 ${days} ${schedule.time || '09:00'}`;
  },
  monthly: (_row, schedule) => `每月 ${schedule.day_of_month || 1} 号 ${schedule.time || '09:00'}`,
  daily: (_row, schedule) => `每天 ${schedule.time || '09:00'}`,
};

export function buildSchedule(values: TaskFormValues): Record<string, unknown> {
  return SCHEDULE_BUILDERS[values.schedule_type](values);
}

export function taskToFormValues(row: ScheduledTaskRead): TaskFormValues {
  const schedule = row.schedule || {};
  return {
    title: row.title,
    prompt: row.prompt,
    description: row.description || '',
    schedule_type: normalizeScheduleType(row.schedule_type),
    time: String(schedule.time || '09:00'),
    run_at: toDatetimeLocal(String(schedule.run_at || row.next_run_at || '')),
    weekdays: Array.isArray(schedule.weekdays) ? schedule.weekdays.map((item) => Number(item)) : [0],
    day_of_month: Number(schedule.day_of_month || 1),
    status: row.status === 'active' ? 'active' : 'paused',
    max_runs: row.max_runs,
  };
}

export function normalizeScheduleType(value: string): TaskFormValues['schedule_type'] {
  const scheduleType = value as TaskFormValues['schedule_type'];
  return SCHEDULE_TYPES.has(scheduleType) ? scheduleType : 'daily';
}

export function toDatetimeLocal(value: string): string {
  if (!value) return '';
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return '';
  const offset = date.getTimezoneOffset();
  const local = new Date(date.getTime() - offset * 60000);
  return local.toISOString().slice(0, 16);
}

export function formatSchedule(row: ScheduledTaskRead): string {
  const schedule = row.schedule || {};
  return SCHEDULE_FORMATTERS[normalizeScheduleType(row.schedule_type)](row, schedule);
}

export function formatTime(value?: string): string {
  return formatClientDateTime(value, '暂无');
}
