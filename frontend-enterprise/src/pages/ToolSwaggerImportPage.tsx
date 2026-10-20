import { useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
  CheckCircle2,
  FileUp,
  Layers,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UploadCloud,
  XCircle,
} from 'lucide-react';

import { api, TENANT_ID } from '../api/client';
import AppHeader from '@/components/AppHeader';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { Button as UIButton } from '@/components/ui/button';
import { Checkbox, Input, Switch, Textarea } from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { JsonTextarea } from '@/components/JsonTextarea';
import { announceEnterpriseCapabilityCatalogChange } from '@/lib/capability-catalog-events';
import { readEmployeeScope } from '../lib/agent-scope-storage';
import type {
  EnterpriseAuthUser,
} from '../auth';
import type {
  SwaggerImportResponse,
  SwaggerParseResponse,
  SwaggerToolDraft,
  ToolRead,
} from '../types';

type SwaggerImportPageProps = {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

type EditableDraft = SwaggerToolDraft & {
  selected: boolean;
};

const CARD_CLASS = 'rounded-[16px] border border-[#eceef1] bg-white';
const CARD_TITLE_CLASS = 'text-[14px] font-medium text-[#18181a]';
const LABEL_CLASS = 'text-[12px] font-medium text-[#18181a]';
const HINT_CLASS = 'text-[12px] leading-[1.55] text-[#858b9c]';
const MONO_CLASS = 'font-mono text-[12px] leading-[1.65]';
const RETURN_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]';
const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';

function agentQuery() {
  const agentId = readEmployeeScope();
  return agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
}

function agentUpdateQuery() {
  const agentId = readEmployeeScope();
  return agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
}

function parseJsonObject(value: string): Record<string, unknown> {
  if (!value.trim()) return {};
  const parsed = JSON.parse(value);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('必须是 JSON 对象');
  }
  return parsed as Record<string, unknown>;
}

function methodTone(method: string) {
  if (method === 'GET') return 'green' as const;
  if (method === 'DELETE') return 'red' as const;
  if (method === 'POST') return 'blue' as const;
  return 'gray' as const;
}

function schemaFieldCount(schema: Record<string, unknown>) {
  const properties = schema?.properties;
  return properties && typeof properties === 'object' ? Object.keys(properties).length : 0;
}

function updatePayloadFromTool(tool: ToolRead, enabled: boolean) {
  return {
    tenant_id: TENANT_ID,
    name: tool.name,
    display_name: tool.display_name,
    description: tool.description,
    bucket: tool.bucket || '未分组',
    tool_type: tool.tool_type,
    method: tool.method,
    url: tool.url,
    headers: tool.headers || {},
    auth: tool.auth || {},
    mcp_config: tool.mcp_config || {},
    execution_policy: tool.execution_policy || undefined,
    input_schema: tool.input_schema || {},
    output_schema: tool.output_schema || {},
    allowed_skills: tool.allowed_skills || [],
    capability_scope: tool.capability_scope || 'general',
    enabled,
  };
}

