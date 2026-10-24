// @vitest-environment jsdom

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import EvolutionPanel from './EvolutionPanel';

type DeferredResponse = {
  promise: Promise<Response>;
  resolve: (body: unknown) => void;
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body),
  } as Response;
}

function deferredResponse(): DeferredResponse {
  let resolvePromise: (response: Response) => void = () => {};
  const promise = new Promise<Response>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (body) => resolvePromise(jsonResponse(body)),
  };
}

function proposal(id: string, resourceName: string) {
  return {
    id,
    resource_type: 'sop',
    resource_name: resourceName,
    resource_key: `sop:${id}`,
    status: 'ready_for_review',
    risk_level: 'low',
    hypothesis: `${resourceName} 的改进假设`,
    rationale: '来自真实反馈',
    expected_outcome: '提升执行质量',
    source_feedback_ids: ['feedback-1'],
    evidence: [],
    diff: [],
    evaluation: {},
    created_at: '2026-08-20T00:00:00Z',
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('EvolutionPanel employee switching', () => {
  it('hides the previous employee proposals while the next employee is loading', async () => {
    const agentBResponse = deferredResponse();
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/agents/agent-a/')) {
        return Promise.resolve(jsonResponse([proposal('proposal-a', '员工 A 的候选')]));
      }
      if (url.includes('/agents/agent-b/')) return agentBResponse.promise;
      return Promise.resolve(jsonResponse([]));
    }));

    const { rerender } = render(
      <I18nProvider>
        <EvolutionPanel agentId="agent-a" />
      </I18nProvider>,
    );
    expect(await screen.findByText('员工 A 的候选')).toBeTruthy();

    rerender(
      <I18nProvider>
        <EvolutionPanel agentId="agent-b" />
      </I18nProvider>,
    );

    expect(screen.queryByText('员工 A 的候选')).toBeNull();
  });

  it('ignores a previous employee response that arrives after the current employee response', async () => {
    const agentAResponse = deferredResponse();
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/agents/agent-a/')) return agentAResponse.promise;
      if (url.includes('/agents/agent-b/')) {
        return Promise.resolve(jsonResponse([proposal('proposal-b', '员工 B 的候选')]));
      }
      return Promise.resolve(jsonResponse([]));
    }));

    const { rerender } = render(
      <I18nProvider>
        <EvolutionPanel agentId="agent-a" />
      </I18nProvider>,
    );
    rerender(
      <I18nProvider>
        <EvolutionPanel agentId="agent-b" />
      </I18nProvider>,
    );
    expect(await screen.findByText('员工 B 的候选')).toBeTruthy();

    // 模拟较早发出的员工 A 请求在员工 B 已展示后才返回。
    await act(async () => {
      agentAResponse.resolve([proposal('proposal-a', '员工 A 的候选')]);
      await Promise.resolve();
    });

    expect(screen.queryByText('员工 A 的候选')).toBeNull();
    expect(screen.getByText('员工 B 的候选')).toBeTruthy();
  });
});
