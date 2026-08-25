import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { notify } from '@/components/ui/app-toast';

import AppHeader from '@/components/AppHeader';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import SearchableSelect from '@/components/SearchableSelect';
import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  RadioGroup,
  RadioGroupItem,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';

import { api, TENANT_ID } from '../api/client';

const _CHANNEL_LABELS: Record<string, string> = { feishu: '飞书', dingtalk: '钉钉', wecom: '企业微信', wechat: '微信', web: '网页端' };
// 渠道转接通知运行时已支持飞书/企微私聊;钉钉/微信适配器只能回会话内消息,不在此列。
const HANDOFF_NOTIFY_CHANNELS = new Set(['feishu', 'wecom']);
import IconAdd from '../assets/icons/add.svg?react';
import IconAlignJustify from '../assets/icons/align-justify.svg?react';
import IconChat from '../assets/icons/chat.svg?react';
import IconChevronDown from '../assets/icons/chevron-down.svg?react';
import IconAccount from '../assets/icons/sys-accounts.svg?react';
import IconWarningFill from '../assets/icons/warning-fill.svg?react';
import type { EnterpriseAuthUser } from '../auth';
import { canManageEmployeeAgent, employeeDisplayName } from '../employee';
import { getDateLocale } from '@/i18n';
import { getClientTimeZone, parseBackendDateTime } from '@/lib/timezone';
import { cn } from '@/lib/utils';
import { copyTextToClipboard } from '@/lib/clipboard';
import type {
  AgentProfileRead,
  ChannelBindingRead,
  ChannelBindCodeRead,
  ChannelConversationMessageRead,
  ChannelConversationAttachment,
  ChannelConversationRead,
  ChannelDeliveryDay,
  ChannelDeliveryDayPage,
  ChannelDeliveryRead,
  ChannelIdentityBindingRead,
  ChannelMetaRead,
  PagedResponse,
  TeamRead,
} from '../types';
import { formatHandoffAssigneeValue, parseHandoffAssigneeValue } from '../lib/handoff-assignee';
import { feishuAppIdFromIdentityScope } from '../lib/identity-scope';
import WechatSetup from './channels/WechatSetup';
import WecomSetup from './channels/WecomSetup';
import FeishuSetup from './channels/FeishuSetup';
import DingTalkSetup from './channels/DingTalkSetup';
import BindingManagers from './channels/BindingManagers';
import {
  canDeleteBinding,
  canManageBinding,
  getChannelPresentation,
  ROLE_LABEL,
} from './channelPresentation';
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
  reaction_add: '收到确认',
  reaction_remove: '确认清理',
};

const CHANNEL_COMMANDS: Array<{ command: string; description: string }> = [
  { command: '/员工', description: '查看可调度员工' },
  { command: '/切换 <员工名> 或 /<员工名>', description: '切换当前员工' },
  { command: '/当前', description: '查看当前员工' },
  { command: '/帮助', description: '查看指令说明' },
];

const BINDING_NAME_MAX_LENGTH = 50;

