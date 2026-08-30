/**
 * 共享知识库历史面板：管理全局版本生命周期，并按来源与权限变化复盘只追加审计事件。
 */

import { useEffect, useMemo, useState } from 'react';

import { api, TENANT_ID } from '@/api/client';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
} from '@/components/ui';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { apiErrorCode, apiErrorMessage } from '@/lib/apiErrorMessages';
import type {
  KnowledgeBaseAuditEventRead,
  KnowledgeBaseAuditPageRead,
  KnowledgeBaseRead,
  KnowledgeBaseVersionRead,
} from '@/types';

type TeamOption = {
  id: string;
  name: string;
};

type SharedKnowledgeVersionsDialogProps = {
  open: boolean;
  knowledgeBase: KnowledgeBaseRead | null;
  teamOptions: TeamOption[];
  onClose: () => void;
  onChanged?: () => void | Promise<void>;
};

type HistoryView = 'versions' | 'audit';

type AuditFilters = {
  teamId: string;
  action: string;
  actorType: string;
  actorId: string;
  versionId: string;
};

const VERSION_STATE_LABELS: Record<'draft' | 'released' | 'rejected', string> = {
  draft: '草稿',
  released: '正式版本',
  rejected: '已驳回',
};

const AUDIT_ACTION_LABELS: Record<string, string> = {
  shared_created: '创建共享知识库',
  binding_created: '绑定团队',
  binding_revoked: '移除团队绑定',
  default_changed: '变更默认写入目标',
  grant_created: '新增权限',
  grant_changed: '变更权限',
  grant_revoked: '撤销权限',
  draft_created: '创建草稿',
  draft_updated: '更新草稿',
  version_published: '发布正式版本',
  draft_rejected: '驳回草稿',
  version_rolled_back: '回滚正式版本',
  dedicated_converted: '专用知识转为共享',
  conversion_failed: '转换失败',
};

const ACTOR_TYPE_LABELS: Record<string, string> = {
  user: '用户',
  agent: 'Agent',
  system: '系统',
};

const PERMISSION_LABELS: Record<string, string> = {
  none: '无权限',
  reader: '可读取',
  editor: '可编辑',
  publisher: '可发布',
};

const EMPTY_AUDIT_FILTERS: AuditFilters = {
  teamId: '',
  action: '',
  actorType: '',
  actorId: '',
  versionId: '',
};

const EMPTY_AUDIT_PAGE: KnowledgeBaseAuditPageRead = {
  items: [],
  total: 0,
  offset: 0,
  limit: 20,
  has_more: false,
};

function publicationState(version: KnowledgeBaseVersionRead) {
  /** 旧版历史缺省视为正式版本，仅供专用兼容数据安全展示。 */
  return version.publication_state || 'released';
}

function auditActionLabel(action: string) {
  /** 将稳定审计动作码映射为可读业务动作，未知码保留原值便于诊断。 */
  return AUDIT_ACTION_LABELS[action] || action;
}

function auditDetail(event: KnowledgeBaseAuditEventRead, key: string) {
  /** 安全读取审计详情中的标量来源字段；对象值不会直接渲染到页面。 */
  const value = event.details?.[key];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '';
}

function permissionTransition(event: KnowledgeBaseAuditEventRead) {
  /** 将权限变更前后状态投影为产品定义的四档权限文案。 */
  const previous = auditDetail(event, 'previous_permission') || 'none';
  const current = auditDetail(event, 'current_permission') || auditDetail(event, 'permission');
  if (!current) return '';
  return `${PERMISSION_LABELS[previous] || previous} → ${PERMISSION_LABELS[current] || current}`;
}

function versionTransition(
  event: KnowledgeBaseAuditEventRead,
  versionLabels: Map<string, string>,
) {
  /** 从事件详情和受影响版本拼出发布或回滚指针变化。 */
  const previousId = auditDetail(event, 'previous_version_id');
  const targetId = auditDetail(event, 'target_version_id')
    || auditDetail(event, 'published_version_id')
    || event.knowledge_base_version_id
    || '';
  if (!previousId || !targetId) return '';
  const previous = versionLabels.get(previousId) || previousId;
  const target = versionLabels.get(targetId) || event.knowledge_base_version || targetId;
  return `v${previous} → v${target}`;
}

