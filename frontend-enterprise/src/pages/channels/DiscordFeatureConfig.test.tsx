// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ChannelBindingRead,
  ChannelBatchJobRead,
  ChannelMetaRead,
} from '../../types';
import DiscordFeatureConfig from './DiscordFeatureConfig';

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

const putMock = vi.hoisted(() => vi.fn());
const postMock = vi.hoisted(() => vi.fn());
const getMock = vi.hoisted(() => vi.fn());

vi.mock('../../api/client', () => ({
  api: {
    put: (...args: unknown[]) => putMock(...args),
    post: (...args: unknown[]) => postMock(...args),
    get: (...args: unknown[]) => getMock(...args),
  },
  TENANT_ID: 'tenant_demo',
}));

const discordMeta: ChannelMetaRead = {
  channel: 'discord',
  name: 'Discord',
  setup: 'credentials',
  capabilities: [],
};

const baseBinding: ChannelBindingRead = {
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

function renderConfig(
  b: ChannelBindingRead,
  meta: ChannelMetaRead = discordMeta,
  onChanged: () => void = () => {},
) {
  return render(<DiscordFeatureConfig binding={b} meta={meta} onChanged={onChanged} />);
}

beforeEach(() => {
  putMock.mockReset();
  postMock.mockReset();
  getMock.mockReset();
  notify.success.mockReset();
  notify.error.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('DiscordFeatureConfig', () => {
  it('renders feature toggles defaulting to on except voice, plus allowlist fields', () => {
    renderConfig(binding());

    expect(screen.getByText('功能配置')).toBeTruthy();
    expect(screen.getByText('原生斜杠命令')).toBeTruthy();
    expect(screen.getByText('语音（需 ffmpeg）')).toBeTruthy();
    expect(screen.getByText('权限白名单')).toBeTruthy();
    expect(screen.getByLabelText('允许的服务器 ID')).toBeTruthy();
    expect(screen.getByLabelText('允许的频道 ID')).toBeTruthy();
    expect(screen.getByLabelText('允许的用户 ID')).toBeTruthy();
    expect(screen.getByLabelText('拒绝列表（每行一个 ID）')).toBeTruthy();

    const switches = screen.getAllByRole('switch');
    expect(switches).toHaveLength(8);
    // voice 默认关闭，其余默认开启
    expect(switches[6].getAttribute('data-state')).toBe('unchecked');
    expect(switches[0].getAttribute('data-state')).toBe('checked');
  });

  it('restricts toggles to meta capabilities when declared', () => {
    renderConfig(
      binding(),
      { ...discordMeta, capabilities: ['slash_commands', 'voice'] },
    );

    expect(screen.getByText('原生斜杠命令')).toBeTruthy();
    expect(screen.getByText('语音（需 ffmpeg）')).toBeTruthy();
    expect(screen.queryByText('线程')).toBeNull();
    expect(screen.queryByText('富媒体（嵌入与附件）')).toBeNull();
  });

  it('pre-fills allowlist and feature state from binding config_json', () => {
    renderConfig(
      binding({
        config_json: {
          features: { slash_commands: false, voice: true },
          allowlist: {
            mode: 'deny_all',
            guild_ids: ['111'],
            channel_ids: ['222'],
            user_ids: ['333'],
            deny: ['444', '555'],
          },
        },
      }),
    );

    const switches = screen.getAllByRole('switch');
    expect(switches[0].getAttribute('data-state')).toBe('unchecked');
    expect(switches[6].getAttribute('data-state')).toBe('checked');
    expect((screen.getByLabelText('允许的服务器 ID') as HTMLTextAreaElement).value).toBe('111');
    expect((screen.getByLabelText('允许的频道 ID') as HTMLTextAreaElement).value).toBe('222');
    expect((screen.getByLabelText('允许的用户 ID') as HTMLTextAreaElement).value).toBe('333');
    expect((screen.getByLabelText('拒绝列表（每行一个 ID）') as HTMLTextAreaElement).value).toBe(
      '444\n555',
    );
  });

  it('pre-fills auto_thread from config_json', () => {
    renderConfig(
      binding({
        config_json: {
          features: { auto_thread: true },
        },
      }),
    );

    expect(screen.getByText('自动创建线程')).toBeTruthy();
    // auto_thread 位于 threads 之后(index 2),配置为 true 时开关勾选
    expect(screen.getAllByRole('switch')[2].getAttribute('data-state')).toBe('checked');
  });

  it('saves features and allowlist through the binding update endpoint', async () => {
    putMock.mockResolvedValue(binding());
    renderConfig(binding());

    fireEvent.click(screen.getAllByRole('switch')[6]); // 打开 voice
    fireEvent.change(screen.getByLabelText('允许的服务器 ID'), {
      target: { value: 'guild-1\nguild-2' },
    });
    fireEvent.change(screen.getByLabelText('拒绝列表（每行一个 ID）'), {
      target: { value: 'deny-channel' },
    });
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => {
      expect(putMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord?tenant_id=tenant_demo',
        {
          tenant_id: 'tenant_demo',
          // batch_send 为前端保留开关，不随保存下发（后端 ChannelFeaturesConfig 无该键）
          features: expect.objectContaining({
            slash_commands: true,
            voice: true,
            backfill: true,
            rich_media: true,
          }),
          allowlist: {
            mode: 'allow_all',
            guild_ids: ['guild-1', 'guild-2'],
            channel_ids: [],
            user_ids: [],
            deny: ['deny-channel'],
          },
        },
      );
      expect((putMock.mock.calls[0][1] as Record<string, unknown>).features).not.toHaveProperty(
        'batch_send',
      );
    });
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith('已保存'));
  });

  it('saves auto_thread through the binding update endpoint', async () => {
    putMock.mockResolvedValue(binding());
    renderConfig(binding());

    fireEvent.click(screen.getAllByRole('switch')[2]); // 打开自动创建线程
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => {
      expect(putMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord?tenant_id=tenant_demo',
        {
          tenant_id: 'tenant_demo',
          features: expect.objectContaining({ auto_thread: true }),
          allowlist: expect.anything(),
        },
      );
    });
  });

  it('switches to strict mode and reports save failure', async () => {
    putMock.mockRejectedValue(new Error('无有效更新内容'));
    renderConfig(binding());

    fireEvent.click(screen.getByText('仅允许列表内的消息'));
    fireEvent.click(screen.getByText('保存'));

    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('无有效更新内容'));
  });

  it('triggers a manual backfill and reports success', async () => {
    postMock.mockResolvedValue({ job_id: 'bf-1', status: 'pending' });
    renderConfig(binding());

    fireEvent.click(screen.getByText('手动回填'));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord/backfill',
        { tenant_id: 'tenant_demo' },
      );
    });
    await waitFor(() => expect(notify.success).toHaveBeenCalledWith('回填已触发'));
  });

  it('passes the target channel id to backfill when provided', async () => {
    postMock.mockResolvedValue({ job_id: 'bf-1', status: 'pending' });
    renderConfig(binding());

    fireEvent.change(screen.getByLabelText('目标频道 ID（可选）'), {
      target: { value: 'channel-9' },
    });
    fireEvent.click(screen.getByText('手动回填'));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord/backfill',
        { tenant_id: 'tenant_demo', channel_id: 'channel-9' },
      );
    });
  });

  it('degrades gracefully when the backfill endpoint is unavailable', async () => {
    postMock.mockRejectedValue(new Error('Not Found'));
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    renderConfig(binding());

    fireEvent.click(screen.getByText('手动回填'));

    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('Not Found'));
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  it('posts batch items, polls the job and reports progress', async () => {
    postMock.mockResolvedValue({ job_id: 'job-1', status: 'pending' } satisfies ChannelBatchJobRead);
    getMock.mockResolvedValueOnce({
      job_id: 'job-1',
      status: 'running',
      progress: 1,
      total: 2,
    } satisfies ChannelBatchJobRead);

    renderConfig(binding());

    fireEvent.change(screen.getByLabelText('批量发送消息（每行一条）'), {
      target: { value: '第一条\n第二条' },
    });
    fireEvent.click(screen.getByText('发送'));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/enterprise/channels/chan_discord/batch', {
        tenant_id: 'tenant_demo',
        items: ['第一条', '第二条'],
      });
    });
    await waitFor(() => {
      expect(getMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord/batch/job-1?tenant_id=tenant_demo',
      );
    });
    await waitFor(() => expect(screen.getByText('批处理进度：1 / 2')).toBeTruthy());
  });

  it('passes the target channel id as a query parameter for batch', async () => {
    postMock.mockResolvedValue({ job_id: 'job-2', status: 'done' } satisfies ChannelBatchJobRead);
    renderConfig(binding());

    fireEvent.change(screen.getByLabelText('目标频道 ID（可选）'), {
      target: { value: 'channel-7' },
    });
    fireEvent.change(screen.getByLabelText('批量发送消息（每行一条）'), {
      target: { value: 'hello' },
    });
    fireEvent.click(screen.getByText('发送'));

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith(
        '/api/enterprise/channels/chan_discord/batch?channel_id=channel-7',
        { tenant_id: 'tenant_demo', items: ['hello'] },
      );
    });
  });

  it('refuses an empty batch', () => {
    renderConfig(binding());

    fireEvent.click(screen.getByText('发送'));

    expect(notify.error).toHaveBeenCalledWith('请先输入至少一条消息');
    expect(postMock).not.toHaveBeenCalled();
  });
});
