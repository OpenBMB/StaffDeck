// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';

import { applyNodeTypeChange, filterActionOptionsForNodeType } from './DistillPage';

describe('SOP node handoff restrictions', () => {
  it('strips handoff actions and assignee when the node type leaves handoff', () => {
    const node = applyNodeTypeChange(
      {
        type: 'handoff',
        allowed_actions: ['answer_user', 'handoff_human'],
        assignee_user_id: 'user-1',
        assignee_notify_channel: 'feishu',
      },
      'response',
    );

    expect(node.type).toBe('response');
    expect(node.allowed_actions).toEqual(['answer_user']);
    expect(node.assignee_user_id).toBeNull();
    expect(node.assignee_notify_channel).toBeNull();
  });

  it('keeps handoff config when the node type stays handoff', () => {
    const node = applyNodeTypeChange(
      {
        type: 'response',
        allowed_actions: ['answer_user'],
        assignee_user_id: 'user-1',
        assignee_notify_channel: 'feishu',
      },
      'handoff',
    );

    expect(node.type).toBe('handoff');
    expect(node.allowed_actions).toEqual(['answer_user']);
    expect(node.assignee_user_id).toBe('user-1');
    expect(node.assignee_notify_channel).toBe('feishu');
  });

  it('omits the handoff action option for non-handoff node types', () => {
    const options = [
      { value: 'answer_user', label: '回复用户' },
      { value: 'handoff_human', label: '转人工' },
    ];

    expect(filterActionOptionsForNodeType(options, 'response')).toEqual([
      { value: 'answer_user', label: '回复用户' },
    ]);
    expect(filterActionOptionsForNodeType(options, 'collect_info')).toEqual([
      { value: 'answer_user', label: '回复用户' },
    ]);
  });

  it('keeps the handoff action option for handoff node types', () => {
    const options = [
      { value: 'answer_user', label: '回复用户' },
      { value: 'handoff_human', label: '转人工' },
    ];

    expect(filterActionOptionsForNodeType(options, 'handoff')).toEqual(options);
    expect(filterActionOptionsForNodeType(options, '')).toEqual([
      { value: 'answer_user', label: '回复用户' },
    ]);
  });
});
