import { useEffect, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import { Button } from './ui/button';

export function McpOAuthControl({ serverId, onAuthorizedChange }: {
  serverId: string; onAuthorizedChange?: (authorized: boolean) => void;
}) {
  const [authorized, setAuthorized] = useState(false);
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  useEffect(() => { onAuthorizedChange?.(authorized); }, [authorized, onAuthorizedChange]);
  const endpoint = `/api/enterprise/mcp-oauth/${encodeURIComponent(serverId)}`;
  const query = `tenant_id=${encodeURIComponent(TENANT_ID)}`;
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const result = await api.get<{ authorized: boolean; pending: boolean }>(`${endpoint}/status?${query}`);
        if (!active) return;
        setAuthorized(result.authorized);
        if (result.authorized && !result.pending) setUrl('');
      } catch { /* Keep the last known status; explicit actions show failures. */ }
    };
    void refresh();
    const timer = url ? window.setInterval(() => void refresh(), 3000) : undefined;
    return () => { active = false; window.clearInterval(timer); };
  }, [endpoint, query, url]);
  async function start() {
    setBusy(true); setMessage(''); setUrl('');
    try {
      const result = await api.post<{ authorization_url: string }>(`${endpoint}/start?${query}`, {});
      setUrl(result.authorization_url);
    } catch { setMessage('无法准备授权，请检查服务端 OAuth 配置后重试。'); }
    finally { setBusy(false); }
  }
  async function disconnect() {
    setBusy(true);
    try {
      const result = await api.delete<{ remote_revocation_confirmed: boolean }>(`${endpoint}?${query}`);
      setAuthorized(false); setUrl('');
      setMessage(result.remote_revocation_confirmed ? '已解绑并撤销授权。' : '本地凭据已移除；远端撤销未确认，请到服务方撤销授权。');
    } catch { setMessage('解绑失败，请重试。'); }
    finally { setBusy(false); }
  }
  return <div className="flex flex-col gap-2 text-sm">
    <span>{authorized ? 'OAuth 已授权' : 'OAuth 尚未授权'}</span>
    <Button variant="outline" size="sm" disabled={busy} onClick={() => void start()}>
      {busy ? '处理中…' : '准备 OAuth 授权'}
    </Button>
    {url && <>
      <a href={url} target="_blank" rel="noreferrer" className="font-semibold underline">打开登录授权页</a>
      <p>请用系统浏览器授权。若桌面窗口未打开，请复制链接到系统浏览器；完成后返回此页。</p>
      <Button variant="outline" size="sm" onClick={() => {
        void navigator.clipboard.writeText(url).then(() => setMessage('授权链接已复制。'),
          () => setMessage('复制失败，请右键复制上方授权链接。'));
      }}>复制授权链接</Button>
    </>}
    {(authorized || url) && <Button variant="outline" size="sm" disabled={busy}
      onClick={() => void disconnect()}>解除 / 取消授权</Button>}
    {message && <p role="status">{message}</p>}
  </div>;
}
