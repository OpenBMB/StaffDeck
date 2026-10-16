// @vitest-environment jsdom

/**
 * 安装页「当前支持的报单场景」：清单来自后端配置下发（/oa-assistant/scenarios）。
 * 这里只验证：拿到清单就逐条展示（名称 + 报销场景 + 一句介绍），拿不到不崩。
 */
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import OaAssistantPage from './OaAssistantPage';

const getMock = vi.fn();

vi.mock('@/api/client', () => ({
  API_BASE_URL: 'http://api.test',
  api: { get: (...args: unknown[]) => getMock(...args) },
}));

describe('OaAssistantPage 报单场景', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  it('按后端下发展示场景名称、报销场景与介绍', async () => {
    getMock.mockResolvedValue({
      items: [
        { id: 'communication', name: '通讯报销', summary: '通讯费报销：报销场景固定「通讯费用」。', scene: '通讯费用' },
        { id: 'company_payment', name: '境内公司付款', summary: '一张发票一行明细。', scene: '' },
      ],
    });
    render(
      <I18nProvider>
        <MemoryRouter>
          <OaAssistantPage />
        </MemoryRouter>
      </I18nProvider>,
    );

    expect(await screen.findByText('当前支持的报单场景')).toBeTruthy();
    expect(await screen.findByText('通讯报销')).toBeTruthy();
    expect(screen.getByText('报销场景：通讯费用')).toBeTruthy();
    expect(screen.getByText('通讯费报销：报销场景固定「通讯费用」。')).toBeTruthy();
    expect(screen.getByText('境内公司付款')).toBeTruthy();
    expect(screen.getByText('共 2 个')).toBeTruthy();
    expect(getMock).toHaveBeenCalledWith('/api/enterprise/oa-assistant/scenarios');
  });

  it('接口失败时给出提示而不是白屏', async () => {
    getMock.mockRejectedValue(new Error('boom'));
    render(
      <I18nProvider>
        <MemoryRouter>
          <OaAssistantPage />
        </MemoryRouter>
      </I18nProvider>,
    );
    await waitFor(() => expect(screen.getByText('场景清单加载失败，请刷新重试。')).toBeTruthy());
  });
});
