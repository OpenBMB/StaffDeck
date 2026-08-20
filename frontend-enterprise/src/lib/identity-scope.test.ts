import { describe, expect, it } from 'vitest';

import { feishuAppIdFromIdentityScope } from './identity-scope';

describe('feishuAppIdFromIdentityScope', () => {
  it('extracts the app id from a feishu identity scope', () => {
    expect(
      feishuAppIdFromIdentityScope('app:20:cli_aaf3d15c5138dbe5:tenant:16:1a0aaaa3801ddcbc'),
    ).toBe('cli_aaf3d15c5138dbe5');
  });

  it('supports app ids and tenant keys of any encoded length', () => {
    expect(feishuAppIdFromIdentityScope('app:3:abc:tenant:2:xy')).toBe('abc');
  });

  it('returns null for non-feishu scopes', () => {
    expect(feishuAppIdFromIdentityScope('')).toBeNull();
    expect(feishuAppIdFromIdentityScope('corpA')).toBeNull();
    expect(feishuAppIdFromIdentityScope('wecom:corp:5:corpA:bot:4:bot1')).toBeNull();
    expect(feishuAppIdFromIdentityScope('binding_123')).toBeNull();
  });

  it('returns null for malformed feishu scopes', () => {
    expect(feishuAppIdFromIdentityScope('app:20:short:tenant:16:1a0aaaa3801ddcbc')).toBeNull();
    expect(feishuAppIdFromIdentityScope('app:xx:cli_aaf3d15c5138dbe5:tenant:16:1a0aaaa3801ddcbc')).toBeNull();
    expect(feishuAppIdFromIdentityScope('app:20:cli_aaf3d15c5138dbe5:corp:16:1a0aaaa3801ddcbc')).toBeNull();
    expect(feishuAppIdFromIdentityScope('app:')).toBeNull();
  });
});
