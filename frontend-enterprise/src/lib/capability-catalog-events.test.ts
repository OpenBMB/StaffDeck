// @vitest-environment jsdom

import { describe, expect, it, vi } from 'vitest';

import {
  announceEnterpriseCapabilityCatalogChange,
  subscribeEnterpriseCapabilityCatalogRefresh,
} from './capability-catalog-events';

describe('enterprise capability catalog refresh events', () => {
  it('refreshes when a capability changes in the current page', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeEnterpriseCapabilityCatalogRefresh(listener);

    announceEnterpriseCapabilityCatalogChange({ resourceType: 'tool', agentId: 'agent-1' });

    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
  });

  it('refreshes when the SOP editor regains focus', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeEnterpriseCapabilityCatalogRefresh(listener);

    window.dispatchEvent(new Event('focus'));

    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
  });
});
