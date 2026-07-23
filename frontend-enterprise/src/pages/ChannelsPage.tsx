import { useEffect, useRef, useState } from 'react';
import { notify } from '@/components/ui/app-toast';

import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  RadioGroup,
  RadioGroupItem,
  Switch,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';

import { api, TENANT_ID } from '../api/client';
import IconAdd from '../assets/icons/add.svg?react';
import IconAlignJustify from '../assets/icons/align-justify.svg?react';
import IconChat from '../assets/icons/chat.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconAccount from '../assets/icons/sys-accounts.svg?react';
import type { EnterpriseAuthUser } from '../auth';
import { canManageEmployeeAgent, employeeDisplayName } from '../employee';
import { getDateLocale } from '@/i18n';
import { getClientTimeZone, parseBackendDateTime } from '@/lib/timezone';
import { cn } from '@/lib/utils';
import type {
  AgentProfileRead,
  ChannelBindingRead,
  ChannelBindCodeRead,
  ChannelConversationMessageRead,
  ChannelConversationRead,
  ChannelDeliveryDay,
  ChannelDeliveryDayPage,
  ChannelDeliveryRead,
  ChannelIdentityBindingRead,
  ChannelMetaRead,
  PagedResponse,
} from '../types';
import WechatSetup from './channels/WechatSetup';
import WecomSetup from './channels/WecomSetup';
import { StatusBadge } from './scheduled-tasks/StatusBadge';
import { formatTime, type BadgeTone } from './scheduled-tasks/shared';

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

const BINDING_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  pending: { tone: 'blue', text: '待扫码' },
  active: { tone: 'green', text: '已接入' },
  expired: { tone: 'red', text: '已过期' },
  disabled: { tone: 'gray', text: '已停用' },
};

const DELIVERY_STATUS_BADGE: Record<string, { tone: BadgeTone; text: string }> = {
  delivered: { tone: 'green', text: '已送达' },
  failed: { tone: 'red', text: '投递失败' },
  pending: { tone: 'blue', text: '待投递' },
  sending: { tone: 'orange', text: '投递中' },
};

const DELIVERY_KIND_LABEL: Record<string, string> = {
  reply: '回复',
  error_notice: '错误通知',
};

const WECHAT_COMMANDS: Array<{ command: string; description: string }> = [
  { command: '/员工', description: '查看可调度员工' },
  { command: '/切换 <员工名> 或 /<员工名>', description: '切换当前员工' },
  { command: '/当前', description: '查看当前员工' },
  { command: '/帮助', description: '查看指令说明' },
];

const CHANNEL_BLURB: Record<string, string> = {
  wechat: '扫码接入，微信用户直接与数字员工对话。',
  wecom: '填入企业微信智能机器人的凭证完成接入。',
};

const CAPABILITY_LABEL: Record<string, string> = {
  typing: '输入状态',
};

function messageDisplay(
  msg: ChannelConversationMessageRead,
  conversation: ChannelConversationRead,
): { label: string; content: string } {
  if (msg.role === 'user') {
    if (conversation.is_group) {
      const match = msg.content.match(/^\[发送者:\s*([^\]]+)\]\n?/);
      if (match) return { label: match[1], content: msg.content.slice(match[0].length) };
    }
    return { label: '用户', content: msg.content };
  }
  if (msg.role === 'assistant') {
    return { label: conversation.agent_name || '员工', content: msg.content };
  }
  return { label: msg.role, content: msg.content };
}

function isSessionRecovering(binding: ChannelBindingRead): boolean {
  return (
    !binding.connected &&
    binding.status !== 'expired' &&
    Boolean(binding.session_expired ?? binding.config_json?.session_expired)
  );
}

function formatDay(value: string): string {
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleDateString(getDateLocale(), { timeZone: getClientTimeZone() });
}

function groupByDay<T>(
  items: T[],
  getTime: (item: T) => string,
): Array<{ day: string; items: T[] }> {
  const groups: Array<{ day: string; items: T[] }> = [];
  items.forEach((item) => {
    const day = formatDay(getTime(item));
    const last = groups[groups.length - 1];
    if (last && last.day === day) {
      last.items.push(item);
    } else {
      groups.push({ day, items: [item] });
    }
  });
  return groups;
}

