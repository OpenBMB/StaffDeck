import { describe, expect, it } from 'vitest';

import { capabilityScopeLabel, normalizeCapabilityScope } from './CapabilityScopeControl';

describe('capability scope helpers', () => {
  it('keeps the canonical SOP-specific wire value', () => {
    expect(normalizeCapabilityScope('sop_specific')).toBe('sop_specific');
  });

  it('accepts the label-style legacy spelling', () => {
    expect(normalizeCapabilityScope('sop-specific')).toBe('sop_specific');
  });

  it('defaults missing or unknown values to general', () => {
    expect(normalizeCapabilityScope(undefined)).toBe('general');
    expect(normalizeCapabilityScope('unknown')).toBe('general');
    expect(capabilityScopeLabel(undefined)).toBe('通用');
  });
});
