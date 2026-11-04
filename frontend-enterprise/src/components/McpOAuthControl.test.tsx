// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { McpOAuthControl } from './McpOAuthControl';
import { api } from '../api/client';

vi.mock('../api/client', () => ({ TENANT_ID: 'tenant', api: {
  get: vi.fn(), post: vi.fn(), delete: vi.fn(),
} }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe('MCP OAuth controls', () => {
  it('provides a visible manual authorization link without opening a popup', async () => {
    vi.mocked(api.get).mockResolvedValue({ authorized: false, pending: true });
    vi.mocked(api.post).mockResolvedValue({ authorization_url: 'https://auth.example/authorize' });
    const popup = vi.spyOn(window, 'open');
    render(<McpOAuthControl serverId="server" />);
    fireEvent.click(screen.getByRole('button', { name: '准备 OAuth 授权' }));
    const link = await screen.findByRole('link', { name: '打开登录授权页' });
    expect(link.getAttribute('href')).toBe('https://auth.example/authorize');
    expect(popup).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '复制授权链接' })).toBeTruthy();
    popup.mockRestore();
  });
  it('reports start failures and never shows a stale authorization link', async () => {
    vi.mocked(api.get).mockResolvedValue({ authorized: false, pending: false });
    vi.mocked(api.post).mockRejectedValue(new Error('failure'));
    render(<McpOAuthControl serverId="server" />);
    fireEvent.click(screen.getByRole('button', { name: '准备 OAuth 授权' }));
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });
  it('removes authorized status on disconnect and reports remote revocation', async () => {
    vi.mocked(api.get).mockResolvedValue({ authorized: true, pending: false });
    vi.mocked(api.delete).mockResolvedValue({ remote_revocation_confirmed: true });
    render(<McpOAuthControl serverId="server" />);
    fireEvent.click(await screen.findByRole('button', { name: '解除 / 取消授权' }));
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('已解绑并撤销授权。'));
    expect(screen.getByText('OAuth 尚未授权')).toBeTruthy();
  });
});