// 接入默认名:渠道名 + YYYYMMDDHHMM,如「飞书202608250910」
function defaultBindingName(channelLabel: string): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}${pad(now.getHours())}${pad(now.getMinutes())}`;
  return `${channelLabel}${stamp}`;
}

function messageDisplay(
  msg: ChannelConversationMessageRead,
  conversation: ChannelConversationRead,
  userLabel: string,
): { label: string; content: string } {
  if (msg.role === 'user') {
    if (conversation.is_group) {
      const match = msg.content.match(/^\[发送者:\s*([^\]]+)\]\n?/);
      if (match) return { label: match[1], content: msg.content.slice(match[0].length) };
    }
    return { label: userLabel, content: msg.content };
  }
  if (msg.role === 'assistant') {
    return { label: conversation.agent_name || '员工', content: msg.content };
  }
  return { label: msg.role, content: msg.content };
}

function ChannelAttachmentView({
  attachment,
  bindingId,
  sessionId,
  messageId,
}: {
  attachment: ChannelConversationAttachment;
  bindingId: string;
  sessionId: string;
  messageId: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const path = `/api/enterprise/channels/${bindingId}/conversations/${sessionId}/messages/${messageId}/attachments/${attachment.id}?tenant_id=${TENANT_ID}`;

  useEffect(() => {
    if (attachment.kind !== 'image') return;
    let disposed = false;
    let objectUrl: string | null = null;
    setLoading(true);
    void api.blob(path).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      if (!disposed) setUrl(objectUrl);
      else URL.revokeObjectURL(objectUrl);
    }).catch(() => {
      if (!disposed) setUrl(null);
    }).finally(() => {
      if (!disposed) setLoading(false);
    });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachment.id, attachment.kind, path]);

  if (attachment.kind === 'image') {
    return url ? (
      <img src={url} alt={attachment.filename} className="max-h-[220px] max-w-[320px] rounded-[8px] object-contain" />
    ) : <span className="text-[12px] text-[#858b9c]">{loading ? '图片加载中…' : '图片暂不可用'}</span>;
  }
  return (
    <button
      type="button"
      className="text-left text-[12px] text-[#3b63c8] underline"
      onClick={() => void api.blob(path).then((blob) => {
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = attachment.filename;
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
      })}
    >
      {attachment.filename}
    </button>
  );
}

function isSessionRecovering(binding: ChannelBindingRead): boolean {
  return (
    !binding.connected &&
    binding.status !== 'expired' &&
    Boolean(binding.session_expired ?? binding.config_json?.session_expired)
  );
}

function attentionText(item: ChannelBindingRead): string {
  if (item.status === 'expired') {
    return item.channel === 'wechat'
      ? 'token 已失效，请重新扫码'
      : '当前未连接，请检查凭证或网络';
  }
  if (isSessionRecovering(item)) return '会话恢复中，系统将自动重试';
  return '当前未连接，请检查凭证或网络';
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
  const [createStep, setCreateStep] = useState<'channel' | 'name' | 'agent'>('channel');
  const [createChannel, setCreateChannel] = useState('wechat');
  const [createName, setCreateName] = useState('');
  const [createTarget, setCreateTarget] = useState<'agent' | 'team'>('agent');
  const [createAgentId, setCreateAgentId] = useState('');
  const [createTeamId, setCreateTeamId] = useState('');
  const [teams, setTeams] = useState<TeamRead[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTargetId, setRenameTargetId] = useState('');
  const [renameValue, setRenameValue] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [unbindOpen, setUnbindOpen] = useState(false);
  const [unbinding, setUnbinding] = useState(false);
  const [togglingStatus, setTogglingStatus] = useState(false);
  const [agentEditing, setAgentEditing] = useState(false);
  const [agentCandidates, setAgentCandidates] = useState<AgentProfileRead[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [selectedAgentIds, setSelectedAgentIds] = useState<Set<string>>(new Set());
  const [defaultAgentId, setDefaultAgentId] = useState('');
  const [savingAgents, setSavingAgents] = useState(false);
  const [autoRouteSaving, setAutoRouteSaving] = useState(false);
  const [tenantUsers, setTenantUsers] = useState<Array<{ id: string; username: string; display_name?: string; source?: string; channel_identities?: Array<{ channel: string; display_name?: string; external_user_id?: string; external_account_scope?: string }> }>>([]);
  const [handoffAssigneeSaving, setHandoffAssigneeSaving] = useState(false);
  const [bindCode, setBindCode] = useState<ChannelBindCodeRead | null>(null);
  const [bindCodeOpen, setBindCodeOpen] = useState(false);
  const [bindCodeLoading, setBindCodeLoading] = useState(false);
  const [bindCodeRemain, setBindCodeRemain] = useState(0);
  const [bindCodeTargetName, setBindCodeTargetName] = useState('');
  const [bindCodeTargetUserId, setBindCodeTargetUserId] = useState<string | undefined>();
  const [identityInviteUserId, setIdentityInviteUserId] = useState('');
  const [identityBindings, setIdentityBindings] = useState<ChannelIdentityBindingRead[]>([]);
  const [unbindIdentityTarget, setUnbindIdentityTarget] =
    useState<ChannelIdentityBindingRead | null>(null);
  const [unbindingIdentity, setUnbindingIdentity] = useState(false);

  const binding = bindings.find((item) => item.id === selectedId) || null;
  const navigate = useNavigate();
  // 身份绑定属于具体渠道账号 scope；同一用户可以分别绑定多个飞书应用。
  const channelIdentities = identityBindings.filter(
    (item) =>
      item.channel === binding?.channel &&
      (item.external_account_scope || '') === (binding?.identity_scope_key || ''),
  );
  const bindingScope = binding?.identity_scope_key || '';
  // 已绑定集合只统计内部成员:渠道懒建账号(外部客户)自带指向自己的身份,
  // 不应计入"邀请成员绑定"的绑定状态。展示与邀请下拉都排除当前用户——
  // 自己的绑定在上方"身份绑定"区有专门入口(绑定/解绑),避免同一人出现两次。
  const identityBoundInternalUserIds = new Set(
    tenantUsers
      .filter(
        (user) =>
          (!user.source || user.source === 'web') &&
          user.channel_identities?.some(
            (identity) =>
              identity.channel === binding?.channel &&
              (identity.external_account_scope || '') === bindingScope,
          ),
      )
      .map((user) => user.id),
  );
  const identityBoundUsers = tenantUsers.filter(
    (user) => identityBoundInternalUserIds.has(user.id) && user.id !== currentUser?.id,
  );
  const identityUnboundUsers = tenantUsers.filter(
    (user) =>
      (!user.source || user.source === 'web') &&
      user.id !== currentUser?.id &&
      !identityBoundInternalUserIds.has(user.id),
  );
  const bindCodeChannelName = binding
    ? channelName(binding.channel)
    : getChannelPresentation(createChannel).name;
  const selectedIdRef = useRef('');

  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  function ifStale(snapshot: string): boolean {
    return snapshot !== selectedIdRef.current;
  }

  async function loadTenantUsers() {
    try {
      const rows = await api.get<Array<{ id: string; username: string; display_name?: string; source?: string; channel_identities?: Array<{ channel: string; display_name?: string; external_user_id?: string; external_account_scope?: string }> }>>(
        `/api/auth/users?tenant_id=${TENANT_ID}&include_channel=true`,
      );
      setTenantUsers(rows);
    } catch {
      setTenantUsers([]);
    }
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
    void loadTeams();
    void loadTenantUsers();
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
        `/api/enterprise/channels/my-identity-bindings/${unbindIdentityTarget.channel}?tenant_id=${TENANT_ID}&external_user_id=${encodeURIComponent(unbindIdentityTarget.external_user_id)}&external_account_scope=${encodeURIComponent(unbindIdentityTarget.external_account_scope || '')}`,
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
    // 飞书 scope 是"app:{长度}:{appId}:tenant:{长度}:{tenantKey}"技术键,
    // 解析出 appId 展示,避免把整段内部键暴露给用户。
    const feishuAppId = feishuAppIdFromIdentityScope(scope);
    if (feishuAppId) return `飞书应用： ${feishuAppId}`;
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

  async function loadTeams() {
    setTeamsLoading(true);
    try {
      const rows = await api.get<TeamRead[]>(`/api/enterprise/teams?tenant_id=${TENANT_ID}`);
      setTeams(rows);
    } catch {
      // 团队列表仅用于绑定对象选择与名称映射，失败不影响主流程
      setTeams([]);
    } finally {
      setTeamsLoading(false);
    }
  }

  function teamNameFor(item: ChannelBindingRead): string {
    if (!item.team_id) return '';
    return item.team_name || teams.find((team) => team.id === item.team_id)?.name || '团队';
  }

  function teamLeaderName(teamId: string): string {
    const team = teams.find((item) => item.id === teamId);
    return team?.members.find((member) => member.role === 'leader')?.agent_name || '未设置';
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
    setCreateName('');
    setCreateTarget('agent');
    setCreateAgentId('');
    setCreateTeamId('');
    setCreateOpen(true);
    void loadAgentCandidates();
    void loadTeams();
  }

  async function createBinding() {
    const agentId = createTarget === 'agent' ? createAgentId : '';
    const teamId = createTarget === 'team' ? createTeamId : '';
    if ((!agentId && !teamId) || creating) return;
    setCreating(true);
    try {
      const created = await api.post<ChannelBindingRead>('/api/enterprise/channels', {
        tenant_id: TENANT_ID,
        // agent_id 与 team_id 互斥，后端二选一
        ...(agentId ? { agent_id: agentId } : { team_id: teamId }),
        channel: createChannel,
        name: createName.trim(),
      });
      notify.success('渠道接入创建成功');
      setCreateOpen(false);
      setCreateAgentId('');
      setCreateTeamId('');
      setCreateName('');
      await load();
      setSelectedId(created.id);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '创建渠道接入失败');
    } finally {
      setCreating(false);
    }
  }

  function openRename(item: ChannelBindingRead) {
    setRenameTargetId(item.id);
    setRenameValue(item.name?.trim() || channelName(item.channel));
    setRenameOpen(true);
  }

  async function confirmRename() {
    const target = bindings.find((item) => item.id === renameTargetId);
    if (!target || renaming) return;
    const name = renameValue.trim();
    if (!name) {
      notify.error('接入名称不能为空');
      return;
    }
    setRenaming(true);
    try {
      const updated = await api.put<ChannelBindingRead>(
        `/api/enterprise/channels/${target.id}?tenant_id=${TENANT_ID}`,
        { tenant_id: TENANT_ID, name },
      );
      setBindings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setRenameOpen(false);
      notify.success('重命名成功');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '重命名失败');
    } finally {
      setRenaming(false);
    }
  }

  async function openBindCode(targetUserId?: string) {
    if (bindCodeLoading) return;
    setBindCodeLoading(true);
    try {
      const target = targetUserId
        ? tenantUsers.find((user) => user.id === targetUserId)
        : currentUser;
      const result = targetUserId && binding
        ? await api.post<ChannelBindCodeRead>(
            `/api/enterprise/channels/${binding.id}/identity-bind-code?tenant_id=${TENANT_ID}`,
            { user_id: targetUserId },
          )
        : await api.post<ChannelBindCodeRead>(
            `/api/enterprise/channels/bind-code?tenant_id=${TENANT_ID}`,
          );
      setBindCode(result);
      setBindCodeTargetName(target?.display_name || target?.username || '当前用户');
      setBindCodeTargetUserId(targetUserId);
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
      await copyTextToClipboard(`/绑定 ${bindCode.code}`);
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

  async function toggleStatus() {
    if (!binding) return;
    setTogglingStatus(true);
    try {
      const updated = await api.post<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}/toggle-status?tenant_id=${TENANT_ID}`,
      );
      setBindings((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      notify.success(updated.status === 'active' ? '已启用渠道' : '已停用渠道');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '切换状态失败');
    } finally {
      setTogglingStatus(false);
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

  async function saveHandoffAssignee(value: string) {
    if (!binding || handoffAssigneeSaving) return;
    const { userId, channel } = parseHandoffAssigneeValue(value === '__none__' ? '' : value);
    setHandoffAssigneeSaving(true);
    try {
      const updated = await api.put<ChannelBindingRead>(
        `/api/enterprise/channels/${binding.id}?tenant_id=${TENANT_ID}`,
        {
          tenant_id: TENANT_ID,
          default_handoff_assignee_user_id: userId || null,
          default_handoff_assignee_channel: userId ? channel : null,
        },
      );
      setBindings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      notify.success('已保存');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存默认人工处理人失败');
    } finally {
      setHandoffAssigneeSaving(false);
    }
  }

  function metaFor(channel: string): ChannelMetaRead | undefined {
    return channelMetas.find((item) => item.channel === channel);
  }

  function channelName(channel: string): string {
    return getChannelPresentation(channel, metaFor(channel)?.name).name;
  }

  function bindingDisplayName(item: ChannelBindingRead): string {
    return item.name?.trim() || channelName(item.channel);
  }

  function channelTypeTag(item: ChannelBindingRead): string | null {
    const name = item.name?.trim();
    // 未命名或改回渠道类型名时不重复展示
    if (!name || name === channelName(item.channel)) return null;
    return channelName(item.channel);
  }

  function setupKindFor(channel: string): string {
    return metaFor(channel)?.setup || (channel === 'wechat' ? 'qrcode' : 'credentials');
  }

  function bindingStatusFor(item: ChannelBindingRead): { tone: BadgeTone; text: string } {
    if (item.status === 'pending' && setupKindFor(item.channel) !== 'qrcode') {
      return { tone: 'blue', text: '待配置' };
    }
    return BINDING_STATUS_BADGE[item.status] || {
      tone: 'gray',
      text: item.status,
    };
  }

  const bindingStatus = binding ? bindingStatusFor(binding) : undefined;
  const attentionBindings = bindings.filter(
    (item) => item.status === 'expired' || (item.status === 'active' && !item.connected),
  );
  const activeChannel = binding
    ? getChannelPresentation(binding.channel, metaFor(binding.channel)?.name)
    : null;
  // bot_id / ilink_bot_id 是 DTO 顶层字段(后端不回传 config_json)
  const botId = binding?.ilink_bot_id || binding?.bot_id || binding?.app_id || '';
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
      {attentionBindings.length > 0 && (
        <div className="flex flex-col gap-[6px] rounded-[12px] border border-[#f3d28b] bg-[#fff8e8] px-[18px] py-[12px] text-[#6f4500]">
          {attentionBindings.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setSelectedId(item.id)}
              className="flex items-center gap-[8px] text-left text-[13px] leading-[20px] transition-opacity hover:opacity-70"
            >
              <IconWarningFill className="size-[14px] shrink-0 text-[#f59e0b]" />
              <span>
                {bindingDisplayName(item)}：{attentionText(item)}
              </span>
            </button>
          ))}
        </div>
      )}
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
          <span>暂无渠道接入，接入后用户可通过斜杠指令在多个数字员工之间切换。</span>
          <UIButton onClick={openCreate} className={PRIMARY_BUTTON_CLASS}>
            接入渠道
          </UIButton>
        </div>
      ) : (
        <div className="grid gap-[12px]">
          {bindings.map((item) => {
            const status = bindingStatusFor(item);
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
                    {bindingDisplayName(item)}
                  </span>
                  {channelTypeTag(item) && (
                    <span className="rounded-[6px] bg-[#f0f1f5] px-[6px] py-[2px] text-[11px] text-[#858b9c]">
                      {channelTypeTag(item)}
                    </span>
                  )}
                  <StatusBadge tone={status?.tone || 'gray'}>
                    {status?.text || item.status}
                  </StatusBadge>
                  {item.status === 'active' && (
                    <span className="text-[12px] text-[#858b9c]">
                      {item.connected ? '已连接' : isSessionRecovering(item) ? '恢复中' : '未连接'}
                    </span>
                  )}
                  {canManageBinding(item) && (
                    <UIButton
                      variant="outline"
                      onClick={(event) => {
                        event.stopPropagation();
                        openRename(item);
                      }}
                      onKeyDown={(event) => event.stopPropagation()}
                      className="ml-auto h-[26px] gap-1 rounded-[8px] border-[#e3e7f1] px-[12px] text-[11px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]"
                    >
                      重命名
                    </UIButton>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-[10px] text-[12px] text-[#858b9c]">
                  <span>创建者：{item.created_by_name || '-'}</span>
                  <span>{formatTime(item.created_at)}</span>
                </div>
                <div className="flex flex-wrap items-center gap-[6px]">
                  <span className="text-[12px] text-[#858b9c]">
                    {item.team_id ? '绑定团队' : '可调度员工'}
                  </span>
                  {item.team_id ? (
                    <StatusBadge tone="blue">{`团队 · ${teamNameFor(item)}`}</StatusBadge>
                  ) : (item.agents || []).length === 0 ? (
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
              {bindingDisplayName(binding)}
            </span>
            {channelTypeTag(binding) && (
              <span className="rounded-[6px] bg-[#f0f1f5] px-[6px] py-[2px] text-[11px] text-[#858b9c]">
                {channelTypeTag(binding)}
              </span>
            )}
            <StatusBadge tone={bindingStatus?.tone || 'gray'}>
              {bindingStatus?.text || binding.status}
            </StatusBadge>
            {binding.status === 'active' && (
              <span className="text-[12px] text-[#858b9c]">
                {binding.connected ? '已连接' : isSessionRecovering(binding) ? '恢复中' : '未连接'}
              </span>
            )}
            {botId && (
              <span className="truncate text-[12px] text-[#858b9c]">
                {activeChannel?.identifierLabel}：{botId}
              </span>
            )}
            <span className="truncate text-[12px] text-[#858b9c]">
              创建者：{binding.created_by_name || '-'}
            </span>
            {binding.my_role && (
              <span className="rounded-[6px] bg-[#f0f1f5] px-[6px] py-[2px] text-[11px] text-[#858b9c]">
                {ROLE_LABEL[binding.my_role] || binding.my_role}
              </span>
            )}
          </div>
          <div className="flex items-center gap-[8px]">
            {canManageBinding(binding) && (
              <UIButton
                variant="outline"
                onClick={() => openRename(binding)}
                className={OUTLINE_BUTTON_CLASS}
              >
                重命名
              </UIButton>
            )}
            {canManageBinding(binding) && (
              <UIButton
                variant="outline"
                onClick={() => void toggleStatus()}
                disabled={togglingStatus}
                className={OUTLINE_BUTTON_CLASS}
              >
                {binding.status === 'active' ? '停用' : '启用'}
              </UIButton>
            )}
            {canDeleteBinding(binding) && (
              <UIButton
                variant="outline"
                onClick={() => setUnbindOpen(true)}
                className={OUTLINE_BUTTON_CLASS}
              >
                断开接入
              </UIButton>
            )}
          </div>
        </div>
        {binding.status === 'expired' && setupKindFor(binding.channel) !== 'qrcode' && (
          <span className="text-[12px] text-[#d20b0b]">当前未连接，请检查凭证或网络</span>
        )}
        {binding.channel === 'feishu' ? (
          <FeishuSetup
            key={binding.id}
            binding={binding}
            onChanged={(updated) =>
              setBindings((current) =>
                current.map((item) => (item.id === updated.id ? updated : item)),
              )
            }
          />
        ) : binding.channel === 'dingtalk' ? (
          <DingTalkSetup
            key={binding.id}
            binding={binding}
            onChanged={(updated) =>
              setBindings((current) => current.map((item) => (item.id === updated.id ? updated : item)))
            }
          />
        ) : setupKindFor(binding.channel) === 'credentials' ? (
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
        <div className="flex items-center justify-between gap-[12px] border-t border-[#eef0f4] pt-[16px]">
          <div className="flex min-w-0 flex-col gap-[4px]">
            <span className="text-[13px] font-semibold text-[#18181a]">默认人工处理人</span>
            <span className="text-[12px] leading-[1.6] text-[#858b9c]">
              SOP 人工节点未指定处理人时，转交给此用户；选择带渠道标注的选项会通过对应渠道转接（当前支持飞书/企业微信）。未配置时回退到数字员工负责人或管理员。
            </span>
          </div>
          <div className="flex items-center gap-[8px]">
            {binding.default_handoff_assignee_name && (
              <span className="text-[12px] text-[#858b9c]">
                当前：{binding.default_handoff_assignee_name}
                {`（${_CHANNEL_LABELS[binding.default_handoff_assignee_channel || 'web'] || _CHANNEL_LABELS.web}）`}
              </span>
            )}
            <Select
              value={
                binding.default_handoff_assignee_user_id
                  ? formatHandoffAssigneeValue(
                    binding.default_handoff_assignee_user_id,
                    binding.default_handoff_assignee_channel,
                  )
                  : '__none__'
              }
              disabled={handoffAssigneeSaving}
              onValueChange={(value) => void saveHandoffAssignee(value)}
            >
              <SelectTrigger className="h-[32px] w-[160px] text-[12px]">
                <SelectValue placeholder="选择处理人" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">未配置</SelectItem>
                {tenantUsers.filter((user) => !user.source || user.source === 'web').flatMap((user) => {
                  const name = user.display_name || user.username || user.id;
                  // 渠道转接通知运行时已支持飞书/企微私聊,渠道标注选项对支持
                  // 私聊通知的绑定渠道生成(后端同样拒绝其他渠道)。
                  const channelVariantAvailable = HANDOFF_NOTIFY_CHANNELS.has(binding.channel);
                  const scope = binding.identity_scope_key || '';
                  const matchingIdentity = user.channel_identities?.find(
                    (ci) => ci.channel === binding.channel && (ci.external_account_scope || '') === scope,
                  );
                  const items = [
                    <SelectItem key={user.id} value={user.id}>
                      {`${name}（${_CHANNEL_LABELS.web}）`}
                    </SelectItem>,
                  ];
                  const channelVariantConfigured = (
                    binding.default_handoff_assignee_user_id === user.id
                    && binding.default_handoff_assignee_channel === binding.channel
                  );
                  if (channelVariantAvailable && (matchingIdentity || channelVariantConfigured)) {
                    const channelLabel = _CHANNEL_LABELS[binding.channel] || binding.channel;
                    items.push(
                      <SelectItem key={`${user.id}::${binding.channel}`} value={`${user.id}::${binding.channel}`}>
                        {`${name}（${channelLabel}）`}
                      </SelectItem>,
                    );
                  }
                  return items;
                })}
              </SelectContent>
            </Select>
          </div>
        </div>
        {canDeleteBinding(binding) && (
          <BindingManagers
            bindingId={binding.id}
            users={tenantUsers}
            creatorUserId={binding.created_by_user_id}
          />
        )}
      </div>

      <section aria-label={activeChannel ? `${activeChannel.name}身份绑定` : '身份绑定'}>
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAccount className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">
            {activeChannel ? `${activeChannel.name}身份绑定` : '身份绑定'}
          </span>
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
          {canManageBinding(binding) && binding.channel === 'feishu' && (
            <div className="mt-[4px] flex flex-col gap-[10px] border-t border-[#eef0f4] pt-[12px]">
              <div className="flex flex-col gap-[3px]">
                <span className="text-[12px] font-medium text-[#18181a]">邀请成员绑定飞书身份</span>
                <span className="text-[11px] leading-[1.6] text-[#858b9c]">
                  每位成员需用自己的飞书账号向当前机器人发送一次性绑定指令。
                </span>
              </div>
              {identityBoundUsers.length > 0 && (
                <div className="flex flex-wrap gap-[6px]">
                  {identityBoundUsers.map((user) => (
                    <StatusBadge key={user.id} tone="green">
                      {`${user.display_name || user.username} 已绑定`}
                    </StatusBadge>
                  ))}
                </div>
              )}
              {identityUnboundUsers.length > 0 ? (
                <div className="flex flex-wrap items-center gap-[8px]">
                  <SearchableSelect
                    value={identityInviteUserId}
                    onValueChange={setIdentityInviteUserId}
                    options={identityUnboundUsers.map((user) => ({
                      value: user.id,
                      label: user.display_name || user.username || user.id,
                      keywords: [user.username],
                    }))}
                    placeholder="选择内部成员"
                    searchPlaceholder="搜索成员"
                    emptyText="无匹配成员"
                  />
                  <UIButton
                    variant="outline"
                    disabled={!identityInviteUserId || bindCodeLoading}
                    onClick={() => void openBindCode(identityInviteUserId)}
                    className={OUTLINE_BUTTON_CLASS}
                  >
                    生成绑定指令
                  </UIButton>
                </div>
              ) : (
                <span className="text-[11px] text-[#858b9c]">所有内部成员均已绑定当前飞书应用</span>
              )}
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
          {!agentEditing && !binding.team_id && (
            <UIButton variant="outline" onClick={openAgentEdit} className={OUTLINE_BUTTON_CLASS}>
              编辑
            </UIButton>
          )}
        </div>
        <p className="mb-[16px] px-[12px] text-[12px] text-[#858b9c]">
          挂载后，该渠道的所有用户均可与这些员工对话。
        </p>
        {binding.team_id ? (
          <div className="flex flex-wrap items-center gap-[8px] rounded-[14px] border border-[#eef0f4] p-[16px]">
            <span className="text-[13px] text-[#18181a]">
              {`团队：${teamNameFor(binding)}（项目领导：${teamLeaderName(binding.team_id)}）`}
            </span>
          </div>
        ) : agentEditing ? (
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

      <section aria-label={activeChannel ? `${activeChannel.name}指令说明` : '指令说明'}>
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconChat className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">
            {activeChannel ? `${activeChannel.name}指令说明` : '指令说明'}
          </span>
        </div>
        <div className="flex flex-col gap-[8px] rounded-[14px] border border-[#eef0f4] p-[16px]">
          {CHANNEL_COMMANDS.map((item) => (
            <div key={item.command} className="flex flex-wrap items-baseline gap-[8px] text-[12px]">
              <code className="rounded-[6px] bg-[#f2f3f7] px-[8px] py-[3px] text-[#18181a]">
                {item.command}
              </code>
              <span className="text-[#858b9c]">{item.description}</span>
            </div>
          ))}
        </div>
      </section>

      <section aria-label={activeChannel ? `${activeChannel.name}对话记录` : '对话记录'}>
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconChat className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">
            {activeChannel ? `${activeChannel.name}对话记录` : '对话记录'}
          </span>
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
                  const shown = messageDisplay(
                    msg,
                    activeConversation,
                    activeChannel?.userLabel || '用户',
                  );
                  return (
                    <div key={msg.id} className="flex flex-col gap-[4px]">
                      <span className="text-[11px] text-[#a0a6b8]">
                        {shown.label} · {formatTime(msg.created_at)}
                      </span>
                       <div className="wrap-break-word rounded-[10px] bg-[#f6f6f6] px-[12px] py-[8px] text-[13px] leading-[1.6] text-[#18181a]">
                         {shown.content}
                         {msg.attachments?.length ? (
                           <span className="mt-[8px] flex flex-col gap-[6px]">
                             {msg.attachments.map((attachment) => (
                               <ChannelAttachmentView
                                 key={attachment.id}
                                 attachment={attachment}
                                 bindingId={binding?.id || ''}
                                 sessionId={activeConversation.session_id}
                                 messageId={msg.id}
                               />
                             ))}
                           </span>
                         ) : null}
                       </div>
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

      <section aria-label={activeChannel ? `${activeChannel.name}投递日志` : '投递日志'}>
        <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
          <IconAlignJustify className="size-[14px] shrink-0" />
          <span className="text-[14px] font-normal leading-none">
            {activeChannel ? `${activeChannel.name}投递日志` : '投递日志'}
          </span>
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
            {createStep === 'channel'
              ? '选择渠道'
              : createStep === 'name'
                ? '命名接入'
                : '选择绑定对象'}
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
                        setCreateName(
                          defaultBindingName(
                            getChannelPresentation(meta.channel, meta.name).name,
                          ),
                        );
                        setCreateStep('name');
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          setCreateChannel(meta.channel);
                          setCreateName(
                            defaultBindingName(
                              getChannelPresentation(meta.channel, meta.name).name,
                            ),
                          );
                          setCreateStep('name');
                        }
                      }}
                      className="flex cursor-pointer flex-col gap-[6px] rounded-[14px] border border-[#eef0f4] p-[16px] transition-colors hover:border-[#cbd3e6]"
                    >
                      <div className="flex items-center gap-[8px]">
                        <span className="text-[13px] font-semibold text-[#18181a]">
                          {meta.name}
                        </span>
                      </div>
                      <span className="text-[12px] text-[#858b9c]">
                        {getChannelPresentation(meta.channel, meta.name).blurb}
                      </span>
                    </article>
                  ))}
                </div>
              )
            ) : createStep === 'name' ? (
              <div className="flex flex-col gap-[12px]">
                <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
                  接入名称
                  <Input
                    type="text"
                    value={createName}
                    maxLength={BINDING_NAME_MAX_LENGTH}
                    autoComplete="off"
                    data-1p-ignore="true"
                    data-lpignore="true"
                    placeholder="请输入接入名称"
                    onChange={(event) => setCreateName(event.target.value)}
                    className="h-8 rounded-[10px] text-[12px]"
                  />
                </label>
                <span className="text-[11px] leading-[1.6] text-[#858b9c]">
                  默认按渠道名与时间生成，可自行修改。
                </span>
              </div>
            ) : (
              <div className="flex flex-col gap-[12px]">
                <div className="flex rounded-[10px] bg-[#f2f3f7] p-[4px]">
                  {(
                    [
                      { key: 'agent', label: '数字员工' },
                      { key: 'team', label: '团队' },
                    ] as const
                  ).map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      onClick={() => setCreateTarget(option.key)}
                      className={cn(
                        'flex-1 rounded-[8px] py-[6px] text-[12px] transition-colors',
                        createTarget === option.key
                          ? 'bg-white font-medium text-[#18181a]'
                          : 'text-[#858b9c] hover:text-[#18181a]',
                      )}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                {createTarget === 'agent' ? (
                  candidatesLoading ? (
                    <div className="py-[24px] text-center text-[12px] text-[#858b9c]">加载中…</div>
                  ) : agentCandidates.length === 0 ? (
                    <div className="py-[24px] text-center text-[12px] text-[#858b9c]">
                      暂无可用员工
                    </div>
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
                          <span className="min-w-0 flex-1 truncate">
                            {employeeDisplayName(agent)}
                          </span>
                        </div>
                      ))}
                    </RadioGroup>
                  )
                ) : teamsLoading ? (
                  <div className="py-[24px] text-center text-[12px] text-[#858b9c]">加载中…</div>
                ) : teams.length === 0 ? (
                  <div className="flex flex-col items-center gap-[12px] py-[24px] text-[12px] text-[#858b9c]">
                    <span>暂无可用团队</span>
                    <UIButton
                      variant="outline"
                      onClick={() => {
                        setCreateOpen(false);
                        navigate('/enterprise/teams');
                      }}
                      className={OUTLINE_BUTTON_CLASS}
                    >
                      去创建团队
                    </UIButton>
                  </div>
                ) : (
                  <RadioGroup
                    value={createTeamId}
                    onValueChange={setCreateTeamId}
                    className="grid gap-[10px]"
                  >
                    {teams.map((team) => (
                      <div
                        key={team.id}
                        className="flex items-center gap-[8px] text-[13px] text-[#18181a]"
                      >
                        <RadioGroupItem value={team.id} />
                        <span className="min-w-0 flex-1 truncate">{team.name}</span>
                        <span className="shrink-0 text-[12px] text-[#858b9c]">
                          {`项目领导：${team.members.find((member) => member.role === 'leader')?.agent_name || '未设置'}`}
                        </span>
                        <span className="shrink-0 text-[12px] text-[#858b9c]">
                          {`${team.members.length} 名成员`}
                        </span>
                      </div>
                    ))}
                  </RadioGroup>
                )}
              </div>
            )}
          </div>
          <div className="flex justify-end gap-[8px]">
            {createStep !== 'channel' && (
              <UIButton
                variant="outline"
                onClick={() => setCreateStep(createStep === 'agent' ? 'name' : 'channel')}
                className={OUTLINE_BUTTON_CLASS}
              >
                {createStep === 'agent' ? '返回命名' : '返回选择渠道'}
              </UIButton>
            )}
            <UIButton
              variant="outline"
              onClick={() => setCreateOpen(false)}
              className={OUTLINE_BUTTON_CLASS}
            >
              取消
            </UIButton>
            {createStep === 'name' && (
              <UIButton
                onClick={() => setCreateStep('agent')}
                disabled={!createName.trim()}
                className={PRIMARY_BUTTON_CLASS}
              >
                下一步
              </UIButton>
            )}
            {createStep === 'agent' && (
              <UIButton
                onClick={() => void createBinding()}
                disabled={
                  (createTarget === 'agent' ? !createAgentId : !createTeamId) || creating
                }
                className={PRIMARY_BUTTON_CLASS}
              >
                {`创建${getChannelPresentation(createChannel, metaFor(createChannel)?.name).name}接入`}
              </UIButton>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[420px]"
        >
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            重命名接入
          </DialogTitle>
          <label className="flex flex-col gap-[6px] text-[12px] text-[#464c5e]">
            接入名称
            <Input
              type="text"
              value={renameValue}
              maxLength={BINDING_NAME_MAX_LENGTH}
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              placeholder="请输入接入名称"
              onChange={(event) => setRenameValue(event.target.value)}
              className="h-8 rounded-[10px] text-[12px]"
            />
          </label>
          <div className="flex justify-end gap-[8px]">
            <UIButton
              variant="outline"
              onClick={() => setRenameOpen(false)}
              className={OUTLINE_BUTTON_CLASS}
            >
              取消
            </UIButton>
            <UIButton
              onClick={() => void confirmRename()}
              disabled={!renameValue.trim() || renaming}
              className={PRIMARY_BUTTON_CLASS}
            >
              保存
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={bindCodeOpen}
        onOpenChange={(open) => {
          setBindCodeOpen(open);
          if (!open) {
            void loadIdentityBindings();
            void loadTenantUsers();
            void load();
          }
        }}
      >
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[420px]"
        >
          <DialogTitle className="text-[14px] font-normal leading-none text-[#757f9c]">
            {`绑定${bindCodeTargetName ? ` ${bindCodeTargetName} 的` : ''}${bindCodeChannelName}身份`}
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
                {`请让${bindCodeTargetName || '该成员'}使用自己的${bindCodeChannelName}账号向当前机器人发送以上指令。`}
              </span>
              {bindCodeRemain === 0 && (
                <UIButton
                  onClick={() => void openBindCode(bindCodeTargetUserId)}
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
        title={activeChannel ? `断开${activeChannel.name}接入？` : '断开渠道接入？'}
        description={activeChannel?.disconnectDescription || '断开后对话记录保留。确定断开接入吗？'}
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