export default function ChannelsPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const [bindings, setBindings] = useState<ChannelBindingRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState('');
  const [deliveriesLoading, setDeliveriesLoading] = useState(false);
  const [deliveryDays, setDeliveryDays] = useState<ChannelDeliveryDay[]>([]);
  const [deliveryTotalDays, setDeliveryTotalDays] = useState(0);
  const [expandedDays, setExpandedDays] = useState<Set<string>>(new Set());
  const [conversations, setConversations] = useState<ChannelConversationRead[]>([]);
  const [conversationsTotal, setConversationsTotal] = useState(0);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [activeConversation, setActiveConversation] = useState<ChannelConversationRead | null>(null);
  const [messages, setMessages] = useState<ChannelConversationMessageRead[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [channelMetas, setChannelMetas] = useState<ChannelMetaRead[]>([]);
  const [metasLoaded, setMetasLoaded] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createStep, setCreateStep] = useState<'channel' | 'agent'>('channel');
  const [createChannel, setCreateChannel] = useState('wechat');
  const [createAgentId, setCreateAgentId] = useState('');
  const [creating, setCreating] = useState(false);
  const [unbindOpen, setUnbindOpen] = useState(false);
  const [unbinding, setUnbinding] = useState(false);
  const [agentEditing, setAgentEditing] = useState(false);
  const [agentCandidates, setAgentCandidates] = useState<AgentProfileRead[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [selectedAgentIds, setSelectedAgentIds] = useState<Set<string>>(new Set());
  const [defaultAgentId, setDefaultAgentId] = useState('');
  const [savingAgents, setSavingAgents] = useState(false);
  const [autoRouteSaving, setAutoRouteSaving] = useState(false);
  const [bindCode, setBindCode] = useState<ChannelBindCodeRead | null>(null);
  const [bindCodeOpen, setBindCodeOpen] = useState(false);
  const [bindCodeLoading, setBindCodeLoading] = useState(false);
  const [bindCodeRemain, setBindCodeRemain] = useState(0);
  const [identityBindings, setIdentityBindings] = useState<ChannelIdentityBindingRead[]>([]);
  const [unbindIdentityTarget, setUnbindIdentityTarget] =
    useState<ChannelIdentityBindingRead | null>(null);
  const [unbindingIdentity, setUnbindingIdentity] = useState(false);

  const binding = bindings.find((item) => item.id === selectedId) || null;
  const channelIdentities = identityBindings.filter((item) => item.channel === binding?.channel);
  const bindCodeChannelName = binding ? channelName(binding.channel) : '微信';
  const selectedIdRef = useRef('');

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  function ifStale(snapshot: string): boolean {
    return snapshot !== selectedIdRef.current;
  }

  useEffect(() => {
    if (!bindCodeOpen || !bindCode) return undefined;
    const update = () => {
      const remain = Math.max(
        0,
        Math.floor((parseBackendDateTime(bindCode.expires_at).getTime() - Date.now()) / 1000),
      );
      setBindCodeRemain(remain);
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [bindCodeOpen, bindCode]);

  useEffect(() => {
    void load();
    void loadIdentityBindings();
    void loadChannelMetas();
  }, []);

  useEffect(() => {
    setAgentEditing(false);
    setDeliveryDays([]);
    setDeliveryTotalDays(0);
    setExpandedDays(new Set());
    setConversations([]);
    setConversationsTotal(0);
    setActiveConversation(null);
    setMessages([]);
    if (selectedId) {
      void loadDeliveries(selectedId);
      void loadConversations(selectedId);
    }
  }, [selectedId]);

  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<ChannelBindingRead[]>(
        `/api/enterprise/channels?tenant_id=${TENANT_ID}`,
      );
      setBindings(rows);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载渠道信息失败');
    } finally {
      setLoading(false);
    }
  }

  async function loadIdentityBindings() {
    try {
      const rows = await api.get<ChannelIdentityBindingRead[]>(
        `/api/enterprise/channels/my-identity-bindings?tenant_id=${TENANT_ID}`,
      );
      setIdentityBindings(rows);
    } catch {
      setIdentityBindings([]);
    }
  }

  async function loadChannelMetas() {
    try {
      const rows = await api.get<ChannelMetaRead[]>(
        `/api/enterprise/channels/meta?tenant_id=${TENANT_ID}`,
      );
      setChannelMetas(rows);
    } catch {
      setChannelMetas([]);
    } finally {
      setMetasLoaded(true);
    }
  }

  async function confirmUnbindIdentity() {
    if (!unbindIdentityTarget) return;
    setUnbindingIdentity(true);
    try {
      await api.delete(
        `/api/enterprise/channels/my-identity-bindings/${unbindIdentityTarget.channel}?tenant_id=${TENANT_ID}&external_user_id=${encodeURIComponent(unbindIdentityTarget.external_user_id)}`,
      );
      notify.success('已解除绑定');
      setUnbindIdentityTarget(null);
      await loadIdentityBindings();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '解除绑定失败');
    } finally {
      setUnbindingIdentity(false);
    }
  }

  function scopeLabel(identity: ChannelIdentityBindingRead): string {
    const scope = identity.external_account_scope || '';
    if (!scope) return channelName(identity.channel);
    if (binding?.corp_id && scope === binding.corp_id) return `企业： ${scope}`;
    return `Bot: ${scope}`;
  }

  async function loadDeliveries(bindingId: string, offset = 0) {
    const snapshot = selectedId;
    setDeliveriesLoading(true);
    try {
      const page = await api.get<ChannelDeliveryDayPage>(
        `/api/enterprise/channels/${bindingId}/deliveries/days?tenant_id=${TENANT_ID}&offset=${offset}&limit=7`,
      );
      if (ifStale(snapshot)) return;
      setDeliveryDays((current) => (offset === 0 ? page.days : [...current, ...page.days]));
      setDeliveryTotalDays(page.total_days);
      if (offset === 0 && page.days.length > 0) {
        setExpandedDays(new Set([page.days[0].date]));
      }
    } catch (error) {
      if (ifStale(snapshot)) return;
      notify.error(error instanceof Error ? error.message : '加载投递日志失败');
    } finally {
      setDeliveriesLoading(false);
    }
  }

  function toggleDeliveryDay(date: string) {
    setExpandedDays((current) => {
      const next = new Set(current);
      if (next.has(date)) {
        next.delete(date);
      } else {
        next.add(date);
      }
      return next;
    });
  }

  async function loadConversations(bindingId: string, offset = 0) {
    const snapshot = selectedId;
    setConversationsLoading(true);
    try {
      const page = await api.get<PagedResponse<ChannelConversationRead>>(
        `/api/enterprise/channels/${bindingId}/conversations?tenant_id=${TENANT_ID}&offset=${offset}&limit=20`,
      );
      if (ifStale(snapshot)) return;
      setConversations((current) => (offset === 0 ? page.items : [...current, ...page.items]));
      setConversationsTotal(page.total);
    } catch (error) {
      if (ifStale(snapshot)) return;
      notify.error(error instanceof Error ? error.message : '加载对话记录失败');
    } finally {
      setConversationsLoading(false);
    }
  }

  async function openConversation(item: ChannelConversationRead) {
    if (!binding) return;
    const snapshot = selectedId;
    setActiveConversation(item);
    setMessages([]);
    setMessagesLoading(true);
    try {
      const rows = await api.get<ChannelConversationMessageRead[]>(
        `/api/enterprise/channels/${binding.id}/conversations/${item.session_id}/messages?tenant_id=${TENANT_ID}`,
      );
      if (ifStale(snapshot)) return;
      setMessages(rows);
    } catch (error) {
      if (ifStale(snapshot)) return;
      notify.error(error instanceof Error ? error.message : '加载会话消息失败');
    } finally {
      setMessagesLoading(false);
    }
  }

  async function loadAgentCandidates() {
    setCandidatesLoading(true);
    try {
      const rows = await api.get<AgentProfileRead[]>(
        `/api/enterprise/agents?tenant_id=${TENANT_ID}`,
      );
      setAgentCandidates(
        // 整体智能体(开放广场载体)是系统资源池,不是可对外服务的岗位员工,与其他页面一致排除
        rows.filter((item) => !item.is_overall && canManageEmployeeAgent(item, currentUser)),
      );
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工列表失败');
    } finally {
      setCandidatesLoading(false);
    }
  }

  function openCreate() {
    setCreateStep('channel');
    setCreateChannel(channelMetas[0]?.channel || 'wechat');
    setCreateAgentId('');
    setCreateOpen(true);
    void loadAgentCandidates();
  }

  async function createBinding() {
    if (!createAgentId || creating) return;
    setCreating(true);
    try {
      const created = await api.post<ChannelBindingRead>('/api/enterprise/channels', {
        tenant_id: TENANT_ID,
        agent_id: createAgentId,
        channel: createChannel,
      });
      notify.success('渠道接入创建成功');
      setCreateOpen(false);
      setCreateAgentId('');
      await load();
      setSelectedId(created.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建渠道接入失败');
    } finally {
      setCreating(false);
    }
  }

  async function openBindCode() {
    if (bindCodeLoading) return;
    setBindCodeLoading(true);
    try {
      const result = await api.post<ChannelBindCodeRead>(
        `/api/enterprise/channels/bind-code?tenant_id=${TENANT_ID}`,
      );
      setBindCode(result);
      setBindCodeOpen(true);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '生成绑定码失败');
    } finally {
      setBindCodeLoading(false);
    }
  }

  async function copyBindCommand() {
    if (!bindCode) return;
    try {
      await navigator.clipboard.writeText(`/绑定 ${bindCode.code}`);
      notify.success('已复制');
    } catch {
      notify.error('复制失败');
    }
  }

  async function confirmUnbind() {
    if (!binding) return;
    setUnbinding(true);
    try {
      await api.delete(`/api/enterprise/channels/${binding.id}?tenant_id=${TENANT_ID}`);
      notify.success('已断开接入');
      setUnbindOpen(false);
      setAgentEditing(false);
      setSelectedId('');
      await load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '断开接入失败');
    } finally {
      setUnbinding(false);
    }
  }

  function openAgentEdit() {
    const mounted = binding?.agents || [];
    setSelectedAgentIds(new Set(mounted.map((item) => item.agent_id)));
    setDefaultAgentId(
      mounted.find((item) => item.is_default)?.agent_id || mounted[0]?.agent_id || '',
    );
    setAgentEditing(true);
    void loadAgentCandidates();
  }

  function toggleAgentSelect(agentIdToToggle: string, checked: boolean) {
    const next = new Set(selectedAgentIds);
    if (checked) {
      next.add(agentIdToToggle);
    } else {
      next.delete(agentIdToToggle);
    }
    setSelectedAgentIds(next);
    if (!next.has(defaultAgentId)) {
      setDefaultAgentId(next.values().next().value || '');
    }
  }

  async function saveAgents() {
    if (!binding || selectedAgentIds.size === 0 || savingAgents) return;
    setSavingAgents(true);
    try {
      const updated = await api.put<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}?tenant_id=${TENANT_ID}`,
        {
          tenant_id: TENANT_ID,
          agents: [...selectedAgentIds].map((id) => ({
            agent_id: id,
            is_default: id === defaultAgentId,
          })),
        },
      );
      setBindings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setAgentEditing(false);
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存可调度员工失败');
    } finally {
      setSavingAgents(false);
    }
  }

  async function toggleAutoRoute(next: boolean) {
    if (!binding || autoRouteSaving) return;
    setAutoRouteSaving(true);
    try {
      const updated = await api.put<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}?tenant_id=${TENANT_ID}`,
        { tenant_id: TENANT_ID, auto_route: next },
      );
      setBindings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '更新智能分发设置失败');
    } finally {
      setAutoRouteSaving(false);
    }
  }

  function metaFor(channel: string): ChannelMetaRead | undefined {
    return channelMetas.find((item) => item.channel === channel);
  }

  function channelName(channel: string): string {
    return metaFor(channel)?.name || (channel === 'wechat' ? '微信' : channel);
  }

  function setupKindFor(channel: string): string {
    return metaFor(channel)?.setup || (channel === 'wechat' ? 'qrcode' : 'credentials');
  }

  const bindingStatus = binding ? BINDING_STATUS_BADGE[binding.status] : undefined;
  // bot_id / ilink_bot_id 是 DTO 顶层字段(后端不回传 config_json)
  const botId = binding?.ilink_bot_id || binding?.bot_id || '';
  const mountedAgents = binding?.agents || [];
  const conversationGroups = groupByDay(conversations, (item) => item.updated_at);

  const deliveryColumns: DataTableColumn<ChannelDeliveryRead>[] = [
    { key: 'time', title: '时间', width: 170, render: (row) => formatTime(row.created_at) },
    {
      key: 'kind',
      title: '类型',
      width: 110,
      render: (row) => DELIVERY_KIND_LABEL[row.kind] || row.kind,
    },
    {
      key: 'status',
      title: '状态',
      width: 110,
      render: (row) => {
        const preset = DELIVERY_STATUS_BADGE[row.status] || {
          tone: 'gray' as BadgeTone,
          text: row.status || '暂无',
        };
        return <StatusBadge tone={preset.tone}>{preset.text}</StatusBadge>;
      },
    },
    { key: 'attempts', title: '重试次数', width: 90, render: (row) => `${row.attempts || 0}` },
    {
      key: 'error',
      title: '错误',
      className: 'whitespace-normal',
      render: (row) => <span className="wrap-break-word">{row.last_error || '暂无'}</span>,
    },
  ];

  const listView = (
    <div className="mt-[20px] flex flex-col gap-[16px]">
      <div className="flex items-center justify-end gap-[8px]">
        <UIButton
          onClick={openCreate}
          className="h-[34px] gap-[4px] rounded-[10px] bg-[#18181a] px-[20px] text-[12px] font-normal text-white hover:bg-[#303030]"
        >
          <IconAdd className="size-[14px]" />
          接入渠道
        </UIButton>
      </div>
      {bindings.length === 0 && !loading ? (
        <div className="flex min-h-[200px] flex-col items-center justify-center gap-[12px] rounded-[14px] bg-[#f6f6f6] text-[13px] text-[#858b9c]">
          <span>暂无渠道接入，接入后微信用户可通过斜杠指令在多个数字员工之间切换。</span>
          <UIButton onClick={openCreate} className={PRIMARY_BUTTON_CLASS}>
            接入渠道
          </UIButton>
        </div>
      ) : (
        <div className="grid gap-[12px]">
          {bindings.map((item) => {
            const status = BINDING_STATUS_BADGE[item.status];
            return (
              <article
                key={item.id}
                role="button"
                tabIndex={0}
                onClick={() => setSelectedId(item.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    setSelectedId(item.id);
                  }
                }}
                className="flex cursor-pointer flex-col gap-[10px] rounded-[14px] border border-[#eef0f4] bg-white p-[16px] transition-colors hover:border-[#cbd3e6]"
              >
                <div className="flex flex-wrap items-center gap-[10px]">
                  <IconChat className="size-[16px] shrink-0" />
                  <span className="text-[14px] font-semibold text-[#18181a]">
                    {channelName(item.channel)}
                  </span>
                  <StatusBadge tone={status?.tone || 'gray'}>
                    {status?.text || item.status}
                  </StatusBadge>
                  {item.status === 'active' && (
                    <span className="text-[12px] text-[#858b9c]">
                      {item.connected ? '已连接' : isSessionRecovering(item) ? '恢复中' : '未连接'}
                    </span>
                  )}
                  <span className="ml-auto text-[12px] text-[#858b9c]">
                    {formatTime(item.created_at)}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-[6px]">
                  <span className="text-[12px] text-[#858b9c]">可调度员工</span>
                  {(item.agents || []).length === 0 ? (
                    <span className="text-[12px] text-[#858b9c]">暂无可调度员工</span>
                  ) : (
                    (item.agents || []).map((agent) => (
                      <span
                        key={agent.agent_id}
                        className="inline-flex items-center gap-[6px] rounded-full bg-[#f2f3f7] px-[12px] py-[6px] text-[12px] text-[#18181a]"
                      >
                        {agent.name}
                        {agent.is_default && <StatusBadge tone="blue">默认</StatusBadge>}
                      </span>
                    ))
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );

  const detailView = !binding ? null : (
    <div className="mt-[20px] flex flex-col gap-[24px]">
      <div>
        <UIButton
          variant="outline"
          onClick={() => setSelectedId('')}
          className={OUTLINE_BUTTON_CLASS}
        >
          返回列表
        </UIButton>
      </div>

      <div className="flex flex-col gap-[16px] rounded-[14px] border border-[#eef0f4] p-[16px]">
        <div className="flex flex-wrap items-center justify-between gap-[12px]">
          <div className="flex min-w-0 items-center gap-[10px]">
            <IconChat className="size-[16px] shrink-0" />
            <span className="text-[14px] font-semibold text-[#18181a]">
              {channelName(binding.channel)}
            </span>
            <StatusBadge tone={bindingStatus?.tone || 'gray'}>
              {bindingStatus?.text || binding.status}
            </StatusBadge>
            {binding.status === 'active' && (
              <span className="text-[12px] text-[#858b9c]">
                {binding.connected ? '已连接' : isSessionRecovering(binding) ? '恢复中' : '未连接'}
              </span>
            )}
            {botId && (
              <span className="truncate text-[12px] text-[#858b9c]">Bot ID：{botId}</span>
            )}
          </div>
          <div className="flex items-center gap-[8px]">
            <UIButton
              variant="outline"
              onClick={() => setUnbindOpen(true)}
              className={OUTLINE_BUTTON_CLASS}
            >
              断开接入
            </UIButton>
          </div>
        </div>
        {setupKindFor(binding.channel) === 'credentials' ? (
          <WecomSetup
            key={binding.id}
            binding={binding}
            meta={metaFor(binding.channel)}
            onChanged={(updated) =>
              setBindings((current) =>
                current.map((item) => (item.id === updated.id ? updated : item)),
              )
            }
          />
        ) : (
          <WechatSetup binding={binding} onChanged={() => void load()} />
        )}
        <div className="flex items-center justify-between gap-[12px] border-t border-[#eef0f4] pt-[16px]">
          <div className="flex min-w-0 flex-col gap-[4px]">
            <span className="text-[13px] font-semibold text-[#18181a]">智能分发</span>
            <span className="text-[12px] leading-[1.6] text-[#858b9c]">
              开启后，用户消息将按意图自动分发给合适的员工；/切换 仍可手动指定，手动指定后 10 分钟内不自动切换。
            </span>
          </div>
          <Switch
            checked={binding.auto_route ?? true}
            disabled={autoRouteSaving}
            onCheckedChange={(next) => void toggleAutoRoute(next)}
          />
        </div>
      </div>

      <section aria-label="身份绑定">
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAccount className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">身份绑定</span>
        </div>
        <div className="flex flex-col gap-[10px] rounded-[14px] border border-[#eef0f4] p-[16px]">
          {channelIdentities.length > 0 ? (
            channelIdentities.map((identity) => (
              <div
                key={`${identity.channel}_${identity.external_user_id}_${identity.external_account_scope || ''}`}
                className="flex flex-wrap items-center gap-[10px]"
              >
                <StatusBadge tone="green">
                  {`已绑定：${identity.display_name || identity.external_user_id}`}
                </StatusBadge>
                <span className="rounded-full bg-[#f2f3f7] px-[8px] py-[2px] text-[10px] text-[#858b9c]">
                  {scopeLabel(identity)}
                </span>
                <UIButton
                  variant="outline"
                  onClick={() => setUnbindIdentityTarget(identity)}
                  className={OUTLINE_BUTTON_CLASS}
                >
                  解除绑定
                </UIButton>
              </div>
            ))
          ) : (
            <div className="flex flex-wrap items-center gap-[10px]">
              <UIButton
                variant="outline"
                onClick={() => void openBindCode()}
                disabled={bindCodeLoading}
                className={OUTLINE_BUTTON_CLASS}
              >
                {`绑定我的${channelName(binding.channel)}`}
              </UIButton>
            </div>
          )}
        </div>
      </section>

      <section aria-label="可调度员工">
        <div className="mb-[16px] flex items-center justify-between gap-[6px] px-[12px] text-[#757f9c]">
          <div className="flex items-center gap-[6px]">
            <IconAccount className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">可调度员工</span>
          </div>
          {!agentEditing && (
            <UIButton variant="outline" onClick={openAgentEdit} className={OUTLINE_BUTTON_CLASS}>
              编辑
            </UIButton>
          )}
        </div>
        <p className="mb-[16px] px-[12px] text-[12px] text-[#858b9c]">
          挂载后，该渠道的所有用户均可与这些员工对话。
        </p>
        {agentEditing ? (
          <div className="flex flex-col gap-[12px] rounded-[14px] border border-[#eef0f4] p-[16px]">
            {candidatesLoading ? (
              <span className="py-[12px] text-center text-[12px] text-[#858b9c]">加载中…</span>
            ) : agentCandidates.length === 0 ? (
              <span className="py-[12px] text-center text-[12px] text-[#858b9c]">暂无可用员工</span>
            ) : (
              <RadioGroup
                value={defaultAgentId}
                onValueChange={setDefaultAgentId}
                className="grid gap-[10px]"
              >
                {agentCandidates.map((agent) => {
                  const checked = selectedAgentIds.has(agent.id);
                  return (
                    <div
                      key={agent.id}
                      className="flex items-center gap-[8px] text-[13px] text-[#18181a]"
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(value) => toggleAgentSelect(agent.id, value === true)}
                      />
                      <span className="min-w-0 flex-1 truncate">{employeeDisplayName(agent)}</span>
                      <span className="flex shrink-0 items-center gap-[6px] text-[12px] text-[#858b9c]">
                        <RadioGroupItem value={agent.id} disabled={!checked} />
                        默认
                      </span>
                    </div>
                  );
                })}
              </RadioGroup>
            )}
            <div className="flex justify-end gap-[8px]">
              <UIButton
                variant="outline"
                onClick={() => setAgentEditing(false)}
                className={OUTLINE_BUTTON_CLASS}
              >
                取消
              </UIButton>
              <UIButton
                onClick={() => void saveAgents()}
                disabled={selectedAgentIds.size === 0 || savingAgents}
                className={PRIMARY_BUTTON_CLASS}
              >
                保存
              </UIButton>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap gap-[8px] rounded-[14px] border border-[#eef0f4] p-[16px]">
            {mountedAgents.length === 0 ? (
              <span className="text-[12px] text-[#858b9c]">暂无可调度员工</span>
            ) : (
              mountedAgents.map((item) => (
                <span
                  key={item.agent_id}
                  className="inline-flex items-center gap-[6px] rounded-full bg-[#f2f3f7] px-[12px] py-[6px] text-[12px] text-[#18181a]"
                >
                  {item.name}
                  {item.is_default && <StatusBadge tone="blue">默认</StatusBadge>}
                </span>
              ))
            )}
          </div>
        )}
      </section>

      <section aria-label="微信指令说明">
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconChat className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">微信指令说明</span>
        </div>
        <div className="flex flex-col gap-[8px] rounded-[14px] border border-[#eef0f4] p-[16px]">
          {WECHAT_COMMANDS.map((item) => (
            <div key={item.command} className="flex flex-wrap items-baseline gap-[8px] text-[12px]">
              <code className="rounded-[6px] bg-[#f2f3f7] px-[8px] py-[3px] text-[#18181a]">
                {item.command}
              </code>
              <span className="text-[#858b9c]">{item.description}</span>
            </div>
          ))}
        </div>
      </section>

      <section aria-label="对话记录">
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconChat className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">对话记录</span>
        </div>
        {activeConversation ? (
          <div className="flex flex-col gap-[12px] rounded-[14px] border border-[#eef0f4] p-[16px]">
            <div className="flex items-center gap-[10px]">
              <UIButton
                variant="outline"
                onClick={() => setActiveConversation(null)}
                className={OUTLINE_BUTTON_CLASS}
              >
                返回列表
              </UIButton>
              <span className="truncate text-[14px] font-semibold text-[#18181a]">
                {activeConversation.display_name || activeConversation.external_conv_id}
              </span>
              {activeConversation.is_group && <StatusBadge tone="blue">群</StatusBadge>}
            </div>
            {messagesLoading ? (
              <div className="py-[24px] text-center text-[12px] text-[#858b9c]">加载中…</div>
            ) : messages.length === 0 ? (
              <div className="py-[24px] text-center text-[12px] text-[#858b9c]">暂无消息</div>
            ) : (
              <div className="flex max-h-[480px] flex-col gap-[10px] overflow-y-auto pr-[4px]">
                {messages.map((msg) => {
                  const shown = messageDisplay(msg, activeConversation);
                  return (
                    <div key={msg.id} className="flex flex-col gap-[4px]">
                      <span className="text-[11px] text-[#a0a6b8]">
                        {shown.label} · {formatTime(msg.created_at)}
                      </span>
                      <span className="wrap-break-word rounded-[10px] bg-[#f6f6f6] px-[12px] py-[8px] text-[13px] leading-[1.6] text-[#18181a]">
                        {shown.content}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ) : conversationsLoading && conversations.length === 0 ? (
          <div className="rounded-[14px] border border-[#eef0f4] py-[24px] text-center text-[12px] text-[#858b9c]">
            加载中…
          </div>
        ) : conversations.length === 0 ? (
          <div className="flex min-h-[120px] items-center justify-center rounded-[14px] bg-[#f6f6f6] text-[13px] text-[#858b9c]">
            暂无对话记录
          </div>
        ) : (
          <div className="flex flex-col gap-[16px]">
            {conversationGroups.map((group) => (
              <div key={group.day} className="flex flex-col gap-[10px]">
                <span className="px-[4px] text-[12px] font-medium text-[#a0a6b8]">
                  {group.day}
                </span>
                {group.items.map((item) => (
                  <article
                    key={item.session_id}
                    role="button"
                    tabIndex={0}
                    onClick={() => void openConversation(item)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        void openConversation(item);
                      }
                    }}
                    className="flex cursor-pointer flex-col gap-[6px] rounded-[14px] border border-[#eef0f4] bg-white p-[16px] transition-colors hover:border-[#cbd3e6]"
                  >
                    <div className="flex items-center gap-[8px]">
                      <span className="truncate text-[13px] font-semibold text-[#18181a]">
                        {item.display_name || item.external_conv_id}
                      </span>
                      {item.is_group && <StatusBadge tone="blue">群</StatusBadge>}
                      <span className="shrink-0 text-[12px] text-[#858b9c]">{item.agent_name}</span>
                      <span className="ml-auto shrink-0 text-[12px] text-[#858b9c]">
                        {formatTime(item.updated_at)}
                      </span>
                    </div>
                    <div className="flex items-center gap-[8px] text-[12px] text-[#858b9c]">
                      <span className="min-w-0 truncate">
                        {item.last_message_preview || '暂无消息'}
                      </span>
                      <span className="ml-auto shrink-0">{`${item.message_count} 条`}</span>
                    </div>
                  </article>
                ))}
              </div>
            ))}
            {conversations.length < conversationsTotal && (
              <div className="flex justify-center">
                <UIButton
                  variant="outline"
                  disabled={conversationsLoading}
                  onClick={() =>
                    binding && void loadConversations(binding.id, conversations.length)
                  }
                  className={OUTLINE_BUTTON_CLASS}
                >
                  {`加载更多（已显示 ${conversations.length} / 共 ${conversationsTotal} 条）`}
                </UIButton>
              </div>
            )}
          </div>
        )}
      </section>

      <section aria-label="投递日志">
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAlignJustify className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">投递日志</span>
        </div>
        {deliveryDays.length === 0 ? (
          <DataTable
            aria-label="投递日志"
            columns={deliveryColumns}
            data={[]}
            rowKey={(row) => row.id}
            loading={deliveriesLoading}
            emptyText="暂无投递记录"
            size="compact"
            striped
            bordered
          />
        ) : (
          <div className="flex flex-col gap-[10px]">
            {deliveryDays.map((day) => {
              const expanded = expandedDays.has(day.date);
              return (
                <div
                  key={day.date}
                  className="overflow-hidden rounded-[14px] border border-[#eef0f4]"
                >
                  <button
                    type="button"
                    onClick={() => toggleDeliveryDay(day.date)}
                    className="flex w-full items-center gap-[8px] px-[16px] py-[12px] text-left transition-colors hover:bg-[#fafbfc]"
                  >
                    <IconChevronDown
                      className={cn(
                        'size-[14px] shrink-0 text-[#858b9c] transition-transform',
                        !expanded && '-rotate-90',
                      )}
                    />
                    <span className="text-[13px] font-medium text-[#18181a]">
                      {formatDay(`${day.date}T12:00:00`)}
                    </span>
                    <span className="text-[12px] text-[#858b9c]">{`${day.count} 条`}</span>
                  </button>
                  {expanded && (
                    <div className="border-t border-[#eef0f4]">
                      <DataTable
                        aria-label="投递日志"
                        columns={deliveryColumns}
                        data={day.items}
                        rowKey={(row) => row.id}
                        size="compact"
                        striped
                        bordered
                      />
                    </div>
                  )}
                </div>
              );
            })}
            {deliveryDays.length < deliveryTotalDays && (
              <div className="flex justify-center">
                <UIButton
                  variant="outline"
                  disabled={deliveriesLoading}
                  onClick={() => binding && void loadDeliveries(binding.id, deliveryDays.length)}
                  className={OUTLINE_BUTTON_CLASS}
                >
                  {`加载更多天（已显示 ${deliveryDays.length} / 共 ${deliveryTotalDays} 天）`}
                </UIButton>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader onLogout={onLogout} userName={currentUser?.username} title="渠道接入" />
      {binding ? detailView : listView}

      <Dialog open={createOpen} onOpenChange={setCreateOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[480px]"
        >
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {createStep === 'channel' ? '选择渠道' : '选择默认员工'}
          </DialogTitle>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {createStep === 'channel' ? (
              channelMetas.length === 0 ? (
                <div className="py-[24px] text-center text-[12px] text-[#858b9c]">
                  {metasLoaded ? '暂无可用渠道' : '加载中…'}
                </div>
              ) : (
                <div className="grid gap-[10px]">
                  {channelMetas.map((meta) => (
                    <article
                      key={meta.channel}
                      role="button"
                      tabIndex={0}
                      onClick={() => {
                        setCreateChannel(meta.channel);
                        setCreateStep('agent');
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          setCreateChannel(meta.channel);
                          setCreateStep('agent');
                        }
                      }}
                      className="flex cursor-pointer flex-col gap-[6px] rounded-[14px] border border-[#eef0f4] p-[16px] transition-colors hover:border-[#cbd3e6]"
                    >
                      <div className="flex items-center gap-[8px]">
                        <span className="text-[13px] font-semibold text-[#18181a]">
                          {meta.name}
                        </span>
                        {meta.capabilities.map((capability) => (
                          <span
                            key={capability}
                            className="rounded-full bg-[#f2f3f7] px-[8px] py-[2px] text-[10px] text-[#858b9c]"
                          >
                            {CAPABILITY_LABEL[capability] || capability}
                          </span>
                        ))}
                      </div>
                      <span className="text-[12px] text-[#858b9c]">
                        {CHANNEL_BLURB[meta.channel] || ''}
                      </span>
                    </article>
                  ))}
                </div>
              )
            ) : candidatesLoading ? (
              <div className="py-[24px] text-center text-[12px] text-[#858b9c]">加载中…</div>
            ) : agentCandidates.length === 0 ? (
              <div className="py-[24px] text-center text-[12px] text-[#858b9c]">暂无可用员工</div>
            ) : (
              <RadioGroup
                value={createAgentId}
                onValueChange={setCreateAgentId}
                className="grid gap-[10px]"
              >
                {agentCandidates.map((agent) => (
                  <div
                    key={agent.id}
                    className="flex items-center gap-[8px] text-[13px] text-[#18181a]"
                  >
                    <RadioGroupItem value={agent.id} />
                    <span className="min-w-0 flex-1 truncate">{employeeDisplayName(agent)}</span>
                  </div>
                ))}
              </RadioGroup>
            )}
          </div>
          <div className="flex justify-end gap-[8px]">
            {createStep === 'agent' && (
              <UIButton
                variant="outline"
                onClick={() => setCreateStep('channel')}
                className={OUTLINE_BUTTON_CLASS}
              >
                返回选择渠道
              </UIButton>
            )}
            <UIButton
              variant="outline"
              onClick={() => setCreateOpen(false)}
              className={OUTLINE_BUTTON_CLASS}
            >
              取消
            </UIButton>
            {createStep === 'agent' && (
              <UIButton
                onClick={() => void createBinding()}
                disabled={!createAgentId || creating}
                className={PRIMARY_BUTTON_CLASS}
              >
                创建
              </UIButton>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={bindCodeOpen}
        onOpenChange={(open) => {
          setBindCodeOpen(open);
          if (!open) void loadIdentityBindings();
        }}
      >
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[420px]"
        >
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {`绑定我的${bindCodeChannelName}`}
          </DialogTitle>
          {bindCode && (
            <div className="flex flex-col items-center gap-[12px]">
              <span className="text-[36px] font-semibold tracking-[8px] text-[#18181a]">
                {bindCode.code}
              </span>
              <span className="text-[12px] text-[#858b9c]">
                {bindCodeRemain > 0
                  ? `绑定码 ${Math.floor(bindCodeRemain / 60)} 分 ${bindCodeRemain % 60} 秒后过期`
                  : '绑定码已过期，请重新生成'}
              </span>
              <div className="flex items-center gap-[8px] rounded-[10px] bg-[#f6f6f6] px-[12px] py-[8px]">
                <code className="text-[13px] text-[#18181a]">{`/绑定 ${bindCode.code}`}</code>
                <UIButton
                  variant="outline"
                  onClick={() => void copyBindCommand()}
                  className={OUTLINE_BUTTON_CLASS}
                >
                  复制
                </UIButton>
              </div>
              <span className="text-center text-[12px] leading-[1.6] text-[#858b9c]">
                {`请在${bindCodeChannelName}中向你的数字员工发送以上指令，完成身份绑定。`}
              </span>
              {bindCodeRemain === 0 && (
                <UIButton
                  onClick={() => void openBindCode()}
                  disabled={bindCodeLoading}
                  className={PRIMARY_BUTTON_CLASS}
                >
                  重新生成
                </UIButton>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={unbindOpen}
        onOpenChange={setUnbindOpen}
        loading={unbinding}
        title="断开微信接入？"
        description="断开后微信 bot 将离线，需要重新扫码才能恢复；对话记录保留。确定断开接入吗？"
        confirmText="断开接入"
        onConfirm={() => void confirmUnbind()}
      />

      <ConfirmDialog
        open={Boolean(unbindIdentityTarget)}
        onOpenChange={(open) => {
          if (!open) setUnbindIdentityTarget(null);
        }}
        loading={unbindingIdentity}
        title="解除身份绑定？"
        description="解除后，该渠道对话将与你的账号分离，历史会话与记忆迁回渠道账号。确定解除绑定吗？"
        confirmText="解除绑定"
        onConfirm={() => void confirmUnbindIdentity()}
      />
    </div>
  );
}
