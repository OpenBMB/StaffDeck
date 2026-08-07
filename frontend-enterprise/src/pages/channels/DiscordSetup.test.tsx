// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChannelBindingRead } from '../../types';
import DiscordSetup from './DiscordSetup';

const { notify } = vi.hoisted(() => ({ notify: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: (value: string) => value,
    locale: 'zh-CN',
    setLocale: () => {},
    toggleLocale: () => {},
  }),
  I18nProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('@/components/ui/app-toast', () => ({ notify }));

const updatedBinding: ChannelBindingRead = {
  id: 'chan_discord',
  tenant_id: 'tenant_demo',
  agent_id: 'agent_a',
  channel: 'discord',
  status: 'active',
  bot_id: 'bot-123',
  bot_name: 'StaffDeck Bot',
  config_revision: 1,
  connected: false,
  agents: [],
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

const baseBinding = {
  id: 'chan_discord',
  tenant_id: 'tenant_demo',
  agent_id: 'agent_a',
  channel: 'discord',
  status: 'active',
  config_revision: 0,
  connected: false,
  agents: [],
  created_at: '2026-08-07T00:00:00Z',
  updated_at: '2026-08-07T00:00:00Z',
};

const binding = (overrides: Partial<ChannelBindingRead> = {}): ChannelBindingRead => ({
  ...baseBinding,
  ...overrides,
});

const postMock = vi.hoisted(() => vi.fn());

vi.mock('../../api/client', () => ({
  api: {
    post: (...args: unknown[]) => postMock(...args),
  },
  TENANT_ID: 'tenant_demo',
}));

function renderSetup(b: ChannelBindingRead, onChanged: () => void = () => {}) {
  return render(<DiscordSetup binding={b} onChanged={onChanged} />);
}

beforeEach(() => {
  postMock.mockReset();
  notify.success.mockReset();
  notify.error.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('DiscordSetup', () => {
  it('renders configured state with bot id and without exposing the token', () => {
    renderSetup(binding({ bot_id: 'bot-123'}));

    expect(screen.getByText('凭证已配置')).toBeTruthy();
    expect(screen.getByText(/Bot ID：bot-123/)).toBeTruthy();
    expect(screen.getByText('未连接')).toBeTruthy();
    expect(screen.queryByText('Bot Token')).toBeNull();
  });

  it('posts the bot token to the discord credentials endpoint on save', async () => {
    postMock.mockResolvedValue(updatedBinding);
    const onChanged = vi.fn();

    renderSetup(binding(), onChanged);

    fireEvent.change(screen.getByLabelText('Bot Token'), { target: { value: 'token-abc' } });
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord/discord/credentials',
        { tenant_id: 'tenant_demo', bot_token: 'token-abc' },
      );
    });
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith('已保存'));
    expect(onChanged).toHaveBeenCalledWith(updatedBinding);
  });

  it('stays in edit state and reports the error when saving fails', async () => {
    postMock.mockRejectedValue(new Error('无效或已被吊销'));

    renderSetup(binding());

    fireEvent.change(screen.getByLabelText('Bot Token'), { target: { value: 'bad-token' } });
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('无效或已被吊销'));
    expect(screen.getByLabelText('Bot Token')).toBeTruthy();
  });

  it('rejects saving when the token is empty', () => {
    renderSetup(binding());

    fireEvent.click(screen.getByText('保存'));

    expect(notify.error).toHaveBeenCalledWith('请填写完整凭证');
    expect(postMock).not.toHaveBeenCalled();
  });
});
