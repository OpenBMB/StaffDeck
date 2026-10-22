import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { InfoCircleOutlined } from '@/icons';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { Check, ChevronDown, FlaskConical, Gift, LoaderCircle, Plus, Trash2 } from 'lucide-react';

import { api, ApiError, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { cn } from '@/lib/utils';
import { MENU_CONTENT_CLASS, MENU_ITEM_CLASS, MENU_ITEM_DANGER_CLASS } from '@/lib/enterprise-ui';
import { apiErrorMessage } from '@/lib/apiErrorMessages';
import IconAdd from '../assets/icons/add.svg?react';
import IconClear from '../assets/icons/field-clear.svg?react';
import IconEdit from '../assets/icons/edit.svg?react';
import IconModels from '../assets/icons/sys-models.svg?react';
import IconMore from '../assets/icons/more.svg?react';
import IconRefresh from '../assets/icons/refresh.svg?react';
import IconSearch from '../assets/icons/search.svg?react';
import { ApiKeyApplicationsPanel } from '@/components/ApiKeyApplicationsPanel';
import { JsonTextarea } from '@/components/JsonTextarea';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { useClientPagination } from '../hooks/useClientPagination';
import type { ModelConfigRead } from '../types';
import { OPEN_MODEL_CREATE_EVENT } from '@/components/QuickStartGuide';

const MODEL_PAGE_SIZE = 8;
const MODEL_TEST_UI_TIMEOUT_MS = 100_000;

type ModelForm = {
  name: string;
  api_protocol: 'openai_chat_completions' | 'openai_responses' | 'anthropic_messages' | 'gemini_generate_content';
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_output_tokens: string;
  extra_body: string;
  custom_headers: string;
  is_default: boolean;
  is_intent_recognition: boolean;
  enabled: boolean;
};

export type ModelProviderErrorDetail = {
  code: string;
  message: string;
  upstream_status?: number | null;
  provider_code?: string | null;
  provider_message?: string | null;
  upstream_body?: string | null;
  request_id?: string | null;
  retryable?: boolean;
};

type ModelTestResponse = {
  success: boolean;
  message: string;
  output?: string;
  activated: boolean;
  error?: ModelProviderErrorDetail | null;
};

const BLANK_MODEL_FORM: ModelForm = {
  name: '',
  api_protocol: 'openai_chat_completions',
  base_url: '',
  model: '',
  api_key: '',
  temperature: '0.2',
  max_output_tokens: '131072',
  extra_body: '{}',
  custom_headers: '{}',
  is_default: false,
  is_intent_recognition: false,
  enabled: true,
};

/**
 * 可搜索 + 可手动输入的下拉：预设模型可直接点选，
 * 输入框里输入任意名称后回车（或点击「使用自定义名称」）即可手动录入。
 */
function ModelCombobox({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);

  const trimmed = query.trim();
  const filtered = useMemo(() => {
    const q = trimmed.toLowerCase();
    return q ? options.filter((model) => model.toLowerCase().includes(q)) : options;
  }, [options, trimmed]);

  // 输入内容不在预设列表内 -> 允许作为自定义名称录入
  const canCustom = trimmed !== '' && !options.includes(trimmed);

  function pick(next: string) {
    onChange(next);
    setOpen(false);
    setQuery('');
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setQuery(value);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            'flex h-8 w-full items-center justify-between gap-1.5 rounded-lg border border-input bg-transparent px-2.5 text-sm transition-colors outline-none select-none',
            'hover:bg-accent/40 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
            value ? 'text-foreground' : 'text-muted-foreground',
          )}
        >
          <span className="truncate">
            {value || (options.length ? '请选择模型，或输入自定义名称' : '暂无可用模型，请在环境变量 MODEL_PRESETS 中配置')}
          </span>
          <ChevronDown className={cn('size-4 shrink-0 text-muted-foreground transition-transform', open && 'rotate-180')} />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={4}
        className="w-(--radix-popover-trigger-width) min-w-[240px] p-1.5"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          searchRef.current?.focus();
        }}
      >
        <input
          ref={searchRef}
          value={query}
          placeholder="搜索或输入模型名称，回车确认"
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && canCustom) pick(trimmed);
            if (event.key === 'Escape') setOpen(false);
          }}
          className="mb-1.5 h-7 w-full rounded-md border border-input bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring"
        />
        <div className="max-h-[224px] overflow-y-auto">
          {filtered.map((model) => (
            <button
              key={model}
              type="button"
              onClick={() => pick(model)}
              className={cn(
                'flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-accent hover:text-accent-foreground',
                model === value && 'bg-accent/70 text-accent-foreground',
              )}
            >
              <span className="truncate font-mono text-[13px]">{model}</span>
              {model === value && <Check className="size-3.5 shrink-0" />}
            </button>
          ))}
          {canCustom && (
            <>
              {filtered.length > 0 && <div className="my-1 h-px bg-border" />}
              <button
                type="button"
                onClick={() => pick(trimmed)}
                className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-sm text-primary transition-colors hover:bg-accent"
              >
                <Plus className="size-3.5 shrink-0" />
                <span className="min-w-0 flex-1 truncate">使用自定义名称“{trimmed}”</span>
                <kbd className="rounded border border-border bg-muted px-1 font-mono text-[10px] leading-4 text-muted-foreground">↵</kbd>
              </button>
            </>
          )}
          {!filtered.length && !canCustom && (
            <div className="px-2 py-4 text-center text-xs text-muted-foreground">无匹配选项</div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}

export function modelProviderErrorMessage(
  error: ModelProviderErrorDetail | null | undefined,
  fallback: string,
): string {
  if (!error) return fallback;
  const parts = [error.code || fallback];
  if (typeof error.upstream_status === 'number') parts.push(`HTTP ${error.upstream_status}`);
  if (error.provider_code) parts.push(`上游错误码：${error.provider_code}`);
  if (error.provider_message) parts.push(`上游消息：${error.provider_message}`);
  if (error.upstream_body) parts.push(`上游响应：${error.upstream_body}`);
  if (error.request_id) parts.push(`Request ID：${error.request_id}`);
  return parts.join('；');
}

function providerErrorFromApiError(error: ApiError): ModelProviderErrorDetail | null {
  try {
    const payload = JSON.parse(error.body) as { detail?: unknown };
    if (!payload.detail || typeof payload.detail !== 'object' || Array.isArray(payload.detail)) return null;
    const detail = payload.detail as Partial<ModelProviderErrorDetail>;
    if (typeof detail.code !== 'string' || typeof detail.message !== 'string') return null;
    return detail as ModelProviderErrorDetail;
  } catch {
    return null;
  }
}

export function modelActionError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const providerError = providerErrorFromApiError(error);
    if (providerError) return modelProviderErrorMessage(providerError, fallback);
  }
  return apiErrorMessage(error, fallback);
}
const MODEL_CONFIGS_UPDATED_EVENT = 'ultrarag-enterprise-model-configs-updated';

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
  const [saveStage, setSaveStage] = useState<'saving' | 'testing' | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ModelConfigRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const testingModelIdsRef = useRef(new Set<string>());
  const [testingModelIds, setTestingModelIds] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<ModelForm>(BLANK_MODEL_FORM);
  const [availableProtocols, setAvailableProtocols] = useState<ModelForm['api_protocol'][]>(['openai_chat_completions']);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  // 意图识别模型互斥确认：开启开关时若已存在其他意图识别模型，先提示再放行
  const [intentSwitchTarget, setIntentSwitchTarget] = useState<ModelConfigRead | null>(null);
  const [pendingIntentSwitch, setPendingIntentSwitch] = useState(false);

  const updateForm = <K extends keyof ModelForm>(key: K, value: ModelForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const load = (showLoading = true) => {
    if (showLoading) setLoading(true);
    return api
      // 管理页需要看到全局免费模型（徽标/取消标记）；其余选模型场景一律不出现它
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}&include_global_free=1`)
      .then((items) => {
        setRows(items);
        // 广播给其他消费方（App/对话页等）时剔除全局免费模型，保持「只在免费链路生效」口径
        window.dispatchEvent(new CustomEvent(MODEL_CONFIGS_UPDATED_EVENT, {
          detail: { models: items.filter((item) => !item.is_global_free) },
        }));
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载模型失败'))
      .finally(() => {
        if (showLoading) setLoading(false);
      });
  };

  useEffect(() => {
    void load();
    // 这两个都是「锦上添花」的次级请求：失败时保留组件内已有的默认选项即可，
    // 但不能没有 catch —— 否则会变成 unhandled rejection。两个请求保持一致。
    void api
      .get<{ protocols: ModelForm['api_protocol'][] }>(`/api/enterprise/model-configs/protocols?tenant_id=${TENANT_ID}`)
      .then((result) => setAvailableProtocols(result.protocols))
      .catch(() => {});
    void api
      .get<{ models: string[] }>(`/api/enterprise/model-configs/presets?tenant_id=${TENANT_ID}`)
      .then((result) => setAvailableModels(result.models))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const openCreate = () => createBlank();
    window.addEventListener(OPEN_MODEL_CREATE_EVENT, openCreate);
    return () => window.removeEventListener(OPEN_MODEL_CREATE_EVENT, openCreate);
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) =>
      [row.name, row.model, row.api_protocol, row.base_url || ''].some((value) =>
        (value || '').toLowerCase().includes(keyword),
      ),
    );
  }, [rows, searchText]);

  const pagination = useClientPagination(filteredRows, MODEL_PAGE_SIZE, searchText);

  // 下拉候选：环境变量配置的固定模型；编辑旧数据时若当前值不在列表内，则并入展示以保留原值
  const modelOptions = useMemo(() => {
    const options = [...availableModels];
    const current = form.model.trim();
    if (current && !options.includes(current)) options.unshift(current);
    return options;
  }, [availableModels, form.model]);

  // 已存在的其他意图识别模型（排除当前正在编辑的这条）
  const existingIntentModel = useMemo(
    () => rows.find((row) => row.is_intent_recognition && row.id !== selected?.id) || null,
    [rows, selected],
  );

  // 打开意图识别开关：若已有其他意图识别模型，弹确认；否则直接开启
  function handleIntentSwitchToggle(next: boolean) {
    if (next && existingIntentModel) {
      setPendingIntentSwitch(true);
      setIntentSwitchTarget(existingIntentModel);
      return;
    }
    updateForm('is_intent_recognition', next);
  }

  function confirmIntentSwitch() {
    updateForm('is_intent_recognition', true);
    setPendingIntentSwitch(false);
    setIntentSwitchTarget(null);
  }

  function cancelIntentSwitch() {
    setPendingIntentSwitch(false);
    setIntentSwitchTarget(null);
  }

  function edit(row: ModelConfigRead) {
    setSelected(row);
    setForm({
      name: row.name,
      api_protocol: row.api_protocol,
      base_url: row.base_url || '',
      model: row.model,
      api_key: '',
      temperature: String(row.temperature),
      max_output_tokens: String(row.max_output_tokens),
      extra_body: JSON.stringify(row.extra_body || {}, null, 2),
      custom_headers: JSON.stringify(row.custom_headers || {}, null, 2),
      is_default: row.is_default,
      is_intent_recognition: row.is_intent_recognition,
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
    const model = form.model.trim();
    if (!name || !model) {
      notify.error('请填写名称并选择 Model');
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
    let customHeaders: Record<string, unknown>;
    try {
      const parsed = JSON.parse(form.custom_headers.trim() || '{}') as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('not an object');
      }
      customHeaders = parsed as Record<string, unknown>;
    } catch {
      notify.error('自定义请求头必须是合法的 JSON 对象');
      return;
    }
    const payload = {
      tenant_id: TENANT_ID,
      name,
      api_protocol: form.api_protocol,
      base_url: form.base_url.trim() || undefined,
      model,
      temperature,
      max_output_tokens: maxOutputTokens,
      extra_body: extraBody,
      custom_headers: customHeaders,
      is_default: form.enabled && form.is_default,
      is_intent_recognition: form.is_intent_recognition,
      enabled: form.enabled,
      api_key: form.api_key || undefined,
    };
    setSaving(true);
    setSaveStage(form.enabled ? 'testing' : 'saving');
    try {
      const verifyQuery = form.enabled ? '?verify_before_save=true' : '';
      if (selected) {
        await api.put<ModelConfigRead>(
          `/api/enterprise/model-configs/${selected.id}${verifyQuery}`,
          payload,
        );
      } else {
        await api.post<ModelConfigRead>(`/api/enterprise/model-configs${verifyQuery}`, payload);
      }
      if (form.enabled) {
        notify.success(form.is_default ? '测试通过，已启用并设为默认模型' : '测试通过，已启用');
      } else {
        notify.success('已保存');
      }
      // 保存成功且本条开启意图识别：若此前存在其他意图识别模型，说明已被替换关闭
      const replacedIntentModel = existingIntentModel && form.is_intent_recognition
        ? existingIntentModel
        : null;
      if (replacedIntentModel) {
        notify.success(`已将「${replacedIntentModel.name}」的意图识别模型切换至当前模型`);
      }
      setEditorOpen(false);
      setSelected(null);
      setForm(BLANK_MODEL_FORM);
      await load();
    } catch (error) {
      notify.error(modelActionError(error, '保存失败'));
    } finally {
      setSaving(false);
      setSaveStage(null);
    }
  }

  async function confirmDelete() {
    const row = deleteTarget;
    if (!row || deleting) return;
    setDeleting(true);
    try {
      await api.delete(`/api/enterprise/model-configs/${row.id}?tenant_id=${TENANT_ID}`);
      notify.success('已删除模型');
      setDeleteTarget(null);
      await load();
    } catch (error) {
      notify.error(modelActionError(error, '删除失败'));
    } finally {
      setDeleting(false);
    }
  }

  async function setDefault(row: ModelConfigRead) {
    try {
      await api.post(`/api/enterprise/model-configs/${row.id}/set-default?tenant_id=${TENANT_ID}`);
      notify.success('已设为默认');
      await load();
    } catch (error) {
      notify.error(modelActionError(error, '设为默认失败'));
    }
  }

  /** 管理员把模型标记为全局免费模型（租户内单选，费用记在自己头上）。 */
  async function setGlobalFree(row: ModelConfigRead) {
    const next = !row.is_global_free;
    try {
      await api.post(
        `/api/enterprise/model-configs/${row.id}/global-free?tenant_id=${TENANT_ID}`,
        { enabled: next },
      );
      notify.success(next
        ? `已把「${row.name}」设为全局免费模型（租户内单选）`
        : `已取消「${row.name}」的全局免费模型`);
      await load();
    } catch (error) {
      notify.error(modelActionError(error, '全局免费模型设置失败'));
    }
  }

  async function test(row: ModelConfigRead): Promise<boolean> {
    if (testingModelIdsRef.current.has(row.id)) return false;
    testingModelIdsRef.current.add(row.id);
    setTestingModelIds(new Set(testingModelIdsRef.current));
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), MODEL_TEST_UI_TIMEOUT_MS);
    try {
      const result = await api.postWithSignal<ModelTestResponse>(
        `/api/enterprise/model-configs/${row.id}/test?tenant_id=${TENANT_ID}&activate_if_initial=true`,
        {},
        controller.signal,
      );
      if (result.success) {
        if (!result.activated) notify.success(result.output || result.message);
        return true;
      } else if (result.message === 'MODEL_VERIFICATION_STALE') {
        notify.warning('模型配置或测试状态已发生变化，本次结果未生效，请刷新后重新测试');
      } else {
        notify.error(modelProviderErrorMessage(result.error, result.message));
      }
      return false;
    } catch (error) {
      notify.error(
        error instanceof DOMException && error.name === 'AbortError'
          ? '模型连接测试超时，请检查本地模型服务地址和网络后重试'
          : error instanceof Error ? error.message : '测试失败',
      );
      return false;
    } finally {
      window.clearTimeout(timeoutId);
      testingModelIdsRef.current.delete(row.id);
      setTestingModelIds(new Set(testingModelIdsRef.current));
      void load(false);
    }
  }

  function renderActions(row: ModelConfigRead) {
    const isTesting = testingModelIds.has(row.id);
    const isAdmin = currentUser?.role === 'admin';
    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          aria-label={isTesting ? `${row.name} 正在测试` : '模型操作'}
          className="ml-auto grid size-7 place-items-center rounded-[8px] text-[#1a71ff] transition-colors outline-none hover:bg-black/5 hover:text-[#4a8dff] focus-visible:bg-black/5"
        >
          {isTesting ? <LoaderCircle className="size-3.5 animate-spin" /> : <IconMore className="size-3.5" />}
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className={MENU_CONTENT_CLASS}>
          <DropdownMenuItem className={MENU_ITEM_CLASS} disabled={isTesting} onSelect={() => edit(row)}>
            <IconEdit />
            编辑
          </DropdownMenuItem>
          <DropdownMenuItem
            className={MENU_ITEM_CLASS}
            disabled={isTesting || row.is_default}
            onSelect={() => void setDefault(row)}
          >
            <Check />
            {row.is_default ? '已默认' : '设为默认'}
          </DropdownMenuItem>
          {isAdmin && (
            <DropdownMenuItem
              className={MENU_ITEM_CLASS}
              disabled={isTesting || (!row.is_global_free && !row.enabled)}
              onSelect={() => void setGlobalFree(row)}
            >
              <Gift />
              {row.is_global_free ? '取消全局免费' : '设为全局免费模型'}
            </DropdownMenuItem>
          )}
          <DropdownMenuItem className={MENU_ITEM_CLASS} disabled={isTesting} onSelect={() => void test(row)}>
            {isTesting ? <LoaderCircle className="animate-spin" /> : <FlaskConical />}
            {isTesting ? '正在测试' : '测试'}
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            className={MENU_ITEM_DANGER_CLASS}
            disabled={isTesting}
            onSelect={() => setDeleteTarget(row)}
          >
            <Trash2 />
            删除
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
            {row.is_intent_recognition && <StatusBadge tone="blue">意图识别</StatusBadge>}
            {row.is_global_free && <StatusBadge tone="green">全局免费</StatusBadge>}
          </span>
          <span className="truncate text-[#858b9c]">
            {row.enabled ? '已启用' : '已停用'} · {row.api_protocol}
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
      render: (row) =>
        row.api_key_masked ? (
          <span className="block truncate font-mono text-[#858b9c]">{row.api_key_masked}</span>
        ) : (
          <span className="block truncate text-[12px] font-medium text-[#e5484d]">密钥失效，请更新</span>
        ),
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
            {row.is_intent_recognition && <StatusBadge tone="blue">意图识别</StatusBadge>}
            {row.is_global_free && <StatusBadge tone="green">全局免费</StatusBadge>}
          </span>
          <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">
            {row.enabled ? '已启用' : '已停用'} · {row.api_protocol}
          </span>
        </div>
        {renderActions(row)}
      </div>
      <p className="mt-[8px] line-clamp-1 wrap-break-word text-[12px] text-[#858b9c]">{row.model}</p>
      <p className="mt-[4px] line-clamp-1 wrap-break-word font-mono text-[12px] text-[#858b9c]">
        {row.api_key_masked || (
          <span className="font-medium text-[#e5484d]">密钥失效，请更新</span>
        )}
      </p>
    </article>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader className="items-center" onLogout={onLogout} userName={currentUser?.username} title="模型配置" />

      <div className="mt-[16px] mb-[16px] flex items-center justify-end gap-[12px]">
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
          data-guide-target="models-create"
          onClick={createBlank}
          className="h-[34px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          <IconAdd className="size-[14px]" />
          新建模型
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <IconModels className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">模型列表</span>
          </div>

          <label className="flex h-[34px] w-[300px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a] max-[900px]:w-full">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchText}
              placeholder="搜索名称、模型、API 协议或 Base URL"
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

      <div className="mt-[16px]">
        <ApiKeyApplicationsPanel currentUser={currentUser} />
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
              <LabeledField label="API 协议">
                <Select
                  value={form.api_protocol}
                  onValueChange={(value) => updateForm('api_protocol', value as ModelForm['api_protocol'])}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {availableProtocols.includes('openai_chat_completions') && (
                      <SelectItem value="openai_chat_completions">OpenAI Chat Completions</SelectItem>
                    )}
                    {availableProtocols.includes('openai_responses') && (
                      <SelectItem value="openai_responses">OpenAI Responses API</SelectItem>
                    )}
                    {availableProtocols.includes('anthropic_messages') && (
                      <SelectItem value="anthropic_messages">Anthropic Messages</SelectItem>
                    )}
                    {availableProtocols.includes('gemini_generate_content') && (
                      <SelectItem value="gemini_generate_content">Gemini Generate Content</SelectItem>
                    )}
                  </SelectContent>
                </Select>
              </LabeledField>
              <LabeledField label="Base URL">
                <Input
                  value={form.base_url}
                  placeholder={form.api_protocol === 'openai_chat_completions' || form.api_protocol === 'openai_responses'
                    ? 'https://llm-center.modelbest.cn/llm/v1'
                    : 'https://llm-center.modelbest.cn/llm'}
                  onChange={(event) => updateForm('base_url', event.target.value)}
                />
              </LabeledField>
              <LabeledField label="Model">
                <ModelCombobox
                  value={form.model}
                  options={modelOptions}
                  onChange={(value) => updateForm('model', value)}
                />
              </LabeledField>
              <LabeledField label="API Key">
                <Input
                  type="password"
                  value={form.api_key}
                  placeholder={
                    selected && !selected.api_key_masked
                      ? '密钥已失效，必须重新填写'
                      : selected
                        ? '不修改请留空'
                        : 'sk-...'
                  }
                  onChange={(event) => updateForm('api_key', event.target.value)}
                />
              </LabeledField>
              <div className="grid grid-cols-2 gap-[14px]">
                <LabeledField label="Temperature">
                  <Input
                    type="number"
                    min={0}
                    max={form.api_protocol === 'anthropic_messages' ? 1 : 2}
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
                <JsonTextarea
                  label="额外请求参数（extra_body JSON）"
                  value={form.extra_body}
                  placeholder={'{\n  "thinking": {\n    "type": "disabled"\n  }\n}'}
                  rows={5}
                  minHeight={116}
                  onChange={(value) => updateForm('extra_body', value)}
                />
              </div>
              <div className="sm:col-span-2">
                <JsonTextarea
                  label="自定义请求头（JSON）"
                  value={form.custom_headers}
                  hint="配置后会附加到该模型的所有出站请求头中；Authorization、X-API-Key 等认证类头不可覆盖。"
                  placeholder={'{\n  "X-Request-Source": "staffdeck",\n  "X-Trace-Id": "abc123"\n}'}
                  rows={3}
                  minHeight={80}
                  onChange={(value) => updateForm('custom_headers', value)}
                />
              </div>
            </div>
            <div className="mt-[16px] flex flex-wrap items-center gap-[24px]">
              <label className="flex cursor-pointer items-center gap-[8px]">
                <Switch checked={form.is_default} onCheckedChange={(next) => updateForm('is_default', next)} />
                <span className="text-[12px] font-medium text-[#464c5e]">设为默认</span>
              </label>
              <label className="flex cursor-pointer items-center gap-[8px]">
                <Switch
                  checked={form.is_intent_recognition}
                  onCheckedChange={handleIntentSwitchToggle}
                />
                <span className="text-[12px] font-medium text-[#464c5e]">用于意图识别</span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      aria-label="查看意图识别模型说明"
                      onClick={(event) => event.preventDefault()}
                      className="inline-flex size-[16px] shrink-0 items-center justify-center rounded-full text-[#8b93a7] outline-none transition-colors hover:text-[#18181a] focus-visible:ring-2 focus-visible:ring-[#1a71ff]/40"
                    >
                      <InfoCircleOutlined className="size-[13px]" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="top" align="start" className="max-w-[300px] leading-[1.55]">
                    建议使用 flash 系列的轻量模型，可大大加快意图识别速度
                  </TooltipContent>
                </Tooltip>
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
              {saving && <LoaderCircle className="size-[14px] animate-spin" />}
              {saveStage === 'testing' ? '测试并保存中' : saving ? '保存中' : '保存'}
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        loading={deleting}
        title={deleteTarget ? `删除模型「${deleteTarget.name}」？` : ''}
        description={deleteTarget?.is_default
          ? '这是当前默认模型。删除后需要重新设置默认模型，相关数字员工中的模型绑定也会一并移除。'
          : '删除后，相关数字员工中的模型绑定也会一并移除，操作不可撤销。'}
        confirmText="删除"
        onConfirm={() => void confirmDelete()}
      />

      <ConfirmDialog
        open={pendingIntentSwitch && Boolean(intentSwitchTarget)}
        onOpenChange={(open) => { if (!open) cancelIntentSwitch(); }}
        title={`已存在意图识别模型「${intentSwitchTarget?.name ?? ''}」`}
        description="每个租户只能有一个用于意图识别的模型。继续开启后，保存成功时将自动关闭原模型的意图识别开关。"
        confirmText="仍要开启"
        onConfirm={confirmIntentSwitch}
      />

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
