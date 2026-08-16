// @vitest-environment jsdom

import { afterEach, describe, expect, it } from 'vitest';

import {
  ENTERPRISE_AGENT_STORAGE_KEY,
  isTeamScope,
  readEmployeeScope,
  teamIdFromScope,
  toTeamScope,
} from './agent-scope-storage';

afterEach(() => {
  window.localStorage.clear();
});

describe('team agent-scope helpers', () => {
  it('builds a team scope value with the team: prefix', () => {
    expect(toTeamScope('team-1')).toBe('team:team-1');
    expect(toTeamScope('')).toBe('');
  });

  it('detects team scope values and leaves employee ids alone', () => {
    expect(isTeamScope('team:team-1')).toBe(true);
    expect(isTeamScope('agent-1')).toBe(false);
    expect(isTeamScope('team:')).toBe(false);
    expect(isTeamScope('')).toBe(false);
    expect(isTeamScope(null)).toBe(false);
    expect(isTeamScope(undefined)).toBe(false);
  });

  it('extracts the team id from a team scope value', () => {
    expect(teamIdFromScope('team:team-1')).toBe('team-1');
    expect(teamIdFromScope('team:team:with:colons')).toBe('team:with:colons');
    expect(teamIdFromScope('agent-1')).toBe('');
    expect(teamIdFromScope(null)).toBe('');
  });

  it('reads team scopes as an empty employee scope', () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    expect(readEmployeeScope()).toBe('');
  });

  it('reads employee scopes as-is', () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'agent-1');
    expect(readEmployeeScope()).toBe('agent-1');
  });

  it('reads an empty storage slot as an empty employee scope', () => {
    expect(readEmployeeScope()).toBe('');
  });
});
