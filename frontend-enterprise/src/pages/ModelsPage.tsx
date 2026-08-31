import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Check, FlaskConical, LoaderCircle, Trash2 } from 'lucide-react';

import { api, ApiError, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { StatCard } from '@/components/StatCard';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
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
import type { ModelAuthMode, ModelConfigRead } from '../types';
import { OPEN_MODEL_CREATE_EVENT } from '@/components/QuickStartGuide';
import ModelSetupWizard from './models/ModelSetupWizard';
import ModelEditDialog from './models/ModelEditDialog';
import type { ApiKeyProtocol } from './models/channelPresets';
import { useCodexSubscriptionAccount } from './models/useCodexSubscriptionAccount';

const MODEL_PAGE_SIZE = 8;
const MODEL_TEST_UI_TIMEOUT_MS = 100_000;

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

// 把上游诊断字段（HTTP 状态、上游错误码/消息、原始响应体、Request ID）整理成一段纯文本，
// 只用于「查看详情」这类默认折叠的交互，不进入主提示文案。
export function modelProviderDiagnosticText(
  error: ModelProviderErrorDetail | null | undefined,
): string | null {
  if (!error) return null;
  const parts: string[] = [];
  if (typeof error.upstream_status === 'number') parts.push(`HTTP 状态码：${error.upstream_status}`);
  if (error.provider_code) parts.push(`上游错误码：${error.provider_code}`);
  if (error.provider_message) parts.push(`上游消息：${error.provider_message}`);
  if (error.upstream_body) parts.push(`上游响应：${error.upstream_body}`);
  if (error.request_id) parts.push(`Request ID：${error.request_id}`);
  return parts.length ? parts.join('\n') : null;
}

// 折叠的诊断详情展示：默认只显示友好文案，点击「查看详情」才展开原始诊断文本；
// 诊断文本按纯文本渲染（React children 天然转义），不使用 dangerouslySetInnerHTML。
function ModelErrorToast({ message, diagnostic }: { message: string; diagnostic: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <span className="flex flex-col items-start gap-[6px]">
      <span>{message}</span>
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="text-[12px] underline underline-offset-2 opacity-80 hover:opacity-100"
      >
        {expanded ? '收起详情' : '查看详情'}
      </button>
      {expanded && (
        <pre className="max-h-[160px] w-full max-w-[420px] overflow-auto whitespace-pre-wrap break-all rounded-[8px] bg-black/5 p-[8px] text-[11px] text-[#464c5e]">
          {diagnostic}
        </pre>
      )}
    </span>
  );
}

// 组装模型上游错误的 toast 内容：有可展示的诊断详情就附加折叠区域，否则只返回友好文案。
export function toastContentForProviderError(
  error: ModelProviderErrorDetail | null | undefined,
  fallback: string,
): ReactNode {
  const message = modelProviderErrorMessage(error, fallback);
  const diagnostic = modelProviderDiagnosticText(error);
  return diagnostic ? <ModelErrorToast message={message} diagnostic={diagnostic} /> : message;
}

export function providerErrorFromApiError(error: ApiError): ModelProviderErrorDetail | null {
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

/** 展示当前租户的模型配置，并复用统一向导与订阅账号状态。 */
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
  const [editingModel, setEditingModel] = useState<ModelConfigRead | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<ModelConfigRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [subscriptionLogoutConfirmOpen, setSubscriptionLogoutConfirmOpen] = useState(false);
  const testingModelIdsRef = useRef(new Set<string>());
  const [testingModelIds, setTestingModelIds] = useState<Set<string>>(new Set());
  const [availableProtocols, setAvailableProtocols] = useState<ApiKeyProtocol[]>(['openai_chat_completions']);
  const {
    account: subscriptionAccount,
    loading: subscriptionLoading,
    startLogin: startSubscriptionLogin,
    cancelLogin: cancelSubscriptionLogin,
    logout: logoutSubscription,
  } = useCodexSubscriptionAccount({ tenantId: TENANT_ID });

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

  useEffect(() => {
    void load();
    void api
      .get<{ protocols: ApiKeyProtocol[] }>(`/api/enterprise/model-configs/protocols?tenant_id=${TENANT_ID}`)
      .then((result) => setAvailableProtocols(result.protocols));
  }, []);

  useEffect(() => {
    const openCreate = () => setWizardOpen(true);
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
  /** 打开退出本机 Codex 的影响确认。 */
  function requestSubscriptionLogout() {
    setSubscriptionLogoutConfirmOpen(true);
  }

  /** 确认退出后关闭提示，并交由共享订阅账号逻辑执行。 */
  function confirmSubscriptionLogout() {
    setSubscriptionLogoutConfirmOpen(false);
    void logoutSubscription();
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
          <DropdownMenuItem className={MENU_ITEM_CLASS} disabled={isTesting} onSelect={() => setEditingModel(row)}>
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
          onClick={() => setWizardOpen(true)}
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
          <StatCard
            label="默认模型"
            value={<span title={defaultRow?.name || undefined}>{defaultRow?.name || '-'}</span>}
            valueClassName="min-w-0 flex-1 shrink truncate text-[18px] leading-[26px]"
          />
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

      <ModelSetupWizard
        open={wizardOpen}
        tenantId={TENANT_ID}
        onOpenChange={setWizardOpen}
        onCreated={(model, options) => {
          void load();
          notify.success(
            options?.tested
              ? `模型「${model.name}」已创建并通过测试`
              : `模型「${model.name}」已保存为草稿，点击「测试」后即可启用`,
          );
        }}
        availableProtocols={availableProtocols}
        subscriptionAccount={subscriptionAccount}
        subscriptionLoading={subscriptionLoading}
        onStartSubscriptionLogin={startSubscriptionLogin}
        onCancelSubscriptionLogin={cancelSubscriptionLogin}
        onRequestSubscriptionLogout={requestSubscriptionLogout}
      />

      <ModelEditDialog
        open={editingModel !== null}
        selected={editingModel}
        availableProtocols={availableProtocols}
        subscriptionAccount={subscriptionAccount}
        subscriptionLoading={subscriptionLoading}
        onStartSubscriptionLogin={startSubscriptionLogin}
        onCancelSubscriptionLogin={cancelSubscriptionLogin}
        onRequestSubscriptionLogout={requestSubscriptionLogout}
        onOpenChange={(open) => !open && setEditingModel(null)}
        onSaved={() => void load()}
      />

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
