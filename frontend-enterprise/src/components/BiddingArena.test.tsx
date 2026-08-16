// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { TeamTaskBidRead } from '@/types';

import BiddingArena, { computeBidHp } from './BiddingArena';

function makeBid(overrides: Partial<TeamTaskBidRead>): TeamTaskBidRead {
  return {
    id: 'bid-1',
    task_id: 'task-1',
    agent_id: 'agent-1',
    agent_name: '小艾',
    round: 1,
    kind: 'statement',
    content: '陈述内容',
    score: null,
    score_rationale: null,
    created_at: '2026-08-02T00:00:00Z',
    ...overrides,
  };
}

afterEach(cleanup);

describe('computeBidHp', () => {
  it('starts at 100 and deducts (10 - score) * 3 per scored round', () => {
    const bids = [
      makeBid({ id: 'bid-1', round: 1, score: 8 }),
      makeBid({ id: 'bid-2', round: 2, score: 5 }),
    ];
    expect(computeBidHp(bids)).toBe(100 - 6 - 15);
  });

  it('floors HP at 0', () => {
    const bids = [1, 2, 3, 4].map((round) => makeBid({ id: `bid-${round}`, round, score: 0 }));
    expect(computeBidHp(bids)).toBe(0);
  });

  it('keeps full HP for legacy bids without scores', () => {
    expect(computeBidHp([makeBid({})])).toBe(100);
    expect(computeBidHp([])).toBe(100);
  });
});

describe('BiddingArena', () => {
  it('renders the winner crown, HP bar width and elimination state', () => {
    const bids = [
      makeBid({ id: 'bid-1', agent_id: 'agent-1', agent_name: '小艾', round: 1, score: 9 }),
      makeBid({
        id: 'bid-2',
        agent_id: 'agent-2',
        agent_name: '小北',
        round: 1,
        kind: 'rebuttal',
        content: '反驳内容',
        score: 0,
      }),
      makeBid({ id: 'bid-3', agent_id: 'agent-2', agent_name: '小北', round: 2, score: 0 }),
      makeBid({ id: 'bid-4', agent_id: 'agent-2', agent_name: '小北', round: 3, score: 1 }),
      makeBid({ id: 'bid-5', agent_id: 'agent-2', agent_name: '小北', round: 4, score: 0 }),
    ];
    render(<BiddingArena bids={bids} winnerId="agent-1" />);

    expect(screen.getByText('胜者为王')).toBeTruthy();
    expect(screen.getByText('淘汰')).toBeTruthy();
    expect(screen.getByText('HP 97')).toBeTruthy();
    expect(screen.getByText('HP 0')).toBeTruthy();
    expect(screen.getByTestId('arena-hp-agent-1').style.width).toBe('97%');
    expect(screen.getByTestId('arena-hp-agent-2').style.width).toBe('0%');
    expect(screen.getAllByText('陈述内容').length).toBeGreaterThan(0);
    expect(screen.getByText('反驳内容')).toBeTruthy();
    expect(screen.getByText('反驳')).toBeTruthy();
  });
});
