import { describe, expect, it } from 'vitest';

import { formatHandoffAssigneeValue, parseHandoffAssigneeValue } from './handoff-assignee';

describe('formatHandoffAssigneeValue', () => {
  it('returns empty for missing user', () => {
    expect(formatHandoffAssigneeValue(null, null)).toBe('');
    expect(formatHandoffAssigneeValue('', '')).toBe('');
  });

  it('returns plain user id for web delivery', () => {
    expect(formatHandoffAssigneeValue('user_1', null)).toBe('user_1');
    expect(formatHandoffAssigneeValue('user_1', 'web')).toBe('user_1');
  });

  it('appends channel for channel delivery', () => {
    expect(formatHandoffAssigneeValue('user_1', 'feishu')).toBe('user_1::feishu');
  });
});

describe('parseHandoffAssigneeValue', () => {
  it('parses empty value as unassigned', () => {
    expect(parseHandoffAssigneeValue('')).toEqual({ userId: '', channel: null });
    expect(parseHandoffAssigneeValue('   ')).toEqual({ userId: '', channel: null });
  });

  it('parses plain user id as web delivery', () => {
    expect(parseHandoffAssigneeValue('user_1')).toEqual({ userId: 'user_1', channel: 'web' });
  });

  it('parses composite value into user and channel', () => {
    expect(parseHandoffAssigneeValue('user_1::feishu')).toEqual({
      userId: 'user_1',
      channel: 'feishu',
    });
  });

  it('falls back to web when channel segment is malformed', () => {
    expect(parseHandoffAssigneeValue('user_1::')).toEqual({ userId: 'user_1::', channel: 'web' });
    expect(parseHandoffAssigneeValue('::feishu')).toEqual({ userId: '::feishu', channel: 'web' });
  });

  it('round-trips with formatHandoffAssigneeValue', () => {
    expect(parseHandoffAssigneeValue(formatHandoffAssigneeValue('user_1', 'feishu'))).toEqual({
      userId: 'user_1',
      channel: 'feishu',
    });
    expect(parseHandoffAssigneeValue(formatHandoffAssigneeValue('user_1', null))).toEqual({
      userId: 'user_1',
      channel: 'web',
    });
  });
});