export default function ToolSwaggerImportPage({ currentUser, onLogout }: SwaggerImportPageProps = {}) {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [sourceText, setSourceText] = useState('');
  const [sourceName, setSourceName] = useState('');
  const [baseUrlOverride, setBaseUrlOverride] = useState('');
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState<SwaggerParseResponse | null>(null);
  const [drafts, setDrafts] = useState<EditableDraft[]>([]);
  const [group, setGroup] = useState('Swagger 导入');
  const [globalHeaders, setGlobalHeaders] = useState('{}');
  const [globalAuth, setGlobalAuth] = useState('{}');
  const [importEnabled, setImportEnabled] = useState(true);
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<SwaggerImportResponse | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);
  const [selectedImportedNames, setSelectedImportedNames] = useState<string[]>([]);
  const [toolEnabledOverrides, setToolEnabledOverrides] = useState<Record<string, boolean>>({});
  const [deletedToolIds, setDeletedToolIds] = useState<string[]>([]);

  const selectedDrafts = useMemo(() => drafts.filter((item) => item.selected), [drafts]);
  const selectedCount = selectedDrafts.length;
  const allSelected = drafts.length > 0 && selectedCount === drafts.length;
  const importedItems = useMemo(
    () =>
      (result?.items || []).filter(
        (item) => item.status === 'imported' && item.tool_id && !deletedToolIds.includes(item.tool_id),
      ),
    [deletedToolIds, result],
  );
  const selectedImportedItems = importedItems.filter((item) =>
    selectedImportedNames.includes(item.name),
  );

  async function readFile(file: File) {
    try {
      const text = await file.text();
      setSourceText(text);
      setSourceName(file.name);
      setParsed(null);
      setDrafts([]);
      setResult(null);
      notify.success(`已读取 ${file.name}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '读取文件失败');
    }
  }

  async function parseSpec() {
    if (!sourceText.trim()) {
      notify.warning('请先选择文件或粘贴 Swagger JSON/YAML');
      return;
    }
    setParsing(true);
    setResult(null);
    try {
      const response = await api.post<SwaggerParseResponse>('/api/enterprise/tools/swagger/parse', {
        tenant_id: TENANT_ID,
        spec: sourceText,
        default_group: group,
        base_url: baseUrlOverride.trim() || undefined,
      });
      if (!response.success) {
        notify.error(response.error?.message || '解析 Swagger 失败');
        return;
      }
      setParsed(response);
      setGroup(response.group || 'Swagger 导入');
      setDrafts(
        response.endpoints.map((endpoint) => ({
          ...endpoint,
          group: response.group || endpoint.group,
          selected: true,
        })),
      );
      setSelectedImportedNames([]);
      notify.success(`已解析 ${response.endpoints.length} 个接口`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '解析 Swagger 失败');
    } finally {
      setParsing(false);
    }
  }

  function patchDraft(index: number, patch: Partial<EditableDraft>) {
    setDrafts((previous) =>
      previous.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    );
  }

  function toggleAll(checked: boolean) {
    setDrafts((previous) => previous.map((item) => ({ ...item, selected: checked })));
  }

  async function importSelected() {
    if (!parsed || selectedDrafts.length === 0) {
      notify.warning('请至少选择一个接口');
      return;
    }
    let headersPatch: Record<string, unknown>;
    let authPatch: Record<string, unknown>;
    try {
      headersPatch = parseJsonObject(globalHeaders);
      authPatch = parseJsonObject(globalAuth);
    } catch (error) {
      notify.error(error instanceof Error ? `统一 Headers/Auth 格式错误：${error.message}` : '统一 Headers/Auth 格式错误');
      return;
    }
    const emptyNames = selectedDrafts.filter((item) => !item.name.trim());
    if (emptyNames.length > 0) {
      notify.error('存在未填写工具名称的接口，请先补全');
      return;
    }
    setImporting(true);
    try {
      const response = await api.post<SwaggerImportResponse>(
        `/api/enterprise/tools/swagger/import?tenant_id=${TENANT_ID}${agentQuery()}`,
        {
          tenant_id: TENANT_ID,
          group: group.trim() || parsed.group,
          enabled: importEnabled,
          endpoints: selectedDrafts.map((draft) => ({
            ...draft,
            name: draft.name.trim(),
            display_name: draft.display_name?.trim() || draft.name.trim(),
            group: group.trim() || draft.group,
            headers: Object.fromEntries(
              Object.entries({ ...draft.headers, ...headersPatch }).map(([key, value]) => [
                key,
                String(value),
              ]),
            ),
            auth: Object.keys(authPatch).length > 0 ? authPatch : draft.auth,
          })),
        },
      );
      setResult(response);
      setToolEnabledOverrides({});
      setDeletedToolIds([]);
      setSelectedImportedNames(
        response.items.filter((item) => item.status === 'imported').map((item) => item.name),
      );
      if (response.imported > 0) {
        notify.success(`已导入 ${response.imported} 个工具`);
        announceEnterpriseCapabilityCatalogChange({
          resourceType: 'tool',
          agentId: readEmployeeScope() || undefined,
        });
      } else {
        notify.warning('没有工具被导入，请检查名称是否重复');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '批量导入失败');
    } finally {
      setImporting(false);
    }
  }

  async function batchSetEnabled(enabled: boolean) {
    if (selectedImportedItems.length === 0) {
      notify.warning('请先选择要批量操作的已导入工具');
      return;
    }
    setBatchLoading(true);
    try {
      for (const item of selectedImportedItems) {
        if (!item.tool_id) continue;
        const row = await api.get<ToolRead>(
          `/api/enterprise/tools/${item.tool_id}?tenant_id=${TENANT_ID}${agentQuery()}`,
        );
        await api.put(
          `/api/enterprise/tools/${item.tool_id}${agentUpdateQuery()}`,
          updatePayloadFromTool(row, enabled),
        );
      }
      setToolEnabledOverrides((previous) => {
        const next = { ...previous };
        for (const item of selectedImportedItems) {
          if (item.tool_id) next[item.tool_id] = enabled;
        }
        return next;
      });
      notify.success(`已${enabled ? '启用' : '停用'} ${selectedImportedItems.length} 个工具`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '批量操作失败');
    } finally {
      setBatchLoading(false);
    }
  }

  async function batchDelete() {
    if (selectedImportedItems.length === 0) {
      notify.warning('请先选择要删除的已导入工具');
      return;
    }
    setBatchLoading(true);
    try {
      for (const item of selectedImportedItems) {
        if (!item.tool_id) continue;
        await api.delete(
          `/api/enterprise/tools/${item.tool_id}?tenant_id=${TENANT_ID}${agentQuery()}`,
        );
      }
      setDeletedToolIds((previous) => [
        ...previous,
        ...selectedImportedItems.map((item) => item.tool_id).filter((value): value is string => Boolean(value)),
      ]);
      setSelectedImportedNames([]);
      notify.success(`已删除 ${selectedImportedItems.length} 个工具`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '批量删除失败');
    } finally {
      setBatchLoading(false);
    }
  }

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        onLogout={onLogout}
        userName={currentUser?.username}
        title="从 Swagger 导入工具"
        description="上传 OpenAPI 3 / Swagger 2 文件，解析后批量编辑并导入 HTTP 工具。"
      />
      <div className="mt-[20px] mb-[16px] flex flex-wrap justify-end gap-[16px]">
        <UIButton
          variant="outline"
          onClick={() => navigate('/enterprise/tools')}
          className={RETURN_BUTTON_CLASS}
        >
          <ArrowLeft className="size-3.5" />
          返回工具管理
        </UIButton>
        {parsed && (
          <UIButton
            disabled={importing || selectedCount === 0}
            onClick={() => void importSelected()}
            className={PRIMARY_BUTTON_CLASS}
          >
            <UploadCloud className="size-3.5" />
            {importing ? '导入中…' : `导入 ${selectedCount} 个接口`}
          </UIButton>
        )}
      </div>

      <div className="grid grid-cols-1 gap-[16px] xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <section className={`${CARD_CLASS} overflow-hidden`}>
          <div className="flex min-h-[54px] items-center justify-between border-b border-[#eceef1] px-[20px]">
            <span className={CARD_TITLE_CLASS}>1. 选择 Swagger 文件</span>
            {sourceName && <StatusBadge tone="gray">{sourceName}</StatusBadge>}
          </div>
          <div className="flex flex-col gap-[14px] p-[20px]">
            <input
              ref={fileInputRef}
              type="file"
              accept=".json,.yaml,.yml,application/json,application/yaml,text/yaml"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void readFile(file);
                event.target.value = '';
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="flex min-h-[132px] flex-col items-center justify-center gap-[10px] rounded-[14px] border border-dashed border-[#d9dee9] bg-[#fafbfc] px-[18px] text-center transition-colors hover:border-[#18181a] hover:bg-white"
            >
              <span className="grid size-[38px] place-items-center rounded-full bg-white text-[#18181a] shadow-[0_2px_8px_rgba(24,24,26,0.08)]">
                <FileUp className="size-[18px]" />
              </span>
              <span className="text-[13px] font-medium text-[#18181a]">上传 .json / .yaml / .yml</span>
              <span className={HINT_CLASS}>也可以把 Swagger 内容直接粘贴到下方</span>
            </button>
            <div className="flex flex-col gap-[6px]">
              <label className={LABEL_CLASS} htmlFor="swagger-source-text">
                Swagger 内容
              </label>
              <Textarea
                id="swagger-source-text"
                rows={10}
                className={MONO_CLASS}
                value={sourceText}
                placeholder={'openapi: 3.0.3\ninfo:\n  title: 订单服务\npaths:\n  ...'}
                onChange={(event) => {
                  setSourceText(event.target.value);
                  setSourceName(event.target.value ? '手动粘贴' : '');
                  setParsed(null);
                  setDrafts([]);
                  setResult(null);
                }}
              />
            </div>
            <div className="flex flex-col gap-[6px]">
              <label className={LABEL_CLASS} htmlFor="swagger-base-url">
                Base URL 覆盖（可选）
              </label>
              <Input
                id="swagger-base-url"
                value={baseUrlOverride}
                onChange={(event) => setBaseUrlOverride(event.target.value)}
                placeholder="https://api.example.com/v1"
              />
              <span className={HINT_CLASS}>留空时使用文件中的 servers / host + basePath。</span>
            </div>
            <UIButton
              disabled={parsing}
              onClick={() => void parseSpec()}
              className={PRIMARY_BUTTON_CLASS}
            >
              <RefreshCw className={`size-3.5 ${parsing ? 'animate-spin' : ''}`} />
              {parsing ? '解析中…' : '解析并预览接口'}
            </UIButton>
            <p className={HINT_CLASS}>
              支持 OpenAPI 3（servers / components.securitySchemes）与 Swagger 2（host / basePath / securityDefinitions）。
              会自动生成 Path / Query / Header / Body 参数 Schema，并从 security 定义填充 Auth 占位符。
            </p>
          </div>
        </section>

        <div className="flex min-w-0 flex-col gap-[16px]">
          {!parsed ? (
            <section className={`${CARD_CLASS} grid min-h-[320px] place-items-center p-[24px]`}>
              <div className="flex max-w-[420px] flex-col items-center gap-[10px] text-center">
                <Layers className="size-[30px] text-[#c0c6d4]" />
                <strong className="text-[14px] text-[#18181a]">解析后在这里批量处理接口</strong>
                <p className={HINT_CLASS}>
                  一次导入多个接口：统一设置分组、Headers 与 Auth，逐条确认工具名和展示名，再批量启用或停用。
                </p>
              </div>
            </section>
          ) : (
            <>
              <section className={`${CARD_CLASS} overflow-hidden`}>
                <div className="flex flex-wrap items-center justify-between gap-[10px] border-b border-[#eceef1] px-[20px] py-[14px]">
                  <span className={CARD_TITLE_CLASS}>2. 解析结果</span>
                  <div className="flex flex-wrap items-center gap-[6px]">
                    <StatusBadge tone="blue">{parsed.title}{parsed.version ? ` v${parsed.version}` : ''}</StatusBadge>
                    <StatusBadge tone="gray">{parsed.endpoints.length} 个接口</StatusBadge>
                    <StatusBadge tone={selectedCount > 0 ? 'green' : 'gray'}>{selectedCount} 个已选</StatusBadge>
                  </div>
                </div>
                <div className="grid gap-[12px] p-[20px] md:grid-cols-2">
                  <div className="flex flex-col gap-[6px]">
                    <label className={LABEL_CLASS} htmlFor="swagger-group">导入分组</label>
                    <Input
                      id="swagger-group"
                      value={group}
                      onChange={(event) => {
                        const next = event.target.value;
                        setGroup(next);
                        setDrafts((previous) => previous.map((item) => ({ ...item, group: next })));
                      }}
                      placeholder="例如：订单服务"
                    />
                    <span className={HINT_CLASS}>所有导入工具会归入同一分组，后续可在工具管理中筛选。</span>
                  </div>
                  <div className="flex flex-col justify-center gap-[6px] rounded-[12px] border border-[#eceef1] bg-[#fafbfc] px-[14px] py-[10px]">
                    <div className="flex items-center justify-between gap-[12px]">
                      <span>
                        <span className="block text-[12px] font-medium text-[#464c5e]">导入后立即启用</span>
                        <span className="mt-[2px] block text-[11px] text-[#858b9c]">关闭后工具会先以停用状态创建。</span>
                      </span>
                      <Switch checked={importEnabled} onCheckedChange={setImportEnabled} />
                    </div>
                    <div className="flex items-center gap-[6px] text-[11px] text-[#858b9c]">
                      <ShieldCheck className="size-[13px]" />
                      Auth 占位符使用 {`\${secret.API_TOKEN}`} 形式，运行时从环境变量读取。
                    </div>
                  </div>
                  <JsonTextarea
                    label="统一 Headers（可选）"
                    rows={4}
                    value={globalHeaders}
                    onChange={setGlobalHeaders}
                    placeholder={'{\n  "X-Tenant": "demo"\n}'}
                  />
                  <JsonTextarea
                    label="统一 Auth（可选）"
                    rows={4}
                    value={globalAuth}
                    onChange={setGlobalAuth}
                    placeholder={'{\n  "type": "bearer",\n  "token": "${secret.API_TOKEN}"\n}'}
                  />
                </div>
                {parsed.warnings.length > 0 && (
                  <div className="mx-[20px] mb-[16px] rounded-[12px] border border-amber-200 bg-amber-50 px-[14px] py-[10px] text-[12px] leading-[1.6] text-amber-800">
                    {parsed.warnings.map((warning) => <div key={warning}>• {warning}</div>)}
                  </div>
                )}
              </section>

              <section className={`${CARD_CLASS} overflow-hidden`}>
                <div className="flex flex-wrap items-center justify-between gap-[10px] border-b border-[#eceef1] px-[20px] py-[12px]">
                  <label className="flex items-center gap-[8px] text-[13px] font-medium text-[#18181a]">
                    <Checkbox
                      checked={allSelected ? true : selectedCount > 0 ? 'indeterminate' : false}
                      onCheckedChange={(checked) => toggleAll(checked === true)}
                      aria-label="全选接口"
                    />
                    3. 选择并编辑接口
                  </label>
                  <span className={HINT_CLASS}>工具名全局唯一；重名会在导入结果中标记为跳过。</span>
                </div>
                <div className="max-h-[560px] overflow-auto">
                  <table className="w-full min-w-[760px] border-collapse text-left">
                    <thead className="sticky top-0 z-10 bg-[#fafbfc] text-[11px] text-[#858b9c]">
                      <tr>
                        <th className="w-[46px] px-[14px] py-[10px] font-medium">选择</th>
                        <th className="w-[82px] px-[10px] py-[10px] font-medium">方法</th>
                        <th className="min-w-[190px] px-[10px] py-[10px] font-medium">路径 / 接口</th>
                        <th className="w-[180px] px-[10px] py-[10px] font-medium">工具名</th>
                        <th className="w-[180px] px-[10px] py-[10px] font-medium">展示名</th>
                        <th className="w-[110px] px-[10px] py-[10px] font-medium">字段</th>
                      </tr>
                    </thead>
                    <tbody>
                      {drafts.map((draft, index) => (
                        <tr
                          key={`${draft.method}-${draft.path}-${index}`}
                          className={`border-t border-[#f0f2f5] align-top ${draft.selected ? 'bg-white' : 'bg-[#fcfcfd] opacity-70'}`}
                        >
                          <td className="px-[14px] py-[12px]">
                            <Checkbox
                              checked={draft.selected}
                              onCheckedChange={(checked) =>
                                patchDraft(index, { selected: checked === true })
                              }
                              aria-label={`选择 ${draft.name}`}
                            />
                          </td>
                          <td className="px-[10px] py-[12px]">
                            <StatusBadge tone={methodTone(draft.method)}>{draft.method}</StatusBadge>
                          </td>
                          <td className="px-[10px] py-[12px]">
                            <div className="flex flex-col gap-[3px]">
                              <code className="font-mono text-[12px] break-all text-[#18181a]">{draft.path}</code>
                              <span className="line-clamp-2 text-[11px] leading-[1.5] text-[#858b9c]">
                                {draft.description || draft.display_name || '—'}
                              </span>
                              <span className="text-[10px] break-all text-[#c0c6d4]">{draft.url}</span>
                              {draft.deprecated && <StatusBadge tone="red">deprecated</StatusBadge>}
                            </div>
                          </td>
                          <td className="px-[10px] py-[12px]">
                            <Input
                              aria-label={`工具名 ${index + 1}`}
                              className="h-[32px] font-mono text-[12px]"
                              value={draft.name}
                              onChange={(event) => patchDraft(index, { name: event.target.value })}
                            />
                          </td>
                          <td className="px-[10px] py-[12px]">
                            <Input
                              aria-label={`展示名 ${index + 1}`}
                              className="h-[32px] text-[12px]"
                              value={draft.display_name || ''}
                              onChange={(event) => patchDraft(index, { display_name: event.target.value })}
                            />
                          </td>
                          <td className="px-[10px] py-[12px] text-[11px] leading-[1.7] text-[#858b9c]">
                            <div>入参 {schemaFieldCount(draft.input_schema)}</div>
                            <div>出参 {schemaFieldCount(draft.output_schema)}</div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </>
          )}

          {result && (
            <section className={`${CARD_CLASS} overflow-hidden`}>
              <div className="flex flex-wrap items-center justify-between gap-[10px] border-b border-[#eceef1] px-[20px] py-[14px]">
                <span className={CARD_TITLE_CLASS}>4. 导入结果与批量操作</span>
                <div className="flex flex-wrap items-center gap-[6px]">
                  <StatusBadge tone="green">成功 {result.imported}</StatusBadge>
                  <StatusBadge tone="gray">跳过 {result.skipped}</StatusBadge>
                  {result.failed > 0 && <StatusBadge tone="red">失败 {result.failed}</StatusBadge>}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-[8px] border-b border-[#eceef1] bg-[#fafbfc] px-[20px] py-[10px]">
                <label className="mr-[4px] flex items-center gap-[8px] text-[12px] text-[#464c5e]">
                  <Checkbox
                    checked={
                      importedItems.length > 0 && selectedImportedItems.length === importedItems.length
                        ? true
                        : selectedImportedItems.length > 0
                          ? 'indeterminate'
                          : false
                    }
                    onCheckedChange={(checked) =>
                      setSelectedImportedNames(
                        checked === true ? importedItems.map((item) => item.name) : [],
                      )
                    }
                    aria-label="全选已导入工具"
                  />
                  已导入工具
                </label>
                <UIButton
                  variant="outline"
                  disabled={batchLoading || selectedImportedItems.length === 0}
                  onClick={() => void batchSetEnabled(true)}
                  className={RETURN_BUTTON_CLASS}
                >
                  <CheckCircle2 className="size-3.5" />
                  批量启用
                </UIButton>
                <UIButton
                  variant="outline"
                  disabled={batchLoading || selectedImportedItems.length === 0}
                  onClick={() => void batchSetEnabled(false)}
                  className={RETURN_BUTTON_CLASS}
                >
                  <XCircle className="size-3.5" />
                  批量停用
                </UIButton>
                <UIButton
                  variant="outline"
                  disabled={batchLoading || selectedImportedItems.length === 0}
                  onClick={() => void batchDelete()}
                  className={`${RETURN_BUTTON_CLASS} text-[#e5484d] hover:text-[#e5484d]`}
                >
                  <Trash2 className="size-3.5" />
                  批量删除
                </UIButton>
                <span className="ml-auto text-[11px] text-[#858b9c]">
                  已选 {selectedImportedItems.length} 个
                </span>
              </div>
              <div className="max-h-[420px] overflow-auto">
                <table className="w-full min-w-[640px] border-collapse text-left">
                  <thead className="sticky top-0 z-10 bg-[#fafbfc] text-[11px] text-[#858b9c]">
                    <tr>
                      <th className="w-[46px] px-[14px] py-[10px] font-medium">选择</th>
                      <th className="px-[10px] py-[10px] font-medium">工具</th>
                      <th className="w-[100px] px-[10px] py-[10px] font-medium">状态</th>
                      <th className="min-w-[220px] px-[10px] py-[10px] font-medium">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.items.map((item) => {
                      const deleted = item.tool_id ? deletedToolIds.includes(item.tool_id) : false;
                      const enabled = item.tool_id
                        ? toolEnabledOverrides[item.tool_id] ?? importEnabled
                        : importEnabled;
                      return (
                        <tr key={`${item.name}-${item.status}`} className="border-t border-[#f0f2f5]">
                          <td className="px-[14px] py-[11px]">
                            <Checkbox
                              disabled={item.status !== 'imported' || deleted}
                              checked={selectedImportedNames.includes(item.name)}
                              onCheckedChange={(checked) =>
                                setSelectedImportedNames((previous) =>
                                  checked === true
                                    ? [...new Set([...previous, item.name])]
                                    : previous.filter((name) => name !== item.name),
                                )
                              }
                              aria-label={`选择导入结果 ${item.name}`}
                            />
                          </td>
                          <td className="px-[10px] py-[11px]">
                            <div className="text-[12px] font-medium text-[#18181a]">
                              {item.display_name || item.name}
                            </div>
                            <code className="text-[11px] text-[#858b9c]">{item.name}</code>
                          </td>
                          <td className="px-[10px] py-[11px]">
                            {deleted ? (
                              <StatusBadge tone="gray">已删除</StatusBadge>
                            ) : item.status === 'imported' ? (
                              <StatusBadge tone={enabled ? 'green' : 'gray'}>
                                {enabled ? '已启用' : '已停用'}
                              </StatusBadge>
                            ) : (
                              <StatusBadge tone={item.status === 'skipped' ? 'gray' : 'red'}>
                                {item.status === 'skipped' ? '已跳过' : '失败'}
                              </StatusBadge>
                            )}
                          </td>
                          <td className="px-[10px] py-[11px] text-[11px] leading-[1.55] text-[#858b9c]">
                            {item.error || (item.status === 'imported' ? '已写入当前分组' : '—')}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="flex flex-wrap justify-end gap-[8px] border-t border-[#eceef1] px-[20px] py-[12px]">
                <UIButton
                  variant="outline"
                  onClick={() => navigate('/enterprise/tools')}
                  className={RETURN_BUTTON_CLASS}
                >
                  查看工具管理
                </UIButton>
                <UIButton
                  variant="outline"
                  onClick={() => {
                    setSourceText('');
                    setSourceName('');
                    setBaseUrlOverride('');
                    setParsed(null);
                    setDrafts([]);
                    setResult(null);
                  }}
                  className={RETURN_BUTTON_CLASS}
                >
                  继续导入另一批
                </UIButton>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
