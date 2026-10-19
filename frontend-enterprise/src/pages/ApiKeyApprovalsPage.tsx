import { useCallback, useEffect, useState } from 'react';
import {
  Check,
  ChevronLeft,
  ChevronRight,
  KeyRound,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  SlidersHorizontal,
  Users,
  X,
} from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import {
  Dialog,
  DialogContent,
  DialogTitle,
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
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { UnderlineTabs } from '@/components/ui/underline-tabs';
import { api, ApiError, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import AccountsPanel from './accounts/AccountsPanel';
import { formatDateTime } from '@/lib/enterprise-ui';
import { toClientDateISO } from '@/lib/timezone';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type GatewayOption = {
  name: string;
  gateway_id: string;
  gateway_url: string;
};

type ApiKeyApproval = {
  id: string;
  tenant_id: string;
  user_id: string;
  username: string | null;
  purpose: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'revoked';
  api_key_masked: string | null;
  api_url: string | null;
  gateway_name: string | null;
  gateway_id: string | null;
  quota_limit: number | null;
  quota_period: string | null;
  quota_rule_id: string | null;
  quota_rule_name: string | null;
  consumer_id: string | null;
  consumer_name: string | null;
  consumer_status: 'enabled' | 'disabled' | 'deleted' | null;
  consumer_group_name: string | null;
  used_amount: number | null;
  usage_month: string | null;
  reviewer_note: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};

type ConsumerGroup = {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  owner: string | null;
  gateway_id: string | null;
  gateway_name: string | null;
  external_consumer_group_id: string | null;
  consumer_type: string;
  consumer_count: number | null;
  status: string;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

type Consumer = {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  gateway_id: string | null;
  gateway_name: string | null;
  external_consumer_id: string | null;
  consumer_group_name: string | null;
  consumer_type: string;
  deploy_status: string | null;
  enable: boolean;
  status: string;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

type QuotaRule = {
  id: string;
  tenant_id: string;
  name: string;
  gateway_id: string | null;
  gateway_name: string | null;
  quota_dimension: string;
  quota_limit: number;
  period_type: string;
  external_rule_id: string | null;
  subject_type?: string | null; // consumer（按消费者）/ consumer_group（按消费组整组）
  status: string;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

type ApprovalStats = {
  pending: number;
  allocated: number;
  history: number;
};

type UsageSummary = {
  allocated_users: number;
  total_quota: number;
  total_used: number;
  total_current_used: number;
  avg_usage_rate: number;
  high_watermark_users: number;
  low_watermark_users: number;
};

type UsageItem = {
  id: string;
  user_id: string;
  username: string | null;
  user_no: string | null;
  department: string | null;
  consumer_id: string | null;
  consumer_name: string | null;
  gateway_name: string | null;
  gateway_id: string | null;
  quota_limit: number | null;
  quota_period: string | null;
  quota_rule_id: string | null;
  used_amount: number;
  current_used_amount: number;
  archived_used_amount: number;
  usage_rate: number;
  suggestion: string;
};

type UsageRead = {
  month: string;
  summary: UsageSummary;
  items: UsageItem[];
};

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const STATUS_META: Record<ApiKeyApproval['status'], { tone: 'orange' | 'green' | 'red' | 'gray'; label: string }> = {
  pending: { tone: 'orange', label: '待审批' },
  approved: { tone: 'green', label: '已批准' },
  rejected: { tone: 'red', label: '已驳回' },
  revoked: { tone: 'gray', label: '已吊销' },
};

const PERIOD_LABEL: Record<string, string> = {
  day: '自然日',
  week: '自然周',
  month: '自然月',
};

const SUGGESTION_META: Record<string, { label: string; tone: 'red' | 'orange' | 'green' | 'gray' }> = {
  expand: { label: '建议扩容', tone: 'red' },
  watch: { label: '关注', tone: 'orange' },
  normal: { label: '正常', tone: 'green' },
  unknown: { label: '未知', tone: 'gray' },
};

type TabValue = 'pending' | 'issued' | 'history' | 'quota' | 'groups' | 'users';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// 曾经自己 new Date(value)：后端时间戳是 naive UTC（无 Z），直接解析会当成本地时间，
// 在东八区显示上整体偏 8 小时；locale 也被写死成 zh-CN。统一走 lib 里的规范实现。
function formatTime(value: string | null): string {
  return formatDateTime(value ?? undefined);
}

type AliyunSyncCounts = { created: number; updated: number; removed: number };

type AliyunSyncResult = {
  synced_at: string;
  groups: AliyunSyncCounts;
  consumers: AliyunSyncCounts;
  quota_rules: AliyunSyncCounts;
};

function StatCard({
  label,
  value,
  tone,
  active,
  onClick,
}: {
  label: string;
  value: number;
  tone: 'orange' | 'green' | 'gray';
  active: boolean;
  onClick: () => void;
}) {
  const toneClass = {
    orange: 'text-[#f59e0b]',
    green: 'text-[#22c55e]',
    gray: 'text-[#858b9c]',
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-[160px] flex-col items-center justify-center gap-[4px] rounded-[12px] border-[0.5px] px-[16px] py-[12px] text-center transition-colors',
        active
          ? 'border-[#18181a] bg-[#f6f6f6]'
          : 'border-[#e3e7f1] bg-white hover:border-[#cbd3e6] hover:bg-[#f8f9fb]',
      )}
    >
      <span className="text-[12px] text-[#858b9c]">{label}</span>
      <span className={cn('text-[24px] font-semibold leading-[28px]', toneClass)}>{value}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ApiKeyApprovalsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [tab, setTab] = useState<TabValue>('pending');
  const [rows, setRows] = useState<ApiKeyApproval[]>([]);
  const [stats, setStats] = useState<ApprovalStats>({ pending: 0, allocated: 0, history: 0 });
  const [loading, setLoading] = useState(false);
  const [actingId, setActingId] = useState<string | null>(null);

  // Consumer groups
  const [groups, setGroups] = useState<ConsumerGroup[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(false);

  // Consumers
  const [consumers, setConsumers] = useState<Consumer[]>([]);
  const [consumersLoading, setConsumersLoading] = useState(false);

  // Aliyun manual sync (consumer groups tab)
  const [syncing, setSyncing] = useState(false);
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null);

  // Quota rules
  const [rules, setRules] = useState<QuotaRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);

  // Gateways
  const [gateways, setGateways] = useState<GatewayOption[]>([]);

  // Usage
  const [usage, setUsage] = useState<UsageRead | null>(null);
  // 当前自然月按客户端时区取：`toISOString().slice(0,7)` 是 UTC 月份，东八区月初凌晨会算成上个月
  const [usageMonth, setUsageMonth] = useState(() => toClientDateISO(new Date()).slice(0, 7));
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageSearch, setUsageSearch] = useState('');
  const [consumerSearch, setConsumerSearch] = useState('');

  // Approve dialog
  const [approveTarget, setApproveTarget] = useState<ApiKeyApproval | null>(null);
  const [approveConsumerName, setApproveConsumerName] = useState('');
  const [approveGroupId, setApproveGroupId] = useState('');
  const [approveRuleId, setApproveRuleId] = useState('');
  const [approveApiUrl, setApproveApiUrl] = useState('');
  const [approving, setApproving] = useState(false);

  // Reject dialog
  const [rejectTarget, setRejectTarget] = useState<ApiKeyApproval | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [rejecting, setRejecting] = useState(false);

  // Revoke confirm dialog
  const [revokeTarget, setRevokeTarget] = useState<ApiKeyApproval | null>(null);
  const [revokePreview, setRevokePreview] = useState<{ api_key_count: number; will_disable_consumer: boolean } | null>(null);
  const [revokePreviewLoading, setRevokePreviewLoading] = useState(false);
  const [revoking, setRevoking] = useState(false);

  // Quota rule create dialog
  const [qrOpen, setQrOpen] = useState(false);
  const [qrName, setQrName] = useState('');
  const [qrGateway, setQrGateway] = useState('');
  const [qrDimension, setQrDimension] = useState<'token' | 'credit'>('token');
  const [qrLimit, setQrLimit] = useState('');
  const [qrPeriod, setQrPeriod] = useState<'day' | 'week' | 'month'>('month');
  const [qrSubjectType, setQrSubjectType] = useState<'consumer' | 'consumer_group'>('consumer');
  const [qrGroupIds, setQrGroupIds] = useState<string[]>([]);
  const [qrConsumerIds, setQrConsumerIds] = useState<string[]>([]);
  const [qrConsumerSearch, setQrConsumerSearch] = useState(''); // 搜索选人（消费者太多不全量展示）
  const [qrSubmitting, setQrSubmitting] = useState(false);

  // Quota rule edit dialog
  const [editRule, setEditRule] = useState<QuotaRule | null>(null);
  const [editRuleName, setEditRuleName] = useState('');
  const [editRuleLimit, setEditRuleLimit] = useState('');
  const [editRulePeriod, setEditRulePeriod] = useState<'day' | 'week' | 'month'>('month');
  const [editRuleGroups, setEditRuleGroups] = useState<string[]>([]); // 当前绑定的组（云端回显）
  const [editRuleOrigGroups, setEditRuleOrigGroups] = useState<string[]>([]); // 打开时快照，用于算增删
  const [editRuleGroupsLoading, setEditRuleGroupsLoading] = useState(false);
  const [editRuleSubmitting, setEditRuleSubmitting] = useState(false);

  // Adjust quota dialog (for usage tab)
  const [adjustTarget, setAdjustTarget] = useState<UsageItem | null>(null);
  const [adjustLimit, setAdjustLimit] = useState('');
  const [adjustSubmitting, setAdjustSubmitting] = useState(false);

  // Consumer quota change dialog
  const [consumerQuotaTarget, setConsumerQuotaTarget] = useState<Consumer | null>(null);
  const [consumerQuotaRuleId, setConsumerQuotaRuleId] = useState('');
  const [consumerQuotaSubmitting, setConsumerQuotaSubmitting] = useState(false);
  const [togglingConsumerId, setTogglingConsumerId] = useState<string | null>(null);

  // Consumer group owner edit dialog（归属=业务字段，仅本地保存，不调阿里云）
  const [editOwnerTarget, setEditOwnerTarget] = useState<ConsumerGroup | null>(null);
  const [editOwnerValue, setEditOwnerValue] = useState('');
  const [editOwnerSubmitting, setEditOwnerSubmitting] = useState(false);

  // ------------------------------------------------------------------------
  // Data loading
  // ------------------------------------------------------------------------

  const loadApplications = useCallback(() => {
    setLoading(true);
    return api
      .get<ApiKeyApproval[]>(`/api/enterprise/api-key-applications?tenant_id=${TENANT_ID}`)
      .then((items) => setRows(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载申请失败'))
      .finally(() => setLoading(false));
  }, []);

  const loadStats = useCallback(() => {
    api
      .get<ApprovalStats>(`/api/enterprise/api-key-applications/stats?tenant_id=${TENANT_ID}`)
      .then((s) => setStats(s))
      .catch(() => {});
  }, []);

  const loadGateways = useCallback(() => {
    api
      .get<GatewayOption[]>('/api/enterprise/api-key-applications/gateways')
      .then((items) => setGateways(items))
      .catch(() => setGateways([]));
  }, []);

  const loadGroups = useCallback(() => {
    setGroupsLoading(true);
    return api
      .get<ConsumerGroup[]>(`/api/enterprise/api-key-applications/consumer-groups?tenant_id=${TENANT_ID}`)
      .then((items) => setGroups(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载消费组失败'))
      .finally(() => setGroupsLoading(false));
  }, []);

  const loadConsumers = useCallback(() => {
    setConsumersLoading(true);
    return api
      .get<Consumer[]>(`/api/enterprise/api-key-applications/consumers?tenant_id=${TENANT_ID}`)
      .then((items) => setConsumers(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载消费者失败'))
      .finally(() => setConsumersLoading(false));
  }, []);

  const loadRules = useCallback(() => {
    setRulesLoading(true);
    return api
      .get<QuotaRule[]>(`/api/enterprise/api-key-applications/quota-rules?tenant_id=${TENANT_ID}`)
      .then((items) => setRules(items))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载配额规则失败'))
      .finally(() => setRulesLoading(false));
  }, []);

  const syncFromAliyun = useCallback(() => {
    setSyncing(true);
    return api
      .post<AliyunSyncResult>('/api/enterprise/api-key-applications/consumer-groups/sync', {
        tenant_id: TENANT_ID,
      })
      .then((res) => {
        setLastSyncedAt(res.synced_at);
        const fmt = (c: AliyunSyncCounts) => `新增 ${c.created} · 更新 ${c.updated} · 移除 ${c.removed}`;
        notify.success(
          `已从阿里云同步（云端为准）：消费组 ${fmt(res.groups)}；消费者 ${fmt(res.consumers)}；配额规则 ${fmt(res.quota_rules)}`,
        );
        return Promise.all([loadGroups(), loadConsumers(), loadRules()]);
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '同步阿里云数据失败'))
      .finally(() => setSyncing(false));
  }, [loadGroups, loadConsumers, loadRules]);

  const loadUsage = useCallback((month?: string) => {
    setUsageLoading(true);
    const m = month ?? usageMonth;
    return api
      .get<UsageRead>(`/api/enterprise/api-key-applications/usage?tenant_id=${TENANT_ID}&month=${m}`)
      .then((data) => setUsage(data))
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载用量失败'))
      .finally(() => setUsageLoading(false));
  }, [usageMonth]);

  useEffect(() => {
    void loadApplications();
    void loadStats();
    void loadGateways();
    void loadGroups();
    void loadConsumers();
    void loadRules();
  }, [loadApplications, loadStats, loadGateways, loadGroups, loadConsumers, loadRules]);

  useEffect(() => {
    if (tab === 'groups') {
      void loadGroups();
      void loadConsumers();
    }
  }, [tab, loadGroups, loadConsumers]);

  useEffect(() => {
    if (tab === 'quota') {
      void loadRules();
      void loadUsage();
      void loadConsumers();
    }
  }, [tab, loadRules, loadUsage, loadConsumers]);

  // ------------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------------

  function openApprove(item: ApiKeyApproval) {
    setApproveTarget(item);
    setApproveConsumerName(`ailab_${item.username || 'user'}`);
    setApproveGroupId('');
    setApproveRuleId('');
    setApproveApiUrl('https://ai-gateway.folidaymall.com/v1/chat/completions');
  }

  async function confirmApprove() {
    if (!approveTarget) return;
    if (!approveConsumerName.trim()) {
      notify.error('请填写消费者名称');
      return;
    }
    if (!approveGroupId) {
      notify.error('请选择消费组');
      return;
    }
    if (!approveRuleId) {
      notify.error('请选择配额规则');
      return;
    }
    setApproving(true);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${approveTarget.id}/approve`, {
        tenant_id: TENANT_ID,
        consumer_name: approveConsumerName.trim(),
        consumer_group_id: approveGroupId,
        quota_rule_id: approveRuleId,
        api_url: approveApiUrl.trim() || undefined,
      });
      notify.success('已通过：已在阿里云创建消费者（系统生成 API Key）并绑定消费组与配额规则');
      setApproveTarget(null);
      await Promise.all([loadApplications(), loadStats(), loadConsumers(), loadGroups()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setApproving(false);
    }
  }

  async function openRevokeConfirm(item: ApiKeyApproval) {
    setRevokeTarget(item);
    setRevokePreview(null);
    setRevokePreviewLoading(true);
    try {
      const preview = await api.get<{ api_key_count: number; will_disable_consumer: boolean }>(
        `/api/enterprise/api-key-applications/${item.id}/revoke-preview?tenant_id=${TENANT_ID}`,
      );
      setRevokePreview(preview);
    } catch {
      // 预检失败不阻塞，仍允许吊销（按通用文案确认）
      setRevokePreview(null);
    } finally {
      setRevokePreviewLoading(false);
    }
  }

  async function revoke(item: ApiKeyApproval) {
    setRevoking(true);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${item.id}/revoke`, {
        tenant_id: TENANT_ID,
      });
      notify.success('已吊销该 API Key');
      setRevokeTarget(null);
      await Promise.all([loadApplications(), loadStats(), loadConsumers()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setRevoking(false);
    }
  }

  async function confirmReject() {
    if (!rejectTarget) return;
    setRejecting(true);
    try {
      await api.post<ApiKeyApproval>(`/api/enterprise/api-key-applications/${rejectTarget.id}/reject`, {
        tenant_id: TENANT_ID,
        reviewer_note: rejectNote.trim() || undefined,
      });
      notify.success('已驳回');
      setRejectTarget(null);
      setRejectNote('');
      await Promise.all([loadApplications(), loadStats()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setRejecting(false);
    }
  }

  // -- Quota rule actions --

  async function submitQuotaRule() {
    if (!qrName.trim()) {
      notify.error('请填写规则名称');
      return;
    }
    if (!qrGateway) {
      notify.error('请选择网关');
      return;
    }
    const limit = Number(qrLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setQrSubmitting(true);
    try {
      await api.post<QuotaRule>('/api/enterprise/api-key-applications/quota-rules', {
        tenant_id: TENANT_ID,
        name: qrName.trim(),
        gateway_name: qrGateway,
        quota_dimension: qrDimension,
        quota_limit: limit,
        period_type: qrPeriod,
        subject_type: qrSubjectType,
        consumer_ids: qrSubjectType === 'consumer' ? qrConsumerIds : [],
        consumer_group_ids: qrSubjectType === 'consumer_group' ? qrGroupIds : [],
      });
      notify.success('配额规则创建成功');
      setQrOpen(false);
      setQrName('');
      setQrGateway('');
      setQrDimension('token');
      setQrLimit('');
      setQrPeriod('month');
      setQrSubjectType('consumer');
      setQrGroupIds([]);
      setQrConsumerIds([]);
      setQrConsumerSearch('');
      await loadRules();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '创建失败');
    } finally {
      setQrSubmitting(false);
    }
  }

  async function submitEditRule() {
    if (!editRule) return;
    const limit = Number(editRuleLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setEditRuleSubmitting(true);
    try {
      // 对比打开弹窗时的快照，计算需要增删的组
      const addGroupIds = editRuleGroups.filter((id) => !editRuleOrigGroups.includes(id));
      const removeGroupIds = editRuleOrigGroups.filter((id) => !editRuleGroups.includes(id));
      await api.put<QuotaRule>(`/api/enterprise/api-key-applications/quota-rules/${editRule.id}`, {
        tenant_id: TENANT_ID,
        name: editRuleName.trim() || undefined,
        quota_limit: limit,
        // 周期创建后云端不可改（UpdateGatewayQuotaRule 无此字段），编辑不携带
        add_group_ids: addGroupIds,
        remove_group_ids: removeGroupIds,
      });
      notify.success('配额规则已更新');
      setEditRule(null);
      await loadRules();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '更新失败');
    } finally {
      setEditRuleSubmitting(false);
    }
  }

  // 编辑弹窗打开时回显当前绑定的消费组（云端 subjects，一律拉取——
  // 消费者粒度规则也可能在云端混绑组主体，不能只看本地 subject_type）
  async function openEditRule(row: QuotaRule) {
    setEditRule(row);
    setEditRuleName(row.name);
    setEditRuleLimit(String(row.quota_limit));
    setEditRulePeriod(row.period_type as 'day' | 'week' | 'month');
    setEditRuleGroups([]);
    setEditRuleOrigGroups([]);
    setEditRuleGroupsLoading(true);
    try {
      const data = await api.get<{ groups: string[]; consumer_count: number }>(
        `/api/enterprise/api-key-applications/quota-rules/${row.id}/subjects?tenant_id=${TENANT_ID}`,
      );
      setEditRuleGroups(data.groups);
      setEditRuleOrigGroups(data.groups);
    } catch {
      // 回显失败不阻塞编辑，提交时按空快照处理
    } finally {
      setEditRuleGroupsLoading(false);
    }
  }

  // -- Consumer actions --

  async function submitGroupOwner() {
    if (!editOwnerTarget) return;
    setEditOwnerSubmitting(true);
    try {
      const updated = await api.put<ConsumerGroup>(
        `/api/enterprise/api-key-applications/consumer-groups/${editOwnerTarget.external_consumer_group_id || editOwnerTarget.id}/owner`,
        {
          tenant_id: TENANT_ID,
          owner: editOwnerValue.trim() || null,
        },
      );
      // 局部更新，避免整表刷新触发阿里云隐式同步
      setGroups((prev) => prev.map((g) => (g.id === updated.id ? updated : g)));
      notify.success('归属已更新');
      setEditOwnerTarget(null);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '更新失败');
    } finally {
      setEditOwnerSubmitting(false);
    }
  }

  async function toggleConsumer(consumer: Consumer, enable: boolean) {
    setTogglingConsumerId(consumer.id);
    try {
      await api.post<Consumer>(`/api/enterprise/api-key-applications/consumers/${consumer.external_consumer_id}/toggle`, {
        tenant_id: TENANT_ID,
        enable,
      });
      notify.success(enable ? `已启用消费者 ${consumer.name}` : `已停用消费者 ${consumer.name}`);
      await loadConsumers();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setTogglingConsumerId(null);
    }
  }

  async function confirmConsumerQuotaChange() {
    if (!consumerQuotaTarget || !consumerQuotaRuleId) return;
    setConsumerQuotaSubmitting(true);
    try {
      await api.post<Consumer>(
        `/api/enterprise/api-key-applications/consumers/${consumerQuotaTarget.external_consumer_id}/quota`,
        {
          tenant_id: TENANT_ID,
          quota_rule_id: consumerQuotaRuleId,
        },
      );
      notify.success(`已更新 ${consumerQuotaTarget.name} 的配额规则`);
      setConsumerQuotaTarget(null);
      setConsumerQuotaRuleId('');
      await loadConsumers();
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '操作失败');
    } finally {
      setConsumerQuotaSubmitting(false);
    }
  }

  // -- Adjust quota (usage tab) --

  async function submitAdjustQuota() {
    if (!adjustTarget) return;
    const limit = Number(adjustLimit);
    if (!Number.isInteger(limit) || limit <= 0) {
      notify.error('请填写大于 0 的整数配额');
      return;
    }
    setAdjustSubmitting(true);
    try {
      await api.post<ApiKeyApproval>(
        `/api/enterprise/api-key-applications/${adjustTarget.id}/quota`,
        {
          tenant_id: TENANT_ID,
          quota_limit: limit,
        },
      );
      notify.success('配额已调整');
      setAdjustTarget(null);
      setAdjustLimit('');
      await Promise.all([loadApplications(), loadUsage()]);
    } catch (error) {
      notify.error(error instanceof ApiError ? error.message : '调整失败');
    } finally {
      setAdjustSubmitting(false);
    }
  }

  // ------------------------------------------------------------------------
  // Derived data
  // ------------------------------------------------------------------------

  const pendingRows = rows.filter((r) => r.status === 'pending');
  const issuedRows = rows.filter((r) => r.status === 'approved');
  const historyRows = rows.filter((r) => r.status !== 'pending');

  // Rules that match the selected consumer group's gateway (for approve dialog)
  const approveCompatibleRules = approveGroupId
    ? rules.filter((r) => {
        const g = groups.find((x) => x.id === approveGroupId || x.external_consumer_group_id === approveGroupId);
        return g && r.gateway_id === g.gateway_id;
      })
    : rules;

  // Filtered usage items
  const filteredUsageItems = usage?.items.filter((item) => {
    if (!usageSearch) return true;
    const q = usageSearch.toLowerCase();
    return (
      (item.username?.toLowerCase().includes(q) ?? false) ||
      (item.user_no?.toLowerCase().includes(q) ?? false) ||
      (item.consumer_name?.toLowerCase().includes(q) ?? false) ||
      (item.gateway_name?.toLowerCase().includes(q) ?? false)
    );
  }) ?? [];

  const getConsumerForUsage = (item: UsageItem): Consumer | undefined =>
    consumers.find((c) => c.external_consumer_id && c.external_consumer_id === item.consumer_id);

  // Filtered consumers (by name / description / group / gateway / consumer id)
  const filteredConsumers = consumers.filter((c) => {
    if (!consumerSearch.trim()) return true;
    const q = consumerSearch.trim().toLowerCase();
    return (
      (c.name?.toLowerCase().includes(q) ?? false) ||
      (c.description?.toLowerCase().includes(q) ?? false) ||
      (c.consumer_group_name?.toLowerCase().includes(q) ?? false) ||
      (c.gateway_name?.toLowerCase().includes(q) ?? false) ||
      (c.external_consumer_id?.toLowerCase().includes(q) ?? false)
    );
  });

  // ------------------------------------------------------------------------
  // Column definitions
  // ------------------------------------------------------------------------

  const pendingColumns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 140,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
          <span className="truncate text-[12px] text-[#858b9c]">{row.user_id}</span>
        </div>
      ),
    },
    {
      key: 'purpose',
      title: '用途',
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
      ),
    },
    {
      key: 'created_at',
      title: '申请时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.created_at)}</span>,
    },
    {
      key: 'actions',
      title: '操作',
      width: 180,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              disabled={busy}
              onClick={() => openApprove(row)}
              className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : <Check className="size-[13px]" />}
              通过
            </UIButton>
            <UIButton
              disabled={busy}
              onClick={() => {
                setRejectTarget(row);
                setRejectNote('');
              }}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b] hover:bg-[#fdeeee] disabled:opacity-60"
            >
              <X className="size-[13px]" />
              驳回
            </UIButton>
          </div>
        );
      },
    },
  ];

  const issuedColumns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 130,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
          <span className="truncate text-[12px] text-[#858b9c]">{row.user_id}</span>
        </div>
      ),
    },
    {
      key: 'purpose',
      title: '用途',
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 110,
      render: (row) =>
        row.gateway_name ? (
          <span className="block truncate text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'quota',
      title: '配额',
      width: 120,
      render: (row) =>
        row.quota_limit ? (
          <span className="text-[12px] text-[#464c5e]">
            {row.quota_limit.toLocaleString('zh-CN')} / {PERIOD_LABEL[row.quota_period ?? ''] ?? row.quota_period ?? '-'}
          </span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'consumer_group',
      title: '消费组',
      width: 120,
      render: (row) =>
        row.consumer_group_name ? (
          <span className="block truncate text-[12px] text-[#464c5e]">{row.consumer_group_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'api_url',
      title: '网关地址',
      className: 'whitespace-normal',
      render: (row) =>
        row.api_url ? (
          <code className="block truncate font-mono text-[12px] text-[#464c5e]" title={row.api_url}>
            {row.api_url}
          </code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'api_key',
      title: 'API Key',
      width: 120,
      render: (row) =>
        row.api_key_masked ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.api_key_masked}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'consumer_health',
      title: '状态',
      width: 110,
      render: (row) => {
        if (row.status === 'approved' && row.consumer_status === 'deleted') {
          return (
            <span className="inline-flex items-center gap-[4px] rounded-full bg-[#fdeeee] px-[8px] py-[2px] text-[11px] font-medium text-[#c0392b]">
              <ShieldAlert className="size-[12px]" />
              已删除
            </span>
          );
        }
        return <span className="text-[12px] text-[#c0c6d4]">—</span>;
      },
    },
    {
      key: 'actions',
      title: '操作',
      width: 90,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <UIButton
            disabled={busy}
            onClick={() => void openRevokeConfirm(row)}
            className="h-[30px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] hover:bg-black/5 hover:text-[#18181a] disabled:opacity-60"
          >
            {busy ? <LoaderCircle className="size-[13px] animate-spin" /> : null}
            吊销
          </UIButton>
        );
      },
    },
  ];

  const historyColumns: DataTableColumn<ApiKeyApproval>[] = [
    {
      key: 'applicant',
      title: '申请人',
      width: 130,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
          <span className="truncate text-[12px] text-[#858b9c]">{row.user_id}</span>
        </div>
      ),
    },
    {
      key: 'purpose',
      title: '用途',
      render: (row) => (
        <span className="block truncate text-[13px] text-[#464c5e]">{row.purpose || '（未填写）'}</span>
      ),
    },
    {
      key: 'status',
      title: '状态',
      width: 90,
      render: (row) => {
        const meta = STATUS_META[row.status];
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>;
      },
    },
    {
      key: 'reviewer_note',
      title: '原因',
      render: (row) =>
        row.reviewer_note ? (
          <span className="block truncate text-[12px] text-[#858b9c]">{row.reviewer_note}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'reviewed_at',
      title: '审核时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.reviewed_at)}</span>,
    },
    {
      key: 'created_at',
      title: '申请时间',
      width: 160,
      render: (row) => <span className="text-[12px] text-[#858b9c]">{formatTime(row.created_at)}</span>,
    },
  ];

  const groupColumns: DataTableColumn<ConsumerGroup>[] = [
    {
      key: 'name',
      title: '消费组名称',
      width: 180,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.name}</span>
          {row.description && (
            <span className="truncate text-[12px] text-[#858b9c]">{row.description}</span>
          )}
        </div>
      ),
    },
    {
      key: 'group_id',
      title: '消费组 ID',
      width: 200,
      render: (row) =>
        row.external_consumer_group_id ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.external_consumer_group_id}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'owner',
      title: '归属',
      width: 170,
      render: (row) => (
        <div className="flex min-w-0 items-center justify-between gap-[6px]">
          {row.owner ? (
            <span className="truncate text-[12px] text-[#464c5e]" title={row.owner}>{row.owner}</span>
          ) : (
            <span className="truncate text-[12px] text-[#c0c6d4]">—</span>
          )}
          <button
            type="button"
            onClick={() => {
              setEditOwnerTarget(row);
              setEditOwnerValue(row.owner ?? '');
            }}
            className="shrink-0 rounded-[6px] p-[4px] text-[#a3aaba] transition-colors hover:bg-[#f3f4f6] hover:text-[#464c5e]"
            title="修改归属"
          >
            <Pencil className="size-[13px]" />
          </button>
        </div>
      ),
    },
  ];

  const consumerColumns: DataTableColumn<Consumer>[] = [
    {
      key: 'name',
      title: '消费者名称',
      width: 170,
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate text-[13px] font-medium text-[#18181a]">{row.name}</span>
          {row.description && <span className="truncate text-[12px] text-[#858b9c]">{row.description}</span>}
        </div>
      ),
    },
    {
      key: 'consumer_group',
      title: '消费组',
      width: 120,
      render: (row) =>
        row.consumer_group_name ? (
          <span className="block truncate text-[12px] text-[#464c5e]">{row.consumer_group_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">未分组</span>
        ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 110,
      render: (row) =>
        row.gateway_name ? (
          <span className="text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'consumer_id',
      title: '消费者 ID',
      render: (row) =>
        row.external_consumer_id ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.external_consumer_id}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'enable',
      title: '启用',
      width: 80,
      render: (row) => (
        <Switch
          size="sm"
          checked={row.enable !== false}
          disabled={togglingConsumerId === row.id}
          onCheckedChange={(checked) => void toggleConsumer(row, checked)}
        />
      ),
    },
    {
      key: 'quota_actions',
      title: '配额',
      width: 100,
      render: (row) => (
        <UIButton
          disabled={togglingConsumerId === row.id}
          onClick={() => {
            setConsumerQuotaTarget(row);
            setConsumerQuotaRuleId('');
          }}
          className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
        >
          <SlidersHorizontal className="size-[13px]" />
          改配额
        </UIButton>
      ),
    },
  ];

  const ruleColumns: DataTableColumn<QuotaRule>[] = [
    {
      key: 'name',
      title: '规则名称',
      width: 180,
      render: (row) => (
        <span className="block truncate text-[13px] font-medium text-[#18181a]">{row.name}</span>
      ),
    },
    {
      key: 'gateway',
      title: '网关',
      width: 120,
      render: (row) =>
        row.gateway_name ? (
          <span className="text-[13px] text-[#464c5e]">{row.gateway_name}</span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'dimension',
      title: '维度',
      width: 80,
      render: (row) => <span className="text-[12px] text-[#464c5e]">{row.quota_dimension}</span>,
    },
    {
      key: 'subject_type',
      title: '主体',
      width: 88,
      render: (row) =>
        row.subject_type === 'consumer_group' ? (
          <span className="inline-flex h-[20px] items-center rounded-[6px] bg-[#eef2ff] px-[8px] text-[11px] font-medium text-[#4f46e5]">
            消费组
          </span>
        ) : (
          <span className="inline-flex h-[20px] items-center rounded-[6px] bg-[#f3f4f6] px-[8px] text-[11px] font-medium text-[#6b7280]">
            消费者
          </span>
        ),
    },
    {
      key: 'limit',
      title: '额度',
      width: 100,
      render: (row) => (
        <span className="text-[13px] font-medium text-[#18181a]">
          {row.quota_limit.toLocaleString('zh-CN')}
        </span>
      ),
    },
    {
      key: 'period',
      title: '周期',
      width: 80,
      render: (row) => (
        <span className="text-[12px] text-[#464c5e]">{PERIOD_LABEL[row.period_type] ?? row.period_type}</span>
      ),
    },
    {
      key: 'rule_id',
      title: '规则 ID',
      render: (row) =>
        row.external_rule_id ? (
          <code className="block truncate font-mono text-[12px] text-[#858b9c]">{row.external_rule_id}</code>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (row) => {
        const busy = actingId === row.id;
        return (
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              disabled={busy}
              onClick={() => void openEditRule(row)}
              className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
            >
              <SlidersHorizontal className="size-[13px]" />
              编辑
            </UIButton>
          </div>
        );
      },
    },
  ];

  const usageColumns: DataTableColumn<UsageItem>[] = [
    {
      key: 'user',
      title: '消费者',
      width: 170,
      render: (row) => {
        const c = getConsumerForUsage(row);
        return (
          <div className="flex min-w-0 flex-col gap-[2px]">
            <span className="truncate text-[13px] font-medium text-[#18181a]">{c?.name || row.consumer_name || row.username || '-'}</span>
            <span className="truncate text-[12px] text-[#858b9c]">{c?.consumer_group_name || row.consumer_id || ''}</span>
          </div>
        );
      },
    },
    {
      key: 'quota',
      title: '配额',
      width: 110,
      render: (row) =>
        row.quota_limit ? (
          <span className="whitespace-nowrap text-[12px] text-[#464c5e]">
            {row.quota_limit.toLocaleString('zh-CN')} / {PERIOD_LABEL[row.quota_period ?? ''] ?? '-'}
          </span>
        ) : (
          <span className="text-[12px] text-[#c0c6d4]">—</span>
        ),
    },
    {
      key: 'used',
      title: '已用',
      width: 150,
      render: (row) => {
        const archived = row.archived_used_amount ?? 0;
        return (
          <div className="flex flex-col gap-[2px]">
            <span className="text-[13px] font-medium text-[#18181a]">
              {row.used_amount.toLocaleString('zh-CN')}
            </span>
            {archived > 0 && (
              <span className="whitespace-nowrap text-[11px] text-[#858b9c] tabular-nums">
                当前窗口 {(row.current_used_amount ?? 0).toLocaleString('zh-CN')} · 重置前 {archived.toLocaleString('zh-CN')}
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: 'rate',
      title: '使用率',
      width: 180,
      render: (row) => {
        const pct = Math.round(row.usage_rate * 100);
        const barColor =
          pct >= 90
            ? 'bg-[#ef4444]'
            : pct >= 70
              ? 'bg-[#f59e0b]'
              : 'bg-[#22c55e]';
        return (
          <div className="flex items-center gap-[8px]">
            <div className="h-[6px] min-w-[80px] flex-1 overflow-hidden rounded-full bg-[#eef0f4]">
              <div className={cn('h-full rounded-full transition-all', barColor)} style={{ width: `${Math.min(pct, 100)}%` }} />
            </div>
            <span className="text-[12px] tabular-nums text-[#464c5e]">{pct}%</span>
          </div>
        );
      },
    },
    {
      key: 'suggestion',
      title: '水位建议',
      width: 100,
      render: (row) => {
        const meta = SUGGESTION_META[row.suggestion] ?? SUGGESTION_META.unknown;
        return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>;
      },
    },
    {
      key: 'enable',
      title: '启用',
      width: 80,
      render: (row) => {
        const c = getConsumerForUsage(row);
        if (!c) return <span className="text-[12px] text-[#c0c6d4]">—</span>;
        return (
          <Switch
            size="sm"
            checked={c.enable !== false}
            disabled={togglingConsumerId === c.id}
            onCheckedChange={(checked) => void toggleConsumer(c, checked)}
          />
        );
      },
    },
    {
      key: 'actions',
      title: '操作',
      width: 100,
      align: 'right',
      render: (row) => {
        const c = getConsumerForUsage(row);
        if (!c) return <span className="text-[12px] text-[#c0c6d4]">—</span>;
        return (
          <UIButton
            disabled={togglingConsumerId === c.id}
            onClick={() => {
              setConsumerQuotaTarget(c);
              setConsumerQuotaRuleId('');
            }}
            className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
          >
            <SlidersHorizontal className="size-[13px]" />
            改配额
          </UIButton>
        );
      },
    },
  ];

  // ------------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------------

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="items-center"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="API Key 审批"
        description="管理 API Key 申请审批、消费组、配额规则、用量分析与用户账号"
      />

      {/* Stats cards */}
      <div className="mt-[20px] flex justify-end gap-[12px]">
        <StatCard
          label="待审核"
          value={stats.pending}
          tone="orange"
          active={tab === 'pending'}
          onClick={() => setTab('pending')}
        />
        <StatCard
          label="已分配密钥"
          value={stats.allocated}
          tone="green"
          active={tab === 'issued'}
          onClick={() => setTab('issued')}
        />
        <StatCard
          label="审核历史"
          value={stats.history}
          tone="gray"
          active={tab === 'history'}
          onClick={() => setTab('history')}
        />
      </div>

      {/* Tabs */}
      <UnderlineTabs
        className="mt-[20px]"
        variant="line"
        aria-label="密钥审核管理"
        value={tab}
        onChange={setTab}
        items={[
          { value: 'pending', label: '待审核' },
          { value: 'issued', label: '已分配密钥' },
          { value: 'history', label: '审核历史' },
          { value: 'quota', label: '配额管理' },
          { value: 'groups', label: '消费组管理' },
          { value: 'users', label: '用户管理' },
        ]}
      />

      {/* Tab content */}
      <div className="mt-[16px] flex flex-col gap-[24px] rounded-[20px_20px_0_0] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        {/* Pending */}
        {tab === 'pending' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">待审核申请</span>
            </div>
            <div className="hidden md:block">
              <DataTable
                aria-label="待审核申请"
                columns={pendingColumns}
                data={pendingRows}
                rowKey={(row) => row.id}
                loading={loading}
                emptyText="暂无待审核申请"
              />
            </div>
            {/* Mobile fallback */}
            <div className="grid gap-[10px] md:hidden">
              {loading ? (
                <div className="py-[40px] text-center text-[13px] text-[#858b9c]">加载中</div>
              ) : pendingRows.length === 0 ? (
                <div className="py-[40px] text-center text-[13px] text-[#858b9c]">暂无待审核申请</div>
              ) : (
                pendingRows.map((row) => (
                  <article key={row.id} className="min-w-0 rounded-[12px] border border-[#eceef1] bg-white p-[14px]">
                    <div className="flex min-w-0 items-start justify-between gap-[10px]">
                      <div className="min-w-0">
                        <span className="block truncate text-[13px] font-medium text-[#18181a]">{row.username || '-'}</span>
                        <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{row.purpose || '（未填写用途）'}</span>
                      </div>
                      <StatusBadge tone="orange">待审批</StatusBadge>
                    </div>
                    <span className="mt-[8px] block text-[12px] text-[#858b9c]">申请于 {formatTime(row.created_at)}</span>
                    <div className="mt-[12px] flex items-center justify-end gap-[8px]">
                      <UIButton
                        onClick={() => openApprove(row)}
                        className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white"
                      >
                        <Check className="size-[13px]" />
                        通过
                      </UIButton>
                      <UIButton
                        onClick={() => {
                          setRejectTarget(row);
                          setRejectNote('');
                        }}
                        className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#c0392b]"
                      >
                        <X className="size-[13px]" />
                        驳回
                      </UIButton>
                    </div>
                  </article>
                ))
              )}
            </div>
          </>
        )}

        {/* Issued */}
        {tab === 'issued' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">已分配密钥</span>
            </div>
            <DataTable
              aria-label="已分配密钥"
              columns={issuedColumns}
              data={issuedRows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无已分配密钥"
            />
          </>
        )}

        {/* History */}
        {tab === 'history' && (
          <>
            <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
              <KeyRound className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">审核历史</span>
            </div>
            <DataTable
              aria-label="审核历史"
              columns={historyColumns}
              data={historyRows}
              rowKey={(row) => row.id}
              loading={loading}
              emptyText="暂无审核历史"
            />
          </>
        )}

        {/* Quota management */}
        {tab === 'quota' && (
          <>
            {/* Usage summary cards */}
            {usage && (
              <div className="grid grid-cols-2 gap-[10px] px-[12px] md:grid-cols-3 lg:grid-cols-6">
                {[
                  { label: '已分配用户', value: usage.summary.allocated_users },
                  { label: '配额总额（含重置窗口）', value: usage.summary.total_quota.toLocaleString('zh-CN') },
                  { label: '已用总额（本月累计）', value: usage.summary.total_used.toLocaleString('zh-CN') },
                  {
                    label: '平均使用率',
                    value: `${Math.round(usage.summary.avg_usage_rate * 100)}%`,
                  },
                  { label: '高水位用户', value: usage.summary.high_watermark_users },
                  { label: '低水位用户', value: usage.summary.low_watermark_users },
                ].map((card) => (
                  <div
                    key={card.label}
                    className="flex flex-col gap-[4px] rounded-[12px] border-[0.5px] border-[#e3e7f1] bg-[#f8f9fb] px-[14px] py-[10px]"
                  >
                    <span className="text-[11px] text-[#858b9c]">{card.label}</span>
                    <span className="text-[20px] font-semibold leading-[24px] text-[#18181a]">{card.value}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Month selector + search for usage table */}
            <div className="flex flex-wrap items-center gap-[8px] px-[12px]">
              <div className="flex items-center gap-[6px]">
                <button
                  type="button"
                  onClick={() => {
                    const d = new Date(usageMonth + '-01');
                    d.setMonth(d.getMonth() - 1);
                    const m = d.toISOString().slice(0, 7);
                    setUsageMonth(m);
                    void loadUsage(m);
                  }}
                  className="grid size-[28px] place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] hover:bg-[#f6f6f6]"
                >
                  <ChevronLeft className="size-[14px]" />
                </button>
                <span className="text-[13px] font-medium text-[#18181a] tabular-nums">{usageMonth}</span>
                <button
                  type="button"
                  disabled={usageMonth >= toClientDateISO(new Date()).slice(0, 7)}
                  onClick={() => {
                    const d = new Date(usageMonth + '-01');
                    d.setMonth(d.getMonth() + 1);
                    const m = d.toISOString().slice(0, 7);
                    setUsageMonth(m);
                    void loadUsage(m);
                  }}
                  className="grid size-[28px] place-items-center rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white text-[#757f9c] hover:bg-[#f6f6f6] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-white"
                >
                  <ChevronRight className="size-[14px]" />
                </button>
              </div>
              <div className="relative flex-1 max-w-[240px]">
                <Search className="absolute left-[8px] top-1/2 size-[14px] -translate-y-1/2 text-[#c0c6d4]" />
                <Input
                  value={usageSearch}
                  onChange={(e) => setUsageSearch(e.target.value)}
                  placeholder="搜索用户 / 消费组"
                  className="h-[30px] pl-[28px] text-[12px]"
                />
              </div>
            </div>

            {/* Usage table */}
            <DataTable
              aria-label="用量明细"
              columns={usageColumns}
              data={filteredUsageItems}
              rowKey={(row) => row.id}
              loading={usageLoading}
              emptyText="暂无用量数据"
            />

            {/* Quota rules section */}
            <div className="flex items-center justify-between px-[12px] pt-[12px]">
              <div className="flex items-center gap-[6px] text-[#757f9c]">
                <SlidersHorizontal className="size-[14px] shrink-0" />
                <span className="text-[14px] font-normal leading-none">配额规则</span>
              </div>
              <UIButton
                onClick={() => {
                  setQrOpen(true);
                  setQrName('');
                  setQrGateway(gateways[0]?.name ?? '');
                  setQrDimension('token');
                  setQrLimit('');
                  setQrPeriod('month');
                }}
                className="h-[30px] gap-[4px] rounded-[8px] bg-[#18181a] px-[12px] text-[12px] font-normal text-white hover:bg-[#303030]"
              >
                <Plus className="size-[13px]" />
                新建规则
              </UIButton>
            </div>
            <DataTable
              aria-label="配额规则列表"
              columns={ruleColumns}
              data={rules}
              rowKey={(row) => row.id}
              loading={rulesLoading}
              emptyText="暂无配额规则，点击右上角新建"
            />
          </>
        )}

        {/* Consumer groups */}
        {tab === 'groups' && (
          <>
            <div className="flex items-center justify-between gap-[10px] px-[12px] text-[#757f9c]">
              <div className="flex items-center gap-[6px]">
                <Users className="size-[14px] shrink-0" />
                <span className="text-[14px] font-normal leading-none">消费者列表</span>
                <span className="text-[12px] text-[#c0c6d4]">随时启用/停用或更换配额规则</span>
              </div>
              <div className="flex items-center gap-[8px]">
                {lastSyncedAt && (
                  <span className="hidden text-[12px] text-[#c0c6d4] lg:inline">
                    上次同步 {formatTime(lastSyncedAt)}
                  </span>
                )}
                <UIButton
                  onClick={syncFromAliyun}
                  disabled={syncing}
                  className="h-[30px] gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] disabled:opacity-60"
                >
                  <RefreshCw className={cn('size-[13px]', syncing && 'animate-spin')} />
                  {syncing ? '同步中…' : '同步阿里云'}
                </UIButton>
                <div className="relative w-[200px]">
                  <Search className="absolute left-[8px] top-1/2 size-[14px] -translate-y-1/2 text-[#c0c6d4]" />
                  <Input
                    value={consumerSearch}
                    onChange={(e) => setConsumerSearch(e.target.value)}
                    placeholder="搜索消费者名称"
                    className="h-[30px] pl-[28px] text-[12px]"
                  />
                </div>
              </div>
            </div>
            <DataTable
              aria-label="消费者列表"
              columns={consumerColumns}
              data={filteredConsumers}
              rowKey={(row) => row.id}
              loading={consumersLoading}
              emptyText={consumerSearch.trim() ? '无匹配的消费者' : '暂无消费者'}
            />

            <div className="flex items-center gap-[6px] px-[12px] pt-[8px] text-[#757f9c]">
              <Users className="size-[14px] shrink-0" />
              <span className="text-[14px] font-normal leading-none">消费组（只读）</span>
            </div>
            <DataTable
              aria-label="消费组列表"
              columns={groupColumns}
              data={groups}
              rowKey={(row) => row.id}
              loading={groupsLoading}
              emptyText="暂无消费组"
            />
          </>
        )}

        {/* User management（原「账号管理」页面并入） */}
        {tab === 'users' && <AccountsPanel currentUser={currentUser} />}
      </div>

      {/* ----------------------------------------------------------------- */}
      {/* Approve dialog: create consumer + bind group + bind quota rule */}
      {/* ----------------------------------------------------------------- */}
      <Dialog open={Boolean(approveTarget)} onOpenChange={(open) => !open && setApproveTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[480px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">通过申请并创建消费者</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            申请：{approveTarget?.purpose || '（未填写用途）'} · 申请人 {approveTarget?.username || '-'}
          </p>

          {/* Consumer name input */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">消费者名称</span>
            <Input
              value={approveConsumerName}
              onChange={(e) => setApproveConsumerName(e.target.value)}
              placeholder="如 ailab_zhangwei"
              className="h-[34px] font-mono text-[13px]"
            />
            <span className="text-[11px] leading-[16px] text-[#858b9c]">
              通过后将在阿里云创建同名消费者，API Key 由系统自动生成并回传给申请人。
            </span>
          </label>

          {/* Consumer group select */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">消费组</span>
            <Select
              value={approveGroupId}
              onValueChange={(value) => {
                setApproveGroupId(value);
                setApproveRuleId('');
              }}
            >
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择消费组" />
              </SelectTrigger>
              <SelectContent>
                {groups.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    无可用消费组
                  </SelectItem>
                ) : (
                  groups.map((g) => (
                    <SelectItem key={g.id} value={g.external_consumer_group_id || g.id}>
                      {g.name}（{g.gateway_name || '-'}）
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>

          {/* Quota rule select (filtered to same gateway as group) */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额规则</span>
            <Select value={approveRuleId} onValueChange={setApproveRuleId}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder={approveGroupId ? '选择配额规则' : '请先选择消费组'} />
              </SelectTrigger>
              <SelectContent>
                {!approveGroupId || approveCompatibleRules.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    {approveGroupId ? '无同网关配额规则' : '请先选择消费组'}
                  </SelectItem>
                ) : (
                  approveCompatibleRules.map((r) => (
                    <SelectItem key={r.id} value={r.external_rule_id || r.id}>
                      {r.name} ({r.quota_limit.toLocaleString('zh-CN')}/{PERIOD_LABEL[r.period_type] ?? r.period_type})
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>

          {/* Gateway URL input */}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">网关地址</span>
            <Input
              value={approveApiUrl}
              onChange={(e) => setApproveApiUrl(e.target.value)}
              placeholder="https://ai-gateway.folidaymall.com/v1/chat/completions"
              className="h-[34px] font-mono text-[12px]"
            />
            <span className="text-[11px] leading-[16px] text-[#858b9c]">
              下发给申请人的调用地址，可在审批时手动修改。
            </span>
          </label>

          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            通过后将在阿里云创建消费者并绑定消费组与配额规则，Model API 授权请在阿里云控制台完成。分配的 API Key 与网关地址仅申请人本人可见。
          </p>

          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={approving}
              onClick={() => setApproveTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={approving || !approveConsumerName.trim() || !approveGroupId || !approveRuleId}
              onClick={() => void confirmApprove()}
              className="h-[32px] w-[96px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {approving && <LoaderCircle className="size-[14px] animate-spin" />}
              确认通过
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Reject dialog */}
      <Dialog open={Boolean(rejectTarget)} onOpenChange={(open) => !open && setRejectTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">驳回申请</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            申请：{rejectTarget?.purpose || '（未填写用途）'} · 申请人 {rejectTarget?.username || '-'}
          </p>
          <Textarea
            rows={3}
            value={rejectNote}
            disabled={rejecting}
            placeholder="驳回原因（可选）"
            onChange={(event) => setRejectNote(event.target.value)}
            className="min-h-[72px] resize-y text-[12px]"
          />
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={rejecting}
              onClick={() => setRejectTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={rejecting}
              onClick={() => void confirmReject()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#c0392b] px-[12px] text-[14px] font-normal text-white hover:bg-[#a93226]"
            >
              {rejecting && <LoaderCircle className="size-[14px] animate-spin" />}
              确认驳回
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Revoke confirm dialog */}
      <Dialog open={Boolean(revokeTarget)} onOpenChange={(open) => !open && !revoking && setRevokeTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">确认吊销 API Key</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            申请人：{revokeTarget?.username || '-'} · Key {revokeTarget?.api_key_masked || '-'}
          </p>
          {revokePreviewLoading ? (
            <div className="flex items-center gap-[8px] text-[12px] text-[#858b9c]">
              <LoaderCircle className="size-[13px] animate-spin" />
              正在检查该消费者名下的 API Key…
            </div>
          ) : revokePreview?.will_disable_consumer ? (
            <div className="rounded-[10px] border-[0.5px] border-[#f3d5d2] bg-[#fdf3f2] px-[12px] py-[10px] text-[12px] leading-[18px] text-[#c0392b]">
              当前消费者只有一个 API Key，如果吊销则停用该消费者。
            </div>
          ) : revokePreview ? (
            <div className="rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-[#f8f9fb] px-[12px] py-[10px] text-[12px] leading-[18px] text-[#464c5e]">
              该消费者名下共有 {revokePreview.api_key_count} 个 API Key，吊销后仅移除当前这一个，消费者继续可用。
            </div>
          ) : (
            <div className="rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-[#f8f9fb] px-[12px] py-[10px] text-[12px] leading-[18px] text-[#464c5e]">
              吊销后将同步删除阿里云网关侧对应的 API Key 凭证，该操作不可恢复。
            </div>
          )}
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={revoking}
              onClick={() => setRevokeTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={revoking || revokePreviewLoading}
              onClick={() => revokeTarget && void revoke(revokeTarget)}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#c0392b] px-[12px] text-[14px] font-normal text-white hover:bg-[#a93226]"
            >
              {revoking && <LoaderCircle className="size-[14px] animate-spin" />}
              确认吊销
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Quota rule create dialog */}
      <Dialog open={qrOpen} onOpenChange={setQrOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[480px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">新建配额规则</DialogTitle>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">规则名称</span>
            <Input
              value={qrName}
              onChange={(e) => setQrName(e.target.value)}
              disabled={qrSubmitting}
              placeholder="例如：研发组-月度配额"
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">网关</span>
            <Select value={qrGateway} onValueChange={setQrGateway}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择网关" />
              </SelectTrigger>
              <SelectContent>
                {gateways.length === 0 ? (
                  <SelectItem value="__none__" disabled>
                    未配置网关
                  </SelectItem>
                ) : (
                  gateways.map((g) => (
                    <SelectItem key={g.gateway_id} value={g.name}>
                      {g.name}
                    </SelectItem>
                  ))
                )}
              </SelectContent>
            </Select>
          </label>
          <div className="flex gap-[12px]">
            <label className="flex flex-1 flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">维度</span>
              <Select value={qrDimension} onValueChange={(v) => setQrDimension(v as 'token' | 'credit')}>
                <SelectTrigger className="h-[34px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="token">Token</SelectItem>
                  <SelectItem value="credit">Credit</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label className="flex flex-1 flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">周期</span>
              <Select value={qrPeriod} onValueChange={(v) => setQrPeriod(v as 'day' | 'week' | 'month')}>
                <SelectTrigger className="h-[34px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="day">自然日</SelectItem>
                  <SelectItem value="week">自然周</SelectItem>
                  <SelectItem value="month">自然月</SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">主体类型</span>
            <Select value={qrSubjectType} onValueChange={(v) => setQrSubjectType(v as 'consumer' | 'consumer_group')}>
              <SelectTrigger className="h-[34px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="consumer">按消费者分配（逐个绑定）</SelectItem>
                <SelectItem value="consumer_group">按消费组分配（整组共享）</SelectItem>
              </SelectContent>
            </Select>
          </label>
          {qrSubjectType === 'consumer_group' && (
            <div className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">绑定消费组</span>
              {groups.length === 0 ? (
                <p className="text-[12px] text-[#c0c6d4]">暂无消费组，请先在消费组页创建</p>
              ) : (
                <div className="flex max-h-[120px] flex-col gap-[4px] overflow-y-auto rounded-[8px] border border-[#eceef1] p-[8px]">
                  {groups.map((g) => (
                    <label key={g.id} className="flex items-center gap-[8px] text-[12px] text-[#464c5e]">
                      <input
                        type="checkbox"
                        checked={qrGroupIds.includes(g.id)}
                        onChange={(e) => {
                          setQrGroupIds((prev) =>
                            e.target.checked ? [...prev, g.id] : prev.filter((id) => id !== g.id),
                          );
                        }}
                        className="size-[14px] accent-[#18181a]"
                      />
                      {g.name}
                    </label>
                  ))}
                </div>
              )}
              <p className="text-[11px] leading-[16px] text-[#858b9c]">
                组粒度配额（网关 2.1.21+）：整组共享该规则限额，成员入组即生效，无需逐个绑定。
              </p>
            </div>
          )}
          {qrSubjectType === 'consumer' && (
            <div className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">
                绑定消费者<span className="ml-[4px] font-normal text-[#a3aaba]">（可选，搜索后逐个选中）</span>
              </span>
              {/* 已选中的消费者，标签形式可移除 */}
              {qrConsumerIds.length > 0 && (
                <div className="flex flex-wrap gap-[6px]">
                  {qrConsumerIds.map((cid) => {
                    const c = consumers.find((item) => (item.external_consumer_id || item.id) === cid);
                    return (
                      <span
                        key={cid}
                        className="inline-flex h-[24px] items-center gap-[4px] rounded-[6px] bg-[#f3f4f6] px-[8px] text-[12px] text-[#464c5e]"
                      >
                        {c?.name ?? cid}
                        <button
                          type="button"
                          disabled={qrSubmitting}
                          onClick={() => setQrConsumerIds((prev) => prev.filter((id) => id !== cid))}
                          className="rounded-[4px] p-[1px] text-[#a3aaba] transition-colors hover:bg-[#e3e7f1] hover:text-[#464c5e]"
                          title="移除"
                        >
                          <X className="size-[12px]" />
                        </button>
                      </span>
                    );
                  })}
                </div>
              )}
              <div className="relative">
                <Search className="pointer-events-none absolute left-[10px] top-1/2 size-[14px] -translate-y-1/2 text-[#a3aaba]" />
                <Input
                  value={qrConsumerSearch}
                  onChange={(e) => setQrConsumerSearch(e.target.value)}
                  disabled={qrSubmitting}
                  placeholder="输入名称搜索消费者，点击结果选中"
                  className="h-[34px] pl-[30px] text-[12px]"
                />
              </div>
              {qrConsumerSearch.trim() && (
                <div className="flex max-h-[140px] flex-col overflow-y-auto rounded-[8px] border border-[#eceef1]">
                  {(() => {
                    const kw = qrConsumerSearch.trim().toLowerCase();
                    const matched = consumers.filter(
                      (c) =>
                        c.name.toLowerCase().includes(kw) ||
                        (c.external_consumer_id || '').toLowerCase().includes(kw),
                    );
                    const selectable = matched.filter(
                      (c) => !qrConsumerIds.includes(c.external_consumer_id || c.id),
                    );
                    if (selectable.length === 0) {
                      return (
                        <p className="px-[10px] py-[8px] text-[12px] text-[#c0c6d4]">
                          {matched.length > 0 ? '匹配的消费者均已选中' : '无匹配消费者'}
                        </p>
                      );
                    }
                    return selectable.map((c) => {
                      const cid = c.external_consumer_id || c.id;
                      return (
                        <button
                          key={c.id}
                          type="button"
                          disabled={qrSubmitting}
                          onClick={() => {
                            setQrConsumerIds((prev) => [...prev, cid]);
                            setQrConsumerSearch('');
                          }}
                          className="flex items-center gap-[8px] px-[10px] py-[7px] text-left text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6]"
                        >
                          <span className="truncate font-medium">{c.name}</span>
                          {c.consumer_group_name && (
                            <span className="truncate text-[11px] text-[#a3aaba]">{c.consumer_group_name}</span>
                          )}
                          <Plus className="ml-auto size-[13px] shrink-0 text-[#a3aaba]" />
                        </button>
                      );
                    });
                  })()}
                </div>
              )}
              <p className="text-[11px] leading-[16px] text-[#858b9c]">
                消费者粒度：每个消费者独立占用该规则限额。可留空创建后再绑定。
              </p>
            </div>
          )}
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额上限</span>
            <Input
              type="number"
              min={1}
              value={qrLimit}
              onChange={(e) => setQrLimit(e.target.value)}
              disabled={qrSubmitting}
              placeholder="例如 60000"
              className="h-[34px] text-[12px]"
            />
          </label>
          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            将在选定网关下创建一个阿里云 QuotaRule。审批时选择此规则即可将消费组纳入限流。
          </p>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={qrSubmitting}
              onClick={() => setQrOpen(false)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={qrSubmitting}
              onClick={() => void submitQuotaRule()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {qrSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              创建
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Consumer group owner edit dialog */}
      <Dialog open={Boolean(editOwnerTarget)} onOpenChange={(open) => !open && setEditOwnerTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">修改消费组归属</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            消费组：{editOwnerTarget?.name}
            {editOwnerTarget?.gateway_name ? ` · 网关 ${editOwnerTarget.gateway_name}` : ''}
          </p>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">归属</span>
            <Input
              value={editOwnerValue}
              onChange={(e) => setEditOwnerValue(e.target.value)}
              disabled={editOwnerSubmitting}
              placeholder="填写业务归属方，留空表示清空"
              className="h-[34px] text-[12px]"
            />
          </label>
          <p className="text-[11px] leading-[16px] text-[#a3aaba]">
            归属为业务记录字段，仅保存在本系统，不会同步至阿里云。
          </p>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={editOwnerSubmitting}
              onClick={() => setEditOwnerTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={editOwnerSubmitting}
              onClick={() => void submitGroupOwner()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {editOwnerSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Quota rule edit dialog */}
      <Dialog open={Boolean(editRule)} onOpenChange={(open) => !open && setEditRule(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">编辑配额规则</DialogTitle>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">规则名称</span>
            <Input
              value={editRuleName}
              onChange={(e) => setEditRuleName(e.target.value)}
              disabled={editRuleSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额上限</span>
            <Input
              type="number"
              min={1}
              value={editRuleLimit}
              onChange={(e) => setEditRuleLimit(e.target.value)}
              disabled={editRuleSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">周期</span>
            {/* periodType 创建后云端不可改，编辑时只读展示 */}
            <div className="flex h-[34px] items-center rounded-[8px] border border-[#eceef1] bg-[#f6f6f6] px-[12px] text-[12px] text-[#757f9c]">
              {PERIOD_LABEL[editRulePeriod] ?? editRulePeriod}
              <span className="ml-auto text-[11px] text-[#a3aaba]">创建后不可修改</span>
            </div>
          </label>
          <div className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">绑定消费组</span>
            {editRuleGroupsLoading ? (
              <p className="flex items-center gap-[6px] text-[12px] text-[#858b9c]">
                <LoaderCircle className="size-[13px] animate-spin" /> 加载绑定信息…
              </p>
            ) : groups.length === 0 ? (
              <p className="text-[12px] text-[#c0c6d4]">暂无消费组</p>
            ) : (
              <div className="flex max-h-[120px] flex-col gap-[4px] overflow-y-auto rounded-[8px] border border-[#eceef1] p-[8px]">
                {groups.map((g) => {
                  const gid = g.external_consumer_group_id || g.id;
                  return (
                    <label key={g.id} className="flex items-center gap-[8px] text-[12px] text-[#464c5e]">
                      <input
                        type="checkbox"
                        checked={editRuleGroups.includes(gid)}
                        onChange={(e) => {
                          setEditRuleGroups((prev) =>
                            e.target.checked ? [...prev, gid] : prev.filter((id) => id !== gid),
                          );
                        }}
                        className="size-[14px] accent-[#18181a]"
                      />
                      {g.name}
                    </label>
                  );
                })}
              </div>
            )}
            <p className="text-[11px] leading-[16px] text-[#858b9c]">
              勾选/取消勾选保存后生效（新增或移出规则）。消费者主体的绑定关系不在本页调整，请到阿里云控制台操作。
            </p>
          </div>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={editRuleSubmitting}
              onClick={() => setEditRule(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={editRuleSubmitting}
              onClick={() => void submitEditRule()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {editRuleSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Consumer quota change dialog */}
      <Dialog open={Boolean(consumerQuotaTarget)} onOpenChange={(open) => !open && setConsumerQuotaTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">修改消费者配额规则</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            消费者：{consumerQuotaTarget?.name} · 消费组 {consumerQuotaTarget?.consumer_group_name || '未分组'} · 网关{' '}
            {consumerQuotaTarget?.gateway_name || '-'}
          </p>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">配额规则</span>
            <Select value={consumerQuotaRuleId} onValueChange={setConsumerQuotaRuleId}>
              <SelectTrigger className="h-[34px]">
                <SelectValue placeholder="选择配额规则" />
              </SelectTrigger>
              <SelectContent>
                {(() => {
                  const compatible = rules.filter((r) => r.gateway_id === consumerQuotaTarget?.gateway_id);
                  if (compatible.length === 0) {
                    return (
                      <SelectItem value="__none__" disabled>
                        无同网关配额规则
                      </SelectItem>
                    );
                  }
                  return compatible.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.name} ({r.quota_limit.toLocaleString('zh-CN')}/{PERIOD_LABEL[r.period_type] ?? r.period_type})
                    </SelectItem>
                  ));
                })()}
              </SelectContent>
            </Select>
          </label>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={consumerQuotaSubmitting}
              onClick={() => setConsumerQuotaTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={consumerQuotaSubmitting || !consumerQuotaRuleId}
              onClick={() => void confirmConsumerQuotaChange()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {consumerQuotaSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              确认
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      {/* Adjust quota dialog */}
      <Dialog open={Boolean(adjustTarget)} onOpenChange={(open) => !open && setAdjustTarget(null)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[14px] rounded-[14px] px-[20px] py-[16px] sm:max-w-[440px]"
        >
          <DialogTitle className="text-[15px] font-medium text-[#18181a]">调整配额</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#757f9c]">
            用户：{adjustTarget?.username || '-'} · 当前用量 {adjustTarget?.used_amount.toLocaleString('zh-CN') ?? 0}
          </p>
          <label className="flex flex-col gap-[6px]">
            <span className="text-[12px] font-medium text-[#464c5e]">新配额上限</span>
            <Input
              type="number"
              min={1}
              value={adjustLimit}
              onChange={(e) => setAdjustLimit(e.target.value)}
              disabled={adjustSubmitting}
              className="h-[34px] text-[12px]"
            />
          </label>
          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={adjustSubmitting}
              onClick={() => setAdjustTarget(null)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:bg-[#f6f6f6]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={adjustSubmitting || !adjustLimit}
              onClick={() => void submitAdjustQuota()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030] disabled:opacity-60"
            >
              {adjustSubmitting && <LoaderCircle className="size-[14px] animate-spin" />}
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
