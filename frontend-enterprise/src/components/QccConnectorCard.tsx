import { useEffect, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import { announceEnterpriseCapabilityCatalogChange } from '../lib/capability-catalog-events';
import { Button } from './ui/button';
import { McpOAuthControl } from './McpOAuthControl';

type Preset = { server_id: string | null; enabled: boolean };
export function QccConnectorCard({ canInstall, onCatalogChanged }: {
  canInstall: boolean; onCatalogChanged: () => void;
}) {
  const [preset, setPreset] = useState<Preset | null>(null);
  const [authorized, setAuthorized] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const endpoint = `/api/enterprise/mcp-presets/qcc?tenant_id=${encodeURIComponent(TENANT_ID)}`;
  useEffect(() => {
    let active = true;
    api.get<Preset>(endpoint).then(value => { if (active) setPreset(value); })
      .catch(() => { if (active) setMessage('无法读取企查查连接状态，请检查权限或已有同名配置。'); });
    return () => { active = false; };
  }, [endpoint]);
  async function install() {
    setBusy(true); setMessage('');
    try {
      setPreset(await api.post<Preset>(endpoint, {}));
      onCatalogChanged();
    } catch { setMessage('无法创建企查查连接，请检查权限或已有同名配置。'); }
    finally { setBusy(false); }
  }
  async function sync() {
    if (!preset?.server_id) return;
    setBusy(true); setMessage('');
    try {
      const result = await api.post<{ success: boolean; imported: string[]; updated: string[] }>(
        `/api/enterprise/mcp-servers/${encodeURIComponent(preset.server_id)}/sync`,
        { tenant_id: TENANT_ID },
      );
      if (!result.success) throw new Error('Discovery failed');
      setMessage(`工具目录已更新，共 ${result.imported.length + result.updated.length} 项；模型按需搜索和加载。`);
      announceEnterpriseCapabilityCatalogChange({ resourceType: 'tool' });
      onCatalogChanged();
    } catch { setMessage('工具发现失败，请确认已授权及账号统一入口权限后重试。'); }
    finally { setBusy(false); }
  }
  return <section aria-label="企查查 MCP" className="rounded-xl border p-4 flex flex-col gap-3">
    <h2 className="font-semibold">企查查 MCP · OAuth</h2>
    <p>一个统一入口，发现账号可用的企业及历史数据工具。通过能力搜索按需加载，无需粘贴 API Key。</p>
    <p>工具数量由账号权限和服务端准入决定。授权不会自动开放额外数据权限。</p>
    {preset && !preset.server_id && (canInstall
      ? <Button disabled={busy} onClick={() => void install()}>添加企查查连接器</Button>
      : <p>请管理员添加企查查连接器，然后使用自己的账号授权。</p>)}
    {preset?.server_id && preset.enabled && <>
      <McpOAuthControl serverId={preset.server_id} onAuthorizedChange={setAuthorized} />
      {canInstall && <Button disabled={busy || !authorized} onClick={() => void sync()}>
        发现并更新工具目录
      </Button>}
      <p>管理员更新目录后，为数字员工绑定所需工具；对话中通过能力搜索加载具体工具。</p>
    </>}
    {preset?.server_id && !preset.enabled && <p>连接器已停用，请管理员在 MCP 服务器设置中启用。</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
