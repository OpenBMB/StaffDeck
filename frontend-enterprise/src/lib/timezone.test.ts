import { describe, expect, it } from 'vitest';

import { formatClientDateTime, parseBackendDateTime } from './timezone';

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
