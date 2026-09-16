// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { HumanHandoffRead } from '@/types';

import type { UseChatSession } from '../useChatSession';
import ChatDialogs from './ChatDialogs';

const noticedHandoff: HumanHandoffRead = {
  id: 'handoff-1',
  tenant_id: 'tenant_demo',
  session_id: 'session-1',
  status: 'pending',
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  notice: {
    title: '法律咨询·转交真人法务',
    inquirer_name: '张三',
    assignee_notice: '由于没有配置处理人，已经转接给Administrator。',
    scoped: true,
    conversation: [
      { role: 'user', text: '合作伙伴在PR里用了我们的开源模型，想咨询合规问题' },
      { role: 'assistant', text: '请补充：时间期限、合作伙伴名称。' },
      { role: 'user', text: '1.时间期限:9月初' },
      { role: 'assistant', text: '好的，正在为您转接人工。' },
    ],
    fallback_question: '',
  },
};

function buildChat(extra: Record<string, unknown> = {}): UseChatSession {
  return {
    showHandoffInbox: true,
    setShowHandoffInbox: () => {},
    handoffs: [noticedHandoff],
    handoffsLoading: false,
    handoffReplies: {},
    setHandoffReplies: () => {},
    submitHandoffReply: () => {},
    agents: [],
    displayedAgent: null,
    displayedProfile: null,
    openSession: () => {},
    activeCitation: null,
    setActiveCitation: () => {},
    renameSession: null,
    setRenameSession: () => {},
    renameTitle: '',
    setRenameTitle: () => {},
    saveRename: () => {},
    pendingDelete: null,
    setPendingDelete: () => {},
    confirmDeleteSession: () => {},
    tenantId: 'tenant_demo',
    canConfigureModels: false,
    modelSetupOpen: false,
    setModelSetupOpen: () => {},
    completeModelSetup: () => {},
    ...extra,
  } as unknown as UseChatSession;
}

function renderDialogs(chat: UseChatSession) {
  return render(
    <I18nProvider>
      <ChatDialogs chat={chat} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe('ChatDialogs handoff inbox', () => {
  it('renders the unified notice content shared with the channel notification', () => {
    renderDialogs(buildChat());

    expect(screen.getByText('法律咨询·转交真人法务')).toBeTruthy();
    expect(screen.getByText('提问人：张三')).toBeTruthy();
    expect(screen.getByText('由于没有配置处理人，已经转接给Administrator。')).toBeTruthy();
    expect(screen.getByText('对话记录（自进入该SOP起）')).toBeTruthy();
    expect(screen.getByText(/合作伙伴在PR里用了我们的开源模型/)).toBeTruthy();
    expect(screen.getByText('助手：好的，正在为您转接人工。')).toBeTruthy();
    expect(screen.getByText('回复并恢复')).toBeTruthy();
    // 统一内容就位后不再渲染旧的两段式内容
    expect(screen.queryByText('上下文摘要')).toBeNull();
    expect(screen.queryByText('这一步需要你处理')).toBeNull();
  });

  it('falls back to the legacy blocks when notice is unavailable', () => {
    renderDialogs(
      buildChat({
        handoffs: [
          {
            ...noticedHandoff,
            notice: null,
            context_summary: 'user: 网络断了',
            pending_question: '请协助处理网络故障。',
          },
        ],
      }),
    );

    expect(screen.getByText('上下文摘要')).toBeTruthy();
    expect(screen.getByText('请协助处理网络故障。')).toBeTruthy();
    expect(screen.queryByText('对话记录（自进入该SOP起）')).toBeNull();
  });
});
