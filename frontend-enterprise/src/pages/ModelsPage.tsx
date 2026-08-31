import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Check, FlaskConical, LoaderCircle, LogIn, LogOut, Trash2 } from 'lucide-react';

import { api, ApiError, TENANT_ID } from '../api/client';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
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
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { useClientPagination } from '../hooks/useClientPagination';
import type { CodexSubscriptionAccountRead, ModelAuthMode, ModelConfigRead } from '../types';
import { OPEN_MODEL_CREATE_EVENT } from '@/components/QuickStartGuide';

const MODEL_PAGE_SIZE = 8;
const MODEL_TEST_UI_TIMEOUT_MS = 100_000;
type ApiKeyProtocol = Exclude<ModelConfigRead['api_protocol'], 'codex_app_server'>;

type ModelForm = {
  name: string;
  auth_mode: ModelAuthMode;
  api_protocol: ApiKeyProtocol;
  base_url: string;
  model: string;
  api_key: string;
  temperature: string;
  max_output_tokens: string;
  extra_body: string;
  is_default: boolean;
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
  auth_mode: 'api_key',
  api_protocol: 'openai_chat_completions',
  base_url: '',
  model: '',
  api_key: '',
  temperature: '0.2',
  max_output_tokens: '8192',
  extra_body: '{}',
  is_default: false,
  enabled: true,
};