function formatAuditTime(value: string) {
  /** 以本地短日期时间展示事件时间；无效值原样返回避免丢失证据。 */
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function buildAuditQuery(offset: number, filters: AuditFilters) {
  /** 只发送已填写的审计筛选，并固定每页二十条。 */
  const params = new URLSearchParams({
    tenant_id: TENANT_ID,
    offset: String(offset),
    limit: '20',
  });
  if (filters.teamId) params.set('team_id', filters.teamId);
  if (filters.action) params.set('action', filters.action);
  if (filters.actorType) params.set('actor_type', filters.actorType);
  if (filters.actorId.trim()) params.set('actor_id', filters.actorId.trim());
  if (filters.versionId) params.set('version_id', filters.versionId);
  return params.toString();
}

export function SharedKnowledgeVersionsDialog({
  open,
  knowledgeBase,
  teamOptions,
  onClose,
  onChanged,
}: SharedKnowledgeVersionsDialogProps) {
  /** 管理一个共享库的全局草稿、发布、驳回与回滚生命周期。 */
  const [versions, setVersions] = useState<KnowledgeBaseVersionRead[]>([]);
  const [selectedTeamId, setSelectedTeamId] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [activeView, setActiveView] = useState<HistoryView>('versions');
  const [auditPage, setAuditPage] = useState<KnowledgeBaseAuditPageRead>(EMPTY_AUDIT_PAGE);
  const [auditFilters, setAuditFilters] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditErrorMessage, setAuditErrorMessage] = useState('');

  const publishedHead = useMemo(
    () => versions.find((version) => version.is_published_head),
    [versions],
  );
  const versionLabels = useMemo(
    () => new Map(versions.map((version) => [version.id, version.version])),
    [versions],
  );
  const auditTeamOptions = useMemo(() => {
    const options = new Map(teamOptions.map((team) => [team.id, team.name]));
    auditPage.items.forEach((event) => {
      if (event.team_id) options.set(event.team_id, event.team_name || event.team_id);
    });
    return Array.from(options, ([id, name]) => ({ id, name }));
  }, [auditPage.items, teamOptions]);
  const auditActionOptions = useMemo(() => {
    const actions = new Set(Object.keys(AUDIT_ACTION_LABELS));
    auditPage.items.forEach((event) => actions.add(event.action));
    return Array.from(actions);
  }, [auditPage.items]);

  useEffect(() => {
    if (!open || !knowledgeBase) return;
    setSelectedTeamId((current) => (
      teamOptions.some((team) => team.id === current) ? current : teamOptions[0]?.id || ''
    ));
    setReason('');
    setErrorMessage('');
    setActiveView('versions');
    setAuditPage(EMPTY_AUDIT_PAGE);
    setAuditFilters(EMPTY_AUDIT_FILTERS);
    setAuditErrorMessage('');
    void loadVersions();
  }, [open, knowledgeBase?.id, teamOptions.map((team) => team.id).join('|')]);

  useEffect(() => {
    if (!open || !knowledgeBase || activeView !== 'audit') return;
    void loadAuditEvents(0, auditFilters, false);
  }, [
    open,
    knowledgeBase?.id,
    activeView,
    auditFilters.teamId,
    auditFilters.action,
    auditFilters.actorType,
    auditFilters.actorId,
    auditFilters.versionId,
  ]);

  async function loadVersions() {
    /** 重新读取服务端正式指针和历史，避免继续使用冲突前的本地状态。 */
    if (!knowledgeBase) return;
    setLoading(true);
    try {
      const rows = await api.get<KnowledgeBaseVersionRead[]>(
        `/api/enterprise/knowledge-bases/${knowledgeBase.id}/versions?tenant_id=${TENANT_ID}`,
      );
      setVersions(rows);
    } catch (error) {
      const message = apiErrorMessage(error, '加载共享版本失败');
      setErrorMessage(message);
      notify.error(message);
    } finally {
      setLoading(false);
    }
  }

  async function loadAuditEvents(
    offset: number,
    filters: AuditFilters,
    append: boolean,
  ) {
    /** 按当前筛选读取审计页；追加模式用于加载更多且保留既有证据顺序。 */
    if (!knowledgeBase) return;
    setAuditLoading(true);
    setAuditErrorMessage('');
    try {
      const page = await api.get<KnowledgeBaseAuditPageRead>(
        `/api/enterprise/knowledge-bases/${knowledgeBase.id}/audit-events?${buildAuditQuery(offset, filters)}`,
      );
      setAuditPage((current) => (
        append
          ? { ...page, items: [...current.items, ...page.items] }
          : page
      ));
    } catch (error) {
      const message = apiErrorMessage(error, '加载审计历史失败');
      setAuditErrorMessage(message);
      notify.error(message);
    } finally {
      setAuditLoading(false);
    }
  }

  function updateAuditFilter(key: keyof AuditFilters, value: string) {
    /** 更新一个筛选维度；筛选 effect 会从第一页重新读取服务端结果。 */
    setAuditFilters((current) => ({ ...current, [key]: value }));
  }

  function loadMoreAuditEvents() {
    /** 从当前已展示数量继续读取下一页，不重复首屏事件。 */
    if (auditLoading || !auditPage.has_more) return;
    void loadAuditEvents(auditPage.items.length, auditFilters, true);
  }

  async function runMutation(
    action: () => Promise<unknown>,
    successMessage: string,
  ) {
    /** 串行执行生命周期动作；冲突时强制刷新正式指针后再让用户确认。 */
    if (!knowledgeBase || !selectedTeamId || !reason.trim()) return;
    setActing(true);
    setErrorMessage('');
    try {
      await action();
      notify.success(successMessage);
      setReason('');
      await loadVersions();
      await onChanged?.();
    } catch (error) {
      const message = apiErrorMessage(error, '共享知识库版本操作失败');
      setErrorMessage(message);
      notify.error(message);
      if (apiErrorCode(error) === 'KNOWLEDGE_PUBLISH_CONFLICT') {
        await loadVersions();
      }
    } finally {
      setActing(false);
    }
  }

  function createDraft() {
    /** 从当前全局正式版本创建团队来源明确的新草稿。 */
    if (!knowledgeBase) return;
    const expectedHead = publishedHead?.id || knowledgeBase.published_version_id;
    void runMutation(
      () => api.post(`/api/enterprise/knowledge-bases/${knowledgeBase.id}/drafts`, {
        tenant_id: TENANT_ID,
        team_id: selectedTeamId,
        change_reason: reason.trim(),
        expected_published_version_id: expectedHead,
      }),
      '共享知识草稿已创建',
    );
  }

  function publishDraft(version: KnowledgeBaseVersionRead) {
    /** 以当前正式指针作为 CAS 预期值发布所选草稿。 */
    if (!knowledgeBase || !publishedHead) return;
    void runMutation(
      () => api.post(
        `/api/enterprise/knowledge-bases/${knowledgeBase.id}/versions/${version.id}/publish`,
        {
          tenant_id: TENANT_ID,
          team_id: selectedTeamId,
          expected_published_version_id: publishedHead.id,
          change_reason: reason.trim(),
        },
      ),
      `已发布 ${version.version}`,
    );
  }

  function rejectDraft(version: KnowledgeBaseVersionRead) {
    /** 驳回草稿并保留历史快照。 */
    if (!knowledgeBase) return;
    void runMutation(
      () => api.post(
        `/api/enterprise/knowledge-bases/${knowledgeBase.id}/versions/${version.id}/reject`,
        {
          tenant_id: TENANT_ID,
          team_id: selectedTeamId,
          change_reason: reason.trim(),
        },
      ),
      `已驳回 ${version.version}`,
    );
  }

  function rollbackTo(version: KnowledgeBaseVersionRead) {
    /** 只移动正式指针到历史发布快照，不删除任何后来版本。 */
    if (!knowledgeBase || !publishedHead) return;
    void runMutation(
      () => api.post(`/api/enterprise/knowledge-bases/${knowledgeBase.id}/rollback`, {
        tenant_id: TENANT_ID,
        team_id: selectedTeamId,
        target_version_id: version.id,
        expected_published_version_id: publishedHead.id,
        change_reason: reason.trim(),
      }),
      `已回滚到 ${version.version}`,
    );
  }

  const canMutate = Boolean(selectedTeamId && reason.trim() && !acting);

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-h-[86vh] max-w-[920px] overflow-y-auto">
        <DialogTitle>共享版本：{knowledgeBase?.name || ''}</DialogTitle>
        <div className="flex flex-col gap-4">
          <p className="m-0 text-sm leading-6 text-muted-foreground">
            所有绑定团队共用一个最新正式版本；草稿在发布前不会影响团队读取。
          </p>

          <div role="tablist" aria-label="共享知识库历史视图" className="flex gap-1 rounded-lg bg-muted p-1">
            <Button
              role="tab"
              aria-selected={activeView === 'versions'}
              variant={activeView === 'versions' ? 'default' : 'ghost'}
              className="flex-1"
              onClick={() => setActiveView('versions')}
            >
              版本历史
            </Button>
            <Button
              role="tab"
              aria-selected={activeView === 'audit'}
              variant={activeView === 'audit' ? 'default' : 'ghost'}
              className="flex-1"
              onClick={() => setActiveView('audit')}
            >
              审计历史
            </Button>
          </div>

          {activeView === 'versions' ? (
            <>
              <div className="grid gap-3 rounded-xl border bg-muted/25 p-4 md:grid-cols-[220px_minmax(0,1fr)_auto]">
            <label className="flex flex-col gap-2 text-sm font-medium">
              操作团队
              <select
                aria-label="操作团队"
                className="h-9 rounded-md border bg-background px-3 text-sm"
                value={selectedTeamId}
                onChange={(event) => setSelectedTeamId(event.target.value)}
              >
                {teamOptions.map((team) => (
                  <option key={team.id} value={team.id}>{team.name}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-2 text-sm font-medium">
              变更原因
              <Input
                aria-label="变更原因"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="说明本次新增、发布、驳回或回滚原因"
              />
            </label>
            <Button className="self-end" disabled={!canMutate} onClick={createDraft}>
              创建草稿
            </Button>
              </div>

              {teamOptions.length === 0 ? (
                <div role="alert" className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
                  当前账号没有可管理的团队绑定，请先在团队详情中绑定此共享知识库。
                </div>
              ) : null}
              {errorMessage ? (
                <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                  {errorMessage}
                </div>
              ) : null}

              <div aria-label="共享版本历史" className="flex flex-col gap-2">
            {loading ? <p className="text-sm text-muted-foreground">正在加载版本…</p> : null}
            {!loading && versions.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无版本记录</p>
            ) : null}
            {versions.map((version) => (
              <article
                key={version.id}
                className="grid gap-3 rounded-xl border p-4 md:grid-cols-[minmax(0,1fr)_auto]"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-sm">v{version.version}</strong>
                    <span className="rounded-full bg-muted px-2 py-1 text-xs">
                      {VERSION_STATE_LABELS[publicationState(version)]}
                    </span>
                    {version.is_published_head ? (
                      <span className="rounded-full bg-emerald-100 px-2 py-1 text-xs text-emerald-700">
                        当前正式
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-2 mb-0 text-sm text-muted-foreground">
                    {version.change_reason || '未填写变更原因'}
                  </p>
                  <small className="mt-1 block text-xs text-muted-foreground">
                    来源团队：{teamOptions.find((team) => team.id === version.source_team_id)?.name || version.source_team_id || '-'}
                  </small>
                </div>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  {publicationState(version) === 'draft' ? (
                    <>
                      <Button
                        size="sm"
                        disabled={!canMutate || !publishedHead}
                        onClick={() => publishDraft(version)}
                        aria-label={`发布 ${version.version}`}
                      >
                        发布
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!canMutate}
                        onClick={() => rejectDraft(version)}
                        aria-label={`驳回 ${version.version}`}
                      >
                        驳回
                      </Button>
                    </>
                  ) : null}
                  {publicationState(version) === 'released' && !version.is_published_head ? (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!canMutate || !publishedHead}
                      onClick={() => rollbackTo(version)}
                      aria-label={`回滚到 ${version.version}`}
                    >
                      回滚
                    </Button>
                  ) : null}
                </div>
              </article>
            ))}
              </div>
            </>
          ) : (
            <div aria-label="共享知识库审计历史" className="flex flex-col gap-4">
              <div className="grid gap-3 rounded-xl border bg-muted/25 p-4 md:grid-cols-3">
                <label className="flex flex-col gap-2 text-sm font-medium">
                  审计动作
                  <select
                    aria-label="审计动作"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={auditFilters.action}
                    onChange={(event) => updateAuditFilter('action', event.target.value)}
                  >
                    <option value="">全部动作</option>
                    {auditActionOptions.map((action) => (
                      <option key={action} value={action}>{auditActionLabel(action)}</option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-2 text-sm font-medium">
                  操作者类型
                  <select
                    aria-label="操作者类型"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={auditFilters.actorType}
                    onChange={(event) => updateAuditFilter('actorType', event.target.value)}
                  >
                    <option value="">全部类型</option>
                    <option value="user">用户</option>
                    <option value="agent">Agent</option>
                    <option value="system">系统</option>
                  </select>
                </label>
                <label className="flex flex-col gap-2 text-sm font-medium">
                  审计团队
                  <select
                    aria-label="审计团队"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={auditFilters.teamId}
                    onChange={(event) => updateAuditFilter('teamId', event.target.value)}
                  >
                    <option value="">全部团队</option>
                    {auditTeamOptions.map((team) => (
                      <option key={team.id} value={team.id}>{team.name}</option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-2 text-sm font-medium">
                  审计版本
                  <select
                    aria-label="审计版本"
                    className="h-9 rounded-md border bg-background px-3 text-sm"
                    value={auditFilters.versionId}
                    onChange={(event) => updateAuditFilter('versionId', event.target.value)}
                  >
                    <option value="">全部版本</option>
                    {versions.map((version) => (
                      <option key={version.id} value={version.id}>v{version.version}</option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-2 text-sm font-medium md:col-span-2">
                  操作者 ID
                  <Input
                    aria-label="操作者 ID"
                    value={auditFilters.actorId}
                    onChange={(event) => updateAuditFilter('actorId', event.target.value)}
                    placeholder="按 Agent 或用户 ID 精确筛选"
                  />
                </label>
              </div>

              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>共 {auditPage.total} 条只追加事件</span>
                <span>事件不会因回滚或后续发布而删除</span>
              </div>
              {auditErrorMessage ? (
                <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">
                  {auditErrorMessage}
                </div>
              ) : null}
              {auditLoading && auditPage.items.length === 0 ? (
                <p role="status" className="text-sm text-muted-foreground">正在加载审计历史…</p>
              ) : null}
              {!auditLoading && auditPage.items.length === 0 ? (
                <p className="text-sm text-muted-foreground">当前筛选下暂无审计事件</p>
              ) : null}

              <div className="flex flex-col gap-2">
                {auditPage.items.map((event) => {
                  const pointerTransition = versionTransition(event, versionLabels);
                  const grantTransition = event.action.startsWith('grant_')
                    ? permissionTransition(event)
                    : '';
                  const sourceTaskId = auditDetail(event, 'source_task_id');
                  const sourceConversationId = auditDetail(event, 'source_conversation_id')
                    || auditDetail(event, 'conversation_id');
                  return (
                    <article key={event.id} className="rounded-xl border p-4">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <strong className="text-sm text-foreground">
                            {auditActionLabel(event.action)}
                          </strong>
                          <p className="mt-1 mb-0 text-sm text-muted-foreground">
                            {event.reason || '未填写变更原因'}
                          </p>
                        </div>
                        <time className="text-xs text-muted-foreground" dateTime={event.created_at}>
                          {formatAuditTime(event.created_at)}
                        </time>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                        <span className="rounded-full bg-muted px-2 py-1">
                          {event.actor_name} · {ACTOR_TYPE_LABELS[event.actor_type] || event.actor_type}
                        </span>
                        {event.team_id ? (
                          <span className="rounded-full bg-muted px-2 py-1">
                            {event.team_name || event.team_id}
                          </span>
                        ) : null}
                        {event.knowledge_base_version ? (
                          <span className="rounded-full bg-muted px-2 py-1">
                            v{event.knowledge_base_version}
                          </span>
                        ) : null}
                      </div>
                      {pointerTransition ? (
                        <p className="mt-3 mb-0 text-sm font-medium text-foreground">{pointerTransition}</p>
                      ) : null}
                      {grantTransition ? (
                        <p className="mt-3 mb-0 text-sm font-medium text-foreground">{grantTransition}</p>
                      ) : null}
                      {sourceTaskId ? (
                        <small className="mt-2 block text-xs text-muted-foreground">
                          来源任务：{sourceTaskId}
                        </small>
                      ) : null}
                      {sourceConversationId ? (
                        <small className="mt-1 block text-xs text-muted-foreground">
                          来源会话：{sourceConversationId}
                        </small>
                      ) : null}
                    </article>
                  );
                })}
              </div>
              {auditPage.has_more ? (
                <Button
                  variant="outline"
                  disabled={auditLoading}
                  onClick={loadMoreAuditEvents}
                >
                  {auditLoading ? '正在加载…' : '加载更多审计事件'}
                </Button>
              ) : null}
            </div>
          )}

          <div className="flex justify-end">
            <Button variant="outline" onClick={onClose}>关闭</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
