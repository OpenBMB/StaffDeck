import { describe, expect, it } from 'vitest';

import {
  formatClientDate,
  formatClientDateISO,
  formatClientDateTime,
  formatClientMonthDayTime,
  formatClientYearMonthDayTime,
  parseBackendDateTime,
  toClientDateISO,
} from './timezone';

describe('parseBackendDateTime', () => {
  it('treats naive ISO timestamps as UTC instead of local time', () => {
    const parsed = parseBackendDateTime('2026-08-11T09:00:00');
    expect(parsed.getTime()).toBe(Date.UTC(2026, 7, 11, 9, 0, 0));
  });

  it('keeps timestamps that already carry a timezone suffix', () => {
    const withZ = parseBackendDateTime('2026-08-11T09:00:00Z');
    expect(withZ.getTime()).toBe(Date.UTC(2026, 7, 11, 9, 0, 0));
    const withOffset = parseBackendDateTime('2026-08-11T17:00:00+08:00');
    expect(withOffset.getTime()).toBe(Date.UTC(2026, 7, 11, 9, 0, 0));
  });

  it('parses date-only strings as UTC without appending a suffix', () => {
    const parsed = parseBackendDateTime('2026-08-11');
    expect(parsed.getTime()).toBe(Date.UTC(2026, 7, 11));
  });

  it('returns an invalid date for empty or malformed input', () => {
    expect(Number.isNaN(parseBackendDateTime('').getTime())).toBe(true);
    expect(Number.isNaN(parseBackendDateTime('not-a-date').getTime())).toBe(true);
  });
});

describe('formatClientDateTime', () => {
  it('renders naive UTC timestamps in the client timezone', () => {
    const expected = new Date(Date.UTC(2026, 7, 11, 9, 0, 0)).toLocaleString('zh-CN', {
      hour12: false,
    });
    expect(formatClientDateTime('2026-08-11T09:00:00')).toBe(expected);
  });

  it('falls back to the empty text for missing values', () => {
    expect(formatClientDateTime(undefined)).toBe('-');
    expect(formatClientDateTime('bad', '')).toBe('');
  });
});

describe('formatClientMonthDayTime', () => {
  it('renders a naive UTC timestamp as local month/day + hh:mm', () => {
    const expected = new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    }).format(new Date(Date.UTC(2026, 9, 19, 8, 34, 0)));
    expect(formatClientMonthDayTime('2026-10-19T08:34:00')).toBe(expected);
  });

  it('does not shift the clock backwards by the UTC offset (分享到期时间回归)', () => {
    // 回归：分享页原先 `new Date('...T08:34:00')` 按本地时间解析，东八区显示 08:34
    // 而实际到期是本地 16:34。这里断言东八区下的具体呈现。
    const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (clientTz === 'Asia/Shanghai') {
      expect(formatClientMonthDayTime('2026-10-19T08:34:00')).toBe('10/19 16:34');
    }
  });

  it('keeps timestamps that already carry a timezone suffix', () => {
    expect(formatClientMonthDayTime('2026-10-19T08:34:00Z'))
      .toBe(formatClientMonthDayTime('2026-10-19T08:34:00'));
    expect(formatClientMonthDayTime('2026-10-19T16:34:00+08:00'))
      .toBe(formatClientMonthDayTime('2026-10-19T08:34:00'));
  });

  it('falls back to the empty text for missing values', () => {
    expect(formatClientMonthDayTime(undefined)).toBe('-');
    expect(formatClientMonthDayTime('bad', '')).toBe('');
  });
});

describe('formatClientYearMonthDayTime', () => {
  it('renders a naive UTC timestamp with year as local time', () => {
    const expected = new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    }).format(new Date(Date.UTC(2026, 9, 19, 8, 34, 0)));
    expect(formatClientYearMonthDayTime('2026-10-19T08:34:00')).toBe(expected);
  });

  it('does not shift the clock backwards by the UTC offset (API Key 时间回归)', () => {
    const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (clientTz === 'Asia/Shanghai') {
      expect(formatClientYearMonthDayTime('2026-10-19T08:34:00')).toBe('2026/10/19 16:34');
    }
  });

  it('falls back to the empty text for missing values', () => {
    expect(formatClientYearMonthDayTime(undefined)).toBe('-');
    expect(formatClientYearMonthDayTime(null)).toBe('-');
    expect(formatClientYearMonthDayTime('bad', '')).toBe('');
  });
});

describe('formatClientDate', () => {
  it('renders a naive UTC timestamp as the date in the client timezone', () => {
    const expected = new Date(Date.UTC(2026, 7, 11, 9, 0, 0)).toLocaleDateString('zh-CN', {
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    });
    expect(formatClientDate('2026-08-11T09:00:00')).toBe(expected);
  });

  it('does not push a date-only value to the previous day', () => {
    // 纯日期串没有时刻信息，任何客户端时区下都必须原样显示，不能变成 08-10
    expect(formatClientDate('2026-08-11')).toBe('2026/8/11');
    expect(formatClientDate('2026-01-01')).toBe('2026/1/1');
  });

  it('agrees with the sliced date only when the client timezone is UTC', () => {
    // 回归：以前各页直接 `updated_at.slice(0, 10)`，本地凌晨的时间戳会显示成前一天
    const raw = '2026-09-22T18:00:00';
    const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const expected = new Date(Date.UTC(2026, 8, 22, 18, 0, 0)).toLocaleDateString('zh-CN', {
      timeZone: clientTz,
    });
    expect(formatClientDate(raw)).toBe(expected);
    if (clientTz === 'UTC') expect(formatClientDate(raw)).toBe('2026/9/22');
    else expect(formatClientDate(raw)).toBe('2026/9/23');
  });

  it('falls back to the empty text for missing values', () => {
    expect(formatClientDate(undefined)).toBe('-');
    expect(formatClientDate('bad', '')).toBe('');
  });
});

describe('toClientDateISO', () => {
  it('renders the local calendar date with zero padding', () => {
    expect(toClientDateISO(new Date(2026, 0, 5, 23, 30))).toBe('2026-01-05');
    expect(toClientDateISO(new Date(2026, 11, 31, 0, 1))).toBe('2026-12-31');
  });

  it('uses local parts, not the UTC date (入职日期默认值回归)', () => {
    // 回归：以前用 `new Date().toISOString().slice(0, 10)`，东八区凌晨到 8 点之间会给成昨天
    const local = new Date(2026, 9, 20, 1, 0);
    const utcSliced = local.toISOString().slice(0, 10);
    expect(toClientDateISO(local)).toBe('2026-10-20');
    if (local.getTimezoneOffset() < 0) expect(utcSliced).not.toBe(toClientDateISO(local));
  });
});

describe('formatClientDateISO', () => {
  it('renders a naive UTC timestamp as the local date', () => {
    const clientTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    expect(formatClientDateISO('2026-09-22T18:00:00'))
      .toBe(clientTz === 'UTC' ? '2026-09-22' : '2026-09-23');
  });

  it('keeps date-only values untouched', () => {
    expect(formatClientDateISO('2026-08-11')).toBe('2026-08-11');
  });

  it('falls back to the empty text for missing or malformed values', () => {
    expect(formatClientDateISO(undefined)).toBe('-');
    expect(formatClientDateISO(null, '')).toBe('');
    expect(formatClientDateISO('bad', '')).toBe('');
  });
});
