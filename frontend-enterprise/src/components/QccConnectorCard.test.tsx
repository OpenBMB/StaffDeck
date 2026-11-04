// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api/client';
import { QccConnectorCard } from './QccConnectorCard';
vi.mock('../api/client', () => ({ TENANT_ID: 't', api: { get: vi.fn(), post: vi.fn() } }));
vi.mock('./McpOAuthControl', () => ({ McpOAuthControl: ({ onAuthorizedChange }: {
  onAuthorizedChange: (value: boolean) => void;
}) => <button onClick={() => onAuthorizedChange(true)}>Authorize test account</button> }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
describe('QCC connector', () => {
  it('does not offer server creation to members', async () => {
    vi.mocked(api.get).mockResolvedValue({ server_id: null, enabled: false });
    render(<QccConnectorCard canInstall={false} onCatalogChanged={() => {}} />);
    expect(await screen.findByText('请管理员添加企查查连接器，然后使用自己的账号授权。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '添加企查查连接器' })).toBeNull();
  });
  it('requires personal authorization before catalog discovery and shows actual count', async () => {
    vi.mocked(api.get).mockResolvedValue({ server_id: 's', enabled: true });
    vi.mocked(api.post).mockResolvedValue({ success: true, imported: ['a', 'b'], updated: [] });
    const changed = vi.fn();
    render(<QccConnectorCard canInstall onCatalogChanged={changed} />);
    const sync = await screen.findByRole('button', { name: '发现并更新工具目录' }) as HTMLButtonElement;
    expect(sync.disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Authorize test account' }));
    expect(sync.disabled).toBe(false);
    fireEvent.click(sync);
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('共 2 项'));
    expect(changed).toHaveBeenCalledOnce();
    expect(api.post).toHaveBeenCalledWith('/api/enterprise/mcp-servers/s/sync', { tenant_id: 't' });
  });
});