const SUBSCRIPTION_PROVIDER_USER_MESSAGES: Record<string, string> = {
  MODEL_SUBSCRIPTION_ACCESS_DENIED: '当前 ChatGPT 订阅无权使用此模型，请检查订阅权益或模型名称。',
  MODEL_SUBSCRIPTION_AUTH_FAILED: '本机 Codex 登录未完成，请重新连接 ChatGPT 订阅。',
  MODEL_SUBSCRIPTION_AUTH_REQUIRED: '请先在本机 Codex 中登录 ChatGPT 订阅，再测试或启用此模型。',
  MODEL_SUBSCRIPTION_BROWSER_UNAVAILABLE: '无法打开本机 Codex 登录页面，请检查桌面浏览器后重试。',
  MODEL_SUBSCRIPTION_NETWORK_UNAVAILABLE: '暂时无法连接 ChatGPT 订阅服务，请检查网络后重试。',
  MODEL_SUBSCRIPTION_QUOTA_EXCEEDED: 'ChatGPT 订阅额度暂不可用，请稍后重试。',
  MODEL_SUBSCRIPTION_REFRESH_FAILED: 'ChatGPT 授权已失效，请在浏览器中重新连接订阅。',
  MODEL_SUBSCRIPTION_RUNTIME_FAILED: '本机 Codex runtime 未能完成模型请求，请检查登录状态和模型名称后重试。',
  MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR: '本机 Codex runtime 返回了无法识别的结果，请升级 Codex 后重试。',
  MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT: '本机 Codex runtime 请求超时，请稍后重试。',
  MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE: '未找到可用的本机 Codex runtime，请安装并登录 Codex 后重试。',
};
export function modelAuthModeLabel(authMode: ModelAuthMode | string | null | undefined): string {
  return authMode === 'chatgpt_subscription' ? 'ChatGPT 订阅（Codex）' : 'API Key';
}
export function modelProviderErrorMessage(
  error: ModelProviderErrorDetail | null | undefined,
  fallback: string,
): string {
  if (!error) return fallback;
  const subscriptionMessage = SUBSCRIPTION_PROVIDER_USER_MESSAGES[error.code];
  if (subscriptionMessage) return subscriptionMessage;
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
  const [subscriptionAccount, setSubscriptionAccount] = useState<CodexSubscriptionAccountRead | null>(null);
  const [subscriptionLoading, setSubscriptionLoading] = useState(false);
  const [subscriptionLogoutConfirmOpen, setSubscriptionLogoutConfirmOpen] = useState(false);
  const testingModelIdsRef = useRef(new Set<string>());
  const [testingModelIds, setTestingModelIds] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<ModelForm>(BLANK_MODEL_FORM);
  const [availableProtocols, setAvailableProtocols] = useState<ApiKeyProtocol[]>(['openai_chat_completions']);

  const updateForm = <K extends keyof ModelForm>(key: K, value: ModelForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const load = (showLoading = true) => {
    if (showLoading) setLoading(true);
    return api
      .get<ModelConfigRead[]>(`/api/enterprise/model-configs?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setRows(items);
        window.dispatchEvent(new CustomEvent(MODEL_CONFIGS_UPDATED_EVENT, { detail: { models: items } }));
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载模型失败'))
      .finally(() => {
        if (showLoading) setLoading(false);
      });
  };

  const loadSubscriptionAccount = useCallback(async () => {
    try {
      const account = await api.get<CodexSubscriptionAccountRead>(
        `/api/enterprise/model-configs/codex-subscription/account?tenant_id=${TENANT_ID}`,
      );
      setSubscriptionAccount(account);
    } catch (error) {
      notify.error(apiErrorMessage(error, '无法读取 ChatGPT 订阅状态'));
    }
  }, []);

  useEffect(() => {
    void load();
    void api
      .get<{ protocols: ApiKeyProtocol[] }>(`/api/enterprise/model-configs/protocols?tenant_id=${TENANT_ID}`)
      .then((result) => setAvailableProtocols(result.protocols));
    void loadSubscriptionAccount();
  }, [loadSubscriptionAccount]);

  useEffect(() => {
    if (subscriptionAccount?.status !== 'pending') return;
    const intervalId = window.setInterval(() => void loadSubscriptionAccount(), 2_000);
    return () => window.clearInterval(intervalId);
  }, [loadSubscriptionAccount, subscriptionAccount?.status]);

  useEffect(() => {
    const openCreate = () => createBlank();
    window.addEventListener(OPEN_MODEL_CREATE_EVENT, openCreate);
    return () => window.removeEventListener(OPEN_MODEL_CREATE_EVENT, openCreate);
  }, []);

  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) =>
      [row.name, row.model, row.api_protocol, row.base_url || '', modelAuthModeLabel(row.auth_mode)].some((value) =>
        (value || '').toLowerCase().includes(keyword),
      ),
    );
  }, [rows, searchText]);

  const pagination = useClientPagination(filteredRows, MODEL_PAGE_SIZE, searchText);

  const enabledCount = rows.filter((item) => item.enabled).length;
  const defaultRow = rows.find((item) => item.is_default);
  const providerCount = new Set(rows.map((item) => item.api_protocol).filter(Boolean)).size;
  const isSubscriptionForm = form.auth_mode === 'chatgpt_subscription';

  function edit(row: ModelConfigRead) {
    setSelected(row);
    setForm({
      name: row.name,
      auth_mode: row.auth_mode || 'api_key',
      api_protocol: row.auth_mode === 'chatgpt_subscription'
        ? 'openai_chat_completions'
        : row.api_protocol as ApiKeyProtocol,
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
    const model = form.model.trim();
    if (!name || !model) {
      notify.error('请填写名称和 Model');
      return;
    }
    const temperature = Number(form.temperature);
    const maxOutputTokens = Number(form.max_output_tokens);
    if (Number.isNaN(temperature) || Number.isNaN(maxOutputTokens)) {
      notify.error('Temperature 与 Max Tokens 必须是数字');
      return;
    }
    let extraBody: Record<string, unknown> = {};
    if (!isSubscriptionForm) {
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
    }
    const payload = {
      tenant_id: TENANT_ID,
      name,
      auth_mode: form.auth_mode,
      model,
      temperature,
      max_output_tokens: maxOutputTokens,
      is_default: form.enabled && form.is_default,
      enabled: form.enabled,
      ...(isSubscriptionForm ? {} : {
        api_protocol: form.api_protocol,
        base_url: form.base_url.trim() || undefined,
        extra_body: extraBody,
        api_key: form.api_key || undefined,
      }),
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

  async function updateSubscriptionAccount(
    action: 'login' | 'login/cancel' | 'logout',
    fallback: string,
  ) {
    setSubscriptionLoading(true);
    try {
      const account = await api.post<CodexSubscriptionAccountRead>(
        `/api/enterprise/model-configs/codex-subscription/${action}?tenant_id=${TENANT_ID}`,
      );
      setSubscriptionAccount(account);
      notify.success(account.message);
    } catch (error) {
      notify.error(apiErrorMessage(error, fallback));
    } finally {
      setSubscriptionLoading(false);
    }
  }

  function startSubscriptionLogin() {
    void updateSubscriptionAccount('login', '无法启动本机 Codex 登录');
  }

  function cancelSubscriptionLogin() {
    void updateSubscriptionAccount('login/cancel', '无法取消本机 Codex 登录');
  }

  function confirmSubscriptionLogout() {
    setSubscriptionLogoutConfirmOpen(false);
    void updateSubscriptionAccount('logout', '无法退出 ChatGPT 订阅');
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
          </span>
          <span className="truncate text-[#858b9c]">
            {row.enabled ? '已启用' : '已停用'} · {modelAuthModeLabel(row.auth_mode)}
          </span>
        </div>
      ),
    },
    { key: 'model', title: '模型', width: 180, render: (row) => <span className="block truncate">{row.model}</span> },
    {
      key: 'auth_mode',
      title: '认证方式',
      className: 'whitespace-normal',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="line-clamp-1 wrap-break-word text-[#464c5e]">{modelAuthModeLabel(row.auth_mode)}</span>
          <span className="line-clamp-1 wrap-break-word text-[#858b9c]">
            {row.auth_mode === 'chatgpt_subscription' ? '本机 Codex runtime' : row.base_url || '未设置 Base URL'}
          </span>
        </div>
      ),
    },
    {
      key: 'api_key',
      title: 'API Key',
      width: 180,
      render: (row) => <span className="block truncate font-mono text-[#858b9c]">
        {row.auth_mode === 'chatgpt_subscription' ? '无需 API Key' : row.api_key_masked || '-'}
      </span>,
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
            {row.enabled ? '已启用' : '已停用'} · {modelAuthModeLabel(row.auth_mode)}
          </span>
        </div>
        {renderActions(row)}
      </div>
      <p className="mt-[8px] line-clamp-1 wrap-break-word text-[12px] text-[#858b9c]">{row.model}</p>
      <p className="mt-[4px] line-clamp-1 wrap-break-word font-mono text-[12px] text-[#858b9c]">
        {row.auth_mode === 'chatgpt_subscription' ? 'ChatGPT 订阅' : row.api_key_masked || '-'}
      </p>
    </article>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader className="items-center" onLogout={onLogout} userName={currentUser?.username} title="模型" />

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
          data-guide-target="models-create"
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
          <StatCard label="默认模型" value={defaultRow?.name || '-'} valueClassName="text-[18px] leading-[26px]" />
          <StatCard label="API 协议" value={providerCount} />
        </div>

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
              <LabeledField label="认证方式">
                <Select
                  value={form.auth_mode}
                  onValueChange={(value) => updateForm('auth_mode', value as ModelAuthMode)}
                >
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="api_key">API Key</SelectItem>
                    <SelectItem value="chatgpt_subscription">ChatGPT 订阅（Codex）</SelectItem>
                  </SelectContent>
                </Select>
              </LabeledField>
              <LabeledField label="Model">
                <Input value={form.model} placeholder="例如 gpt-4o" onChange={(event) => updateForm('model', event.target.value)} />
              </LabeledField>
              {isSubscriptionForm ? (
                <div className="flex flex-col gap-[10px] rounded-[10px] border border-[#dce7ff] bg-[#f6f9ff] p-[12px] sm:col-span-2">
                  <div className="flex flex-wrap items-start justify-between gap-[10px]">
                    <div className="min-w-0">
                      <p className="text-[12px] font-medium text-[#29466f]">ChatGPT 订阅</p>
                      <p className="mt-[3px] text-[12px] leading-[18px] text-[#5d6f8c]">
                        {subscriptionAccount?.message || '正在读取 ChatGPT 订阅状态…'}
                        {subscriptionAccount?.status === 'connected' && subscriptionAccount.plan_type
                          ? `（${subscriptionAccount.plan_type}）`
                          : ''}
                      </p>
                    </div>
                    {subscriptionAccount?.status === 'connected' ? (
                      <UIButton
                        type="button"
                        variant="outline"
                        disabled={subscriptionLoading}
                        onClick={() => setSubscriptionLogoutConfirmOpen(true)}
                        className="h-[30px] gap-[4px] border-[#cbd8f2] bg-white px-[10px] text-[12px] text-[#464c5e]"
                      >
                        <LogOut className="size-[13px]" />
                        退出本机 Codex
                      </UIButton>
                    ) : subscriptionAccount?.status === 'pending' ? (
                      <UIButton
                        type="button"
                        variant="outline"
                        disabled={subscriptionLoading}
                        onClick={cancelSubscriptionLogin}
                        className="h-[30px] border-[#cbd8f2] bg-white px-[10px] text-[12px] text-[#464c5e]"
                      >
                        取消登录
                      </UIButton>
                    ) : (
                      <UIButton
                        type="button"
                        disabled={subscriptionLoading}
                        onClick={startSubscriptionLogin}
                        className="h-[30px] gap-[4px] bg-[#1a71ff] px-[10px] text-[12px] text-white hover:bg-[#1463df]"
                      >
                        {subscriptionLoading ? <LoaderCircle className="size-[13px] animate-spin" /> : <LogIn className="size-[13px]" />}
                        连接 ChatGPT 订阅
                      </UIButton>
                    )}
                  </div>
                  <p className="text-[11px] leading-[16px] text-[#7483a0]">
                    登录由本机 Codex runtime 管理。StaffDeck 不保存 ChatGPT OAuth code、access token 或 refresh token。
                  </p>
                </div>
              ) : (
                <>
                  <LabeledField label="API 协议">
                    <Select
                      value={form.api_protocol}
                      onValueChange={(value) => updateForm('api_protocol', value as ApiKeyProtocol)}
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
                  <LabeledField label="API Key">
                    <Input
                      type="password"
                      value={form.api_key}
                      placeholder={selected ? '不修改请留空' : 'sk-...'}
                      onChange={(event) => updateForm('api_key', event.target.value)}
                    />
                  </LabeledField>
                </>
              )}
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
              {!isSubscriptionForm && form.api_protocol === 'openai_chat_completions' && <div className="sm:col-span-2">
                <LabeledField label="额外请求参数（extra_body JSON）">
                  <Textarea
                    rows={5}
                    value={form.extra_body}
                    placeholder={'{\n  "thinking": {\n    "type": "disabled"\n  }\n}'}
                    className="min-h-[116px] resize-y font-mono text-[12px]"
                    onChange={(event) => updateForm('extra_body', event.target.value)}
                  />
                </LabeledField>
              </div>}
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
        open={subscriptionLogoutConfirmOpen}
        onOpenChange={setSubscriptionLogoutConfirmOpen}
        loading={subscriptionLoading}
        destructive={false}
        title="退出本机 Codex？"
        description="这会让本机 Codex 退出 ChatGPT。所有采用“ChatGPT 订阅（Codex）”的模型都会失去授权；同一台电脑上使用该 Codex 登录的其他应用也可能受影响。API Key 模型不受影响。"
        confirmText="退出本机 Codex"
        onConfirm={confirmSubscriptionLogout}
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
