/**
 * 专用知识库转换向导：读取员工分支版本与可选团队，提交原子转换，并清楚展示来源保护边界。
 */

import { useEffect, useMemo, useState } from 'react';

import { api, TENANT_ID } from '@/api/client';
import {
  Checkbox,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { apiErrorMessage } from '@/lib/apiErrorMessages';
import type {
  KnowledgeBaseConversionRead,
  KnowledgeBaseRead,
  KnowledgeBaseVersionRead,
  TeamRead,
} from '@/types';

type SharedKnowledgeConversionDialogProps = {
  open: boolean;
  knowledgeBase: KnowledgeBaseRead | null;
  agentId: string;
  onClose: () => void;
  onConverted: (result: KnowledgeBaseConversionRead) => void | Promise<void>;
};

export function SharedKnowledgeConversionDialog({
  open,
  knowledgeBase,
  agentId,
  onClose,
  onConverted,
}: SharedKnowledgeConversionDialogProps) {
  /** 管理一次员工专用分支到新共享谱系的预览、配置和提交。 */
  const [versions, setVersions] = useState<KnowledgeBaseVersionRead[]>([]);
  const [teams, setTeams] = useState<TeamRead[]>([]);
  const [sourceVersionId, setSourceVersionId] = useState('');
  const [sharedName, setSharedName] = useState('');
  const [sharedDescription, setSharedDescription] = useState('');
  const [changeReason, setChangeReason] = useState('');
  const [selectedTeamIds, setSelectedTeamIds] = useState<string[]>([]);
  const [defaultTeamId, setDefaultTeamId] = useState('');
  const [contextLoading, setContextLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState('');
  const [conversionResult, setConversionResult] = useState<KnowledgeBaseConversionRead | null>(null);

  const selectedVersion = useMemo(
    () => versions.find((version) => version.id === sourceVersionId) || null,
    [sourceVersionId, versions],
  );
  const selectedTeams = useMemo(
    () => teams.filter((team) => selectedTeamIds.includes(team.id)),
    [selectedTeamIds, teams],
  );
  const canSubmit = Boolean(
    knowledgeBase
    && agentId
    && sourceVersionId
    && sharedName.trim()
    && changeReason.trim()
    && !contextLoading
    && !submitting
    && !conversionResult,
  );

  useEffect(() => {
    if (!open || !knowledgeBase) return;
    setVersions([]);
    setTeams([]);
    setSourceVersionId('');
    setSharedName(`${knowledgeBase.name}（共享）`);
    setSharedDescription(knowledgeBase.description || '');
    setChangeReason('');
    setSelectedTeamIds([]);
    setDefaultTeamId('');
    setErrorMessage('');
    setConversionResult(null);
    void loadConversionContext();
  }, [open, knowledgeBase?.id, agentId]);

  async function loadConversionContext() {
    /** 读取可转换版本和同租户活动团队；只更新向导本地状态，不改动知识数据。 */
    if (!knowledgeBase || !agentId) return;
    setContextLoading(true);
    setErrorMessage('');
    try {
      // 版本和团队彼此独立，并行加载后再共同决定默认选项。
      const [versionRows, teamRows] = await Promise.all([
        api.get<KnowledgeBaseVersionRead[]>(
          `/api/enterprise/knowledge-bases/${knowledgeBase.id}/versions?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(agentId)}`,
        ),
        api.get<TeamRead[]>(`/api/enterprise/teams?tenant_id=${TENANT_ID}`),
      ]);
      const defaultVersion = versionRows.find((version) => version.is_head)
        || versionRows.find((version) => version.version === knowledgeBase.branch_head_version)
        || versionRows[0];
      setVersions(versionRows);
      setTeams(teamRows.filter((team) => team.status === 'active'));
      setSourceVersionId(defaultVersion?.id || '');
      if (!defaultVersion) {
        setErrorMessage('当前员工分支没有可转换的版本，来源知识库未发生任何变化。');
      }
    } catch (error) {
      const message = apiErrorMessage(error, '加载转换信息失败');
      setErrorMessage(`${message}。来源专用知识库未发生任何变化。`);
      notify.error(message);
    } finally {
      setContextLoading(false);
    }
  }

  function toggleTeam(teamId: string, checked: boolean) {
    /** 增删初始团队绑定；移除默认团队时同步清空默认写入目标。 */
    setSelectedTeamIds((current) => (
      checked
        ? [...current, teamId]
        : current.filter((currentTeamId) => currentTeamId !== teamId)
    ));
    if (!checked && defaultTeamId === teamId) setDefaultTeamId('');
  }

  async function submitConversion() {
    /** 提交原子转换；仅 API 明确成功后才通知页面替换来源实例。 */
    if (!knowledgeBase || !canSubmit) return;
    setSubmitting(true);
    setErrorMessage('');

    let result: KnowledgeBaseConversionRead;
    try {
      // 服务端先复制与校验，成功响应代表新共享正式版可见且来源实例已归档。
      result = await api.post<KnowledgeBaseConversionRead>(
        `/api/enterprise/knowledge-bases/${knowledgeBase.id}/convert-to-shared`,
        {
          tenant_id: TENANT_ID,
          agent_id: agentId,
          source_version_id: sourceVersionId,
          name: sharedName.trim(),
          description: sharedDescription.trim() || null,
          change_reason: changeReason.trim(),
          team_bindings: selectedTeamIds,
          default_for_team_id: defaultTeamId || null,
        },
      );
    } catch (error) {
      const message = apiErrorMessage(error, '转换失败');
      setErrorMessage(
        `${message}。转换失败，来源专用知识库仍保持可用，尚未归档；不会显示未完成的共享知识库。`,
      );
      notify.error(message);
      setSubmitting(false);
      return;
    }

    // 页面回调只负责定位新共享库；即使刷新失败，也不能把已完成的转换误报为失败。
    setConversionResult(result);
    notify.success(`已创建共享知识库「${result.new_knowledge_base.name}」`);
    try {
      await onConverted(result);
    } catch {
      notify.warning('转换已成功，但列表刷新失败，请手动刷新后查看共享知识库。');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent className="max-h-[88vh] max-w-[780px] overflow-y-auto p-0">
        <div className="border-b border-[#eef0f4] px-6 py-5">
          <DialogTitle>转换为共享知识库：{knowledgeBase?.name || ''}</DialogTitle>
          <p className="mt-2 mb-0 text-sm leading-6 text-[#757f9c]">
            系统会先复制并校验所选版本，全部通过后才创建全局唯一正式版。
          </p>
        </div>

        <div className="flex flex-col gap-5 px-6 pb-6">
          <section aria-label="转换来源预览" className="rounded-xl border border-[#e7eaf1] bg-[#fafbfc] p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="m-0 text-xs font-medium tracking-wide text-[#858b9c]">来源专用实例</p>
                <strong className="mt-1 block text-sm text-[#18181a]">{knowledgeBase?.name || '-'}</strong>
              </div>
              <span className="rounded-full bg-[#eef1fb] px-3 py-1 text-xs font-medium text-[#5f73b7]">
                员工分支：{agentId || '-'}
              </span>
            </div>
            <div className="mt-4 grid grid-cols-3 gap-2">
              <div className="rounded-lg bg-white p-3 ring-1 ring-[#eef0f4]">
                <strong className="block text-base text-[#18181a]">{knowledgeBase?.document_count || 0}</strong>
                <span className="text-xs text-[#858b9c]">个文档</span>
              </div>
              <div className="rounded-lg bg-white p-3 ring-1 ring-[#eef0f4]">
                <strong className="block text-base text-[#18181a]">{knowledgeBase?.bucket_count || 0}</strong>
                <span className="text-xs text-[#858b9c]">个知识节点</span>
              </div>
              <div className="rounded-lg bg-white p-3 ring-1 ring-[#eef0f4]">
                <strong className="block text-base text-[#18181a]">{knowledgeBase?.chunk_count || 0}</strong>
                <span className="text-xs text-[#858b9c]">个引用片段</span>
              </div>
            </div>
            <p className="mt-4 mb-0 text-xs leading-5 text-[#5d6475]">
              转换成功后只归档当前员工的专用实例，不删除历史数据，也不影响其他员工的独立分支。
            </p>
          </section>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="flex flex-col gap-2 text-sm font-medium text-[#464c5e]">
              来源版本
              <select
                aria-label="来源版本"
                className="h-10 rounded-lg border border-[#dfe3ec] bg-white px-3 text-sm outline-none focus:border-[#879ee0]"
                value={sourceVersionId}
                disabled={contextLoading || submitting}
                onChange={(event) => setSourceVersionId(event.target.value)}
              >
                {versions.map((version) => (
                  <option key={version.id} value={version.id}>
                    v{version.version}{version.is_head ? ' · 当前分支 Head' : ''}
                  </option>
                ))}
              </select>
              {selectedVersion ? (
                <span className="text-xs font-normal text-[#858b9c]">
                  更新时间：{selectedVersion.updated_at.slice(0, 10)}
                </span>
              ) : null}
            </label>
            <label className="flex flex-col gap-2 text-sm font-medium text-[#464c5e]">
              共享知识库名称
              <Input
                aria-label="共享知识库名称"
                value={sharedName}
                disabled={submitting}
                onChange={(event) => setSharedName(event.target.value)}
              />
            </label>
          </div>

          <label className="flex flex-col gap-2 text-sm font-medium text-[#464c5e]">
            共享知识库描述
            <Textarea
              aria-label="共享知识库描述"
              value={sharedDescription}
              disabled={submitting}
              onChange={(event) => setSharedDescription(event.target.value)}
              placeholder="说明团队将如何使用这份共享知识"
            />
          </label>

          <label className="flex flex-col gap-2 text-sm font-medium text-[#464c5e]">
            转换原因
            <Textarea
              aria-label="转换原因"
              value={changeReason}
              disabled={submitting}
              onChange={(event) => setChangeReason(event.target.value)}
              placeholder="记录为什么把这份专用知识转为组织共享资产"
            />
          </label>

          <section aria-label="初始团队绑定" className="rounded-xl border border-[#e7eaf1] p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="m-0 text-sm font-medium text-[#18181a]">初始团队绑定</h3>
                <p className="mt-1 mb-0 text-xs text-[#858b9c]">可先不绑定，后续仍可在团队详情中分配。</p>
              </div>
              <span className="text-xs text-[#858b9c]">已选 {selectedTeamIds.length} 个团队</span>
            </div>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {teams.map((team) => (
                <label
                  key={team.id}
                  className="flex cursor-pointer items-center gap-3 rounded-lg border border-[#eef0f4] px-3 py-2.5 text-sm text-[#464c5e] hover:bg-[#fafbfc]"
                >
                  <Checkbox
                    aria-label={`绑定 ${team.name}`}
                    checked={selectedTeamIds.includes(team.id)}
                    disabled={submitting}
                    onCheckedChange={(checked) => toggleTeam(team.id, checked === true)}
                  />
                  <span>{team.name}</span>
                </label>
              ))}
              {!contextLoading && teams.length === 0 ? (
                <p className="col-span-full m-0 text-sm text-[#858b9c]">当前没有可绑定的活动团队。</p>
              ) : null}
            </div>
            <label className="mt-4 flex flex-col gap-2 text-sm font-medium text-[#464c5e]">
              默认写入团队
              <select
                aria-label="默认写入团队"
                className="h-10 rounded-lg border border-[#dfe3ec] bg-white px-3 text-sm outline-none focus:border-[#879ee0]"
                value={defaultTeamId}
                disabled={selectedTeams.length === 0 || submitting}
                onChange={(event) => setDefaultTeamId(event.target.value)}
              >
                <option value="">暂不设置默认团队</option>
                {selectedTeams.map((team) => (
                  <option key={team.id} value={team.id}>{team.name}</option>
                ))}
              </select>
            </label>
          </section>

          {contextLoading ? (
            <p role="status" className="m-0 text-sm text-[#757f9c]">正在读取来源版本与团队…</p>
          ) : null}
          {submitting ? (
            <p role="status" className="m-0 rounded-lg bg-[#f4f6fb] px-3 py-2 text-sm text-[#5f6d91]">
              正在复制、校验并创建首个正式版本，请勿关闭窗口…
            </p>
          ) : null}
          {errorMessage ? (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm leading-6 text-red-700">
              {errorMessage}
            </div>
          ) : null}
          {conversionResult ? (
            <div role="status" className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm leading-6 text-emerald-800">
              转换成功：已创建「{conversionResult.new_knowledge_base.name}」v{conversionResult.released_version.version}，
              来源专用实例已安全归档。
            </div>
          ) : null}

          <div className="flex justify-end gap-2 border-t border-[#eef0f4] pt-4">
            <Button variant="outline" disabled={submitting} onClick={onClose}>取消</Button>
            <Button disabled={!canSubmit} onClick={() => void submitConversion()}>
              {submitting ? '正在转换…' : '确认转换'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
