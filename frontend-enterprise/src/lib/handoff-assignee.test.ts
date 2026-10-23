import { describe, expect, it } from 'vitest';

import { formatHandoffAssigneeValue, parseHandoffAssigneeValue } from './handoff-assignee';

describe('formatHandoffAssigneeValue', () => {
  it('returns empty for missing user', () => {
    expect(formatHandoffAssigneeValue(null)).toBe('');
    expect(formatHandoffAssigneeValue('')).toBe('');
    expect(formatHandoffAssigneeValue('   ')).toBe('');
  });

  it('returns plain user id', () => {
    expect(formatHandoffAssigneeValue('user_1')).toBe('user_1');
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

  it('truncates legacy composite values left by the removed channel feature', () => {
    expect(parseHandoffAssigneeValue('user_1::wecom')).toEqual({
      userId: 'user_1',
      channel: 'web',
    });
    expect(parseHandoffAssigneeValue('user_1::')).toEqual({ userId: 'user_1', channel: 'web' });
  });

  it('round-trips with formatHandoffAssigneeValue', () => {
    expect(parseHandoffAssigneeValue(formatHandoffAssigneeValue('user_1'))).toEqual({
      userId: 'user_1',
      channel: 'web',
    });
  });
});
