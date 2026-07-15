import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Check, FlaskConical } from 'lucide-react';

import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Input,
  Switch,
  Textarea,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS } from '@/lib/enterprise-ui';
import IconAdd from '../assets/icons/add.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconModels from '../assets/icons/sys-models.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { useClientPagination } from '../hooks/useClientPagination';
import type { ModelConfigRead } from '../types';

const MODEL_PAGE_SIZE = 8;

type ModelForm = {
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_output_tokens: string;
  extra_body: string;
  is_default: boolean;
  enabled: boolean;
};

const BLANK_MODEL_FORM: ModelForm = {
  name: '',
  provider: 'openai_compatible',
  base_url: '',
  model: '',
  api_key: '',
  temperature: '0.2',
  max_output_tokens: '8192',
  extra_body: '{}',
  is_default: false,
  enabled: true,
};

export default function ModelsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [rows, setRows] = useState<ModelConfigRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [selected, setSelected] = useState<ModelConfigRead | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<ModelForm>(BLANK_MODEL_FORM);

  const updateForm = <K extends keyof ModelForm>(key: K, value: ModelForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const load = () => {
    setLoading(true);
    return api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then(setRows)
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载模型失败'))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    void load();
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) =>
      [row.name, row.model, row.provider, row.base_url || ''].some((value) =>
        (value || '').toLowerCase().includes(keyword),
      ),
    );
  }, [rows, searchText]);

  const pagination = useClientPagination(filteredRows, MODEL_PAGE_SIZE, searchText);

  const enabledCount = rows.filter((item) => item.enabled).length;
  const defaultRow = rows.find((item) => item.is_default);
  const providerCount = new Set(rows.map((item) => item.provider).filter(Boolean)).size;

  function edit(row: ModelConfigRead) {
    setSelected(row);
    setForm({
      name: row.name,
      provider: row.provider,
      base_url: row.base_url || '',
      model: row.model,
      api_key: '',
      temperature: String(row.temperature),
      max_output_tokens: String(row.max_output_tokens),
      extra_body: JSON.stringify(row.extra_body || {}, null, 2),
      is_default: row.is_default,
      enabled: row.enabled,
    });
    setEditorOpen(true);
  }

  function createBlank() {
    setSelected(null);
    setForm(BLANK_MODEL_FORM);
    setEditorOpen(true);
  }

  function closeEditor() {
    if (saving) return;
    setEditorOpen(false);
    setSelected(null);
  }

  async function save() {
    const name = form.name.trim();
    const provider = form.provider.trim();
    const model = form.model.trim();
    if (!name || !provider || !model) {
      notify.error('请填写名称、Provider 和 Model');
      return;
    }
    const temperature = Number(form.temperature);
    const maxOutputTokens = Number(form.max_output_tokens);
    if (Number.isNaN(temperature) || Number.isNaN(maxOutputTokens)) {
      notify.error('Temperature 与 Max Tokens 必须是数字');
      return;
    }
    let extraBody: Record<string, unknown>;
    try {
      const parsed = JSON.parse(form.extra_body.trim() || '{}') as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('not an object');
      }
      extraBody = parsed as Record<string, unknown>;
    } catch {
      notify.error('额外参数必须是合法的 JSON 对象');
      return;
    }
    const payload = {
      tenant_id: TENANT_ID,
      name,
      provider,
      base_url: form.base_url.trim() || undefined,
      model,
      temperature,
      max_output_tokens: maxOutputTokens,
      extra_body: extraBody,
      is_default: form.is_default,
      enabled: form.enabled,
      api_key: form.api_key || undefined,
    };
    setSaving(true);
    try {
      if (selected) {
        await api.put(`/api/enterprise/model-configs/${selected.id}`, payload);
      } else {
        await api.post('/api/enterprise/model-configs', payload);
      }
      notify.success('已保存');
      setEditorOpen(false);
      setSelected(null);
      setForm(BLANK_MODEL_FORM);
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  async function setDefault(row: ModelConfigRead) {
    try {
      await api.post(`/api/enterprise/model-configs/${row.id}/set-default?tenant_id=${TENANT_ID}`);
      notify.success('已设为默认');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '设为默认失败');
    }
  }

  async function test(row: ModelConfigRead) {
    try {
      const result = await api.post<{ success: boolean; message: string; output?: string }>(
        `/api/enterprise/model-configs/${row.id}/test?tenant_id=${TENANT_ID}`,
      );
      if (result.success) {
        notify.success(result.output || result.message);
      } else {
        notify.error(result.message);
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '测试失败');
    }
  }

  function renderActions(row: ModelConfigRead) {
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label="模型操作"
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          <IconMore className="size-3.5" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => edit(row)}>
            <IconEdit />
            编辑
          </DropdownMenuItem>
          <DropdownMenuItem className={MENU_ITEM_CLASS} disabled={row.is_default} onSelect={() => void setDefault(row)}>
            <Check />
            {row.is_default ? '已默认' : '设为默认'}
          </DropdownMenuItem>
          <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => void test(row)}>
            <FlaskConical />
            测试
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  }

  const columns: DataTableColumn<ModelConfigRead>[] = [
    {
      key: 'name',
      title: '名称',
      width: 240,
      className: 'text-[#18181a]',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="flex min-w-0 items-center gap-[6px]">
            <span className="truncate font-medium leading-[18px] text-[#18181a]">{row.name}</span>
            {row.is_default && <StatusBadge tone="green">默认</StatusBadge>}
          </span>
          <span className="truncate text-[#858b9c]">
            {row.enabled ? '已启用' : '已停用'} · {row.provider}
          </span>
        </div>
      ),
    },
    { key: 'model', title: '模型', width: 180, render: (row) => <span className="block truncate">{row.model}</span> },
    {
      key: 'base_url',
      title: 'Base URL',
      className: 'whitespace-normal',
      render: (row) => <span className="line-clamp-1 wrap-break-word text-[#858b9c]">{row.base_url || '-'}</span>,
    },
    {
      key: 'api_key',
      title: 'API Key',
      width: 180,
      render: (row) => <span className="block truncate font-mono text-[#858b9c]">{row.api_key_masked || '-'}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 70,
      align: 'right',
      render: (row) => renderActions(row),
    },
  ];

  const renderMobileCard = (row: ModelConfigRead) => (
    <article
      className="min-w-0 rounded-[8px] border border-[#eceef1] bg-white p-[14px]"
      key={row.id}
    >
      <div className="flex min-w-0 items-start justify-between gap-[10px]">
        <div className="min-w-0">
          <span className="flex min-w-0 items-center gap-[6px]">
            <strong className="truncate text-[14px] font-semibold text-[#18181a]">{row.name}</strong>
            {row.is_default && <StatusBadge tone="green">默认</StatusBadge>}
          </span>
          <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">
            {row.enabled ? '已启用' : '已停用'} · {row.provider}
          </span>
        </div>
        {renderActions(row)}
      </div>
      <p className="mt-[8px] line-clamp-1 wrap-break-word text-[12px] text-[#858b9c]">{row.model}</p>
      <p className="mt-[4px] line-clamp-1 wrap-break-word font-mono text-[12px] text-[#858b9c]">
        {row.api_key_masked || '-'}
      </p>
    </article>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="模型" />

      <div className="mt-[20px] mb-[16px] flex items-center justify-end gap-[12px]">
        <UIButton
          variant="outline"
          onClick={() => void load()}
          disabled={loading}
          className="h-[34px] gap-[4px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[20px] text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]"
        >
          <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </UIButton>
        <UIButton
          onClick={createBlank}
          className="h-[34px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          <IconAdd className="size-[14px]" />
          新建模型
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label="模型统计">
          <StatCard label="模型" value={rows.length} />
          <StatCard label="已启用" value={enabledCount} tone="green" />
          <StatCard label="默认模型" value={defaultRow?.name || '-'} valueClassName="text-[18px]" />
          <StatCard label="Provider" value={providerCount} />
        </div>

        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconModels className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">模型列表</span>
          </div>

          <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              value={searchText}
              placeholder="搜索名称、模型、Provider 或 Base URL"
              onChange={(event) => setSearchText(event.target.value)}
              className="h-full min-w-0 flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
            />
            {searchText && (
              <button
                type="button"
                aria-label="清除搜索"
                onClick={() => setSearchText('')}
                className="grid size-[16px] shrink-0 place-items-center text-[#c0c6d4] hover:text-[#858b9c]"
              >
                <IconClear className="size-[14px]" />
              </button>
            )}
          </label>

          <div className="grid gap-[10px] md:hidden">
            {filteredRows.length ? (
              pagination.pagedItems.map(renderMobileCard)
            ) : (
              <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无模型</div>
            )}
          </div>

          <div className="hidden md:block">
            <DataTable
              aria-label="模型列表"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无模型，点击「新建模型」添加一个吧"
            />
          </div>

          {filteredRows.length > 0 && (
            <Paginator
              aria-label="模型分页"
              className="mt-0 mb-[6px]"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </div>
      </div>

      <Dialog open={editorOpen} onOpenChange={(next) => !next && closeEditor()}>
        <DialogContent
          aria-describedby={undefined}
          className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[640px]"
        >
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconModels className="size-[14px] shrink-0" />
            <DialogTitle className="min-w-0 truncate text-[14px] font-normal leading-none text-[#757f9c]">
              {selected ? `编辑模型：${selected.name}` : '新建模型'}
            </DialogTitle>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-[12px]">
            <div className="grid grid-cols-1 gap-[14px] sm:grid-cols-2">
              <LabeledField label="名称">
                <Input value={form.name} placeholder="例如 GPT-4o" onChange={(event) => updateForm('name', event.target.value)} />
              </LabeledField>
              <LabeledField label="Provider">
                <Input value={form.provider} placeholder="例如 openai_compatible" onChange={(event) => updateForm('provider', event.target.value)} />
              </LabeledField>
              <LabeledField label="Base URL">
                <Input value={form.base_url} placeholder="https://api.openai.com/v1" onChange={(event) => updateForm('base_url', event.target.value)} />
              </LabeledField>
              <LabeledField label="Model">
                <Input value={form.model} placeholder="例如 gpt-4o" onChange={(event) => updateForm('model', event.target.value)} />
              </LabeledField>
              <LabeledField label="API Key">
                <Input
                  type="password"
                  value={form.api_key}
                  placeholder={selected ? '不修改请留空' : 'sk-...'}
                  onChange={(event) => updateForm('api_key', event.target.value)}
                />
              </LabeledField>
              <div className="grid grid-cols-2 gap-[14px]">
                <LabeledField label="Temperature">
                  <Input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={form.temperature}
                    onChange={(event) => updateForm('temperature', event.target.value)}
                  />
                </LabeledField>
                <LabeledField label="Max Tokens">
                  <Input
                    type="number"
                    min={128}
                    max={32000}
                    value={form.max_output_tokens}
                    onChange={(event) => updateForm('max_output_tokens', event.target.value)}
                  />
                </LabeledField>
              </div>
              <div className="sm:col-span-2">
                <LabeledField label="额外请求参数（extra_body JSON）">
                  <Textarea
                    rows={5}
                    value={form.extra_body}
                    placeholder={'{\n  "thinking": {\n    "type": "disabled"\n  }\n}'}
                    className="min-h-[116px] resize-y font-mono text-[12px]"
                    onChange={(event) => updateForm('extra_body', event.target.value)}
                  />
                </LabeledField>
              </div>
            </div>
            <div className="mt-[16px] flex flex-wrap items-center gap-[24px]">
              <label className="flex cursor-pointer items-center gap-[8px]">
                <Switch checked={form.is_default} onCheckedChange={(next) => updateForm('is_default', next)} />
                <span className="text-[12px] font-medium text-[#464c5e]">设为默认</span>
              </label>
              <label className="flex cursor-pointer items-center gap-[8px]">
                <Switch checked={form.enabled} onCheckedChange={(next) => updateForm('enabled', next)} />
                <span className="text-[12px] font-medium text-[#464c5e]">启用</span>
              </label>
            </div>
          </div>

          <div className="flex items-center justify-end gap-[8px] px-[12px]">
            <UIButton
              variant="outline"
              disabled={saving}
              onClick={closeEditor}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={saving}
              onClick={() => void save()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}
