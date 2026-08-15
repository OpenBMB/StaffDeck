import { useCallback, useEffect, useMemo, useState } from 'react';
import { Button, notify } from '@/components/ui';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import { api, ApiError, TENANT_ID } from '@/api/client';
import { useI18n } from '@/i18n';

type EvolutionProposal = {
  id: string;
  resource_type: 'sop' | 'general_skill';
  resource_name: string;
  resource_key: string;
  base_version?: string | null;
  status: string;
  risk_level: string;
  hypothesis: string;
  rationale: string;
  expected_outcome: string;
  source_feedback_ids: string[];
  evidence: Array<Record<string, unknown>>;
  diff: Array<{ op?: string; path?: string; before?: unknown; after?: unknown }>;
  evaluation: Record<string, unknown>;
  created_at: string;
};

const STATUS_LABELS: Record<string, string> = {
  ready_for_review: '待审核',
  evaluation_failed: '校验未通过',
  published: '已批准',
  rejected: '已拒绝',
  rolled_back: '已回滚',
};

const RISK_LABELS: Record<string, string> = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
};

const EVOLUTION_ERROR_MESSAGES: Record<string, string> = {
  EVOLUTION_FEEDBACK_NOT_FOUND: '未找到可用于改进的 Skill 或 SOP 反馈',
  EVOLUTION_SOP_FEEDBACK_NOT_FOUND: '未找到与该 SOP 匹配的反馈',
  EVOLUTION_PROPOSAL_NOT_FOUND: '未找到自进化候选',
  EVOLUTION_PROPOSAL_NOT_REVIEWABLE: '当前自进化候选不可审核',
  EVOLUTION_PROPOSAL_VALIDATION_FAILED: '自进化候选未通过校验',
  EVOLUTION_SOP_NOT_FOUND: '未找到对应的 SOP',
  EVOLUTION_GENERAL_SKILL_NOT_FOUND: '未找到对应的通用技能',
  EVOLUTION_PUBLISHED_PROPOSAL_REQUIRES_ROLLBACK: '已应用的自进化候选只能通过回滚撤销',
  EVOLUTION_ROLLBACK_UNAVAILABLE: '该候选没有可回滚的已应用版本',
  EVOLUTION_MODEL_NOT_CONFIGURED: '没有可用于自进化的默认模型',
};

const LEGACY_EVOLUTION_ERRORS: Record<string, string> = {
  'No evolvable Skill or SOP feedback was found': EVOLUTION_ERROR_MESSAGES.EVOLUTION_FEEDBACK_NOT_FOUND,
  'No matching SOP feedback was found': EVOLUTION_ERROR_MESSAGES.EVOLUTION_SOP_FEEDBACK_NOT_FOUND,
  'Evolution proposal not found': EVOLUTION_ERROR_MESSAGES.EVOLUTION_PROPOSAL_NOT_FOUND,
  'Evolution proposal is not reviewable': EVOLUTION_ERROR_MESSAGES.EVOLUTION_PROPOSAL_NOT_REVIEWABLE,
  'Evolution proposal did not pass validation': EVOLUTION_ERROR_MESSAGES.EVOLUTION_PROPOSAL_VALIDATION_FAILED,
  'SOP not found': EVOLUTION_ERROR_MESSAGES.EVOLUTION_SOP_NOT_FOUND,
  'General skill not found': EVOLUTION_ERROR_MESSAGES.EVOLUTION_GENERAL_SKILL_NOT_FOUND,
  'Published proposal must be rolled back': EVOLUTION_ERROR_MESSAGES.EVOLUTION_PUBLISHED_PROPOSAL_REQUIRES_ROLLBACK,
  'Proposal has no published version to roll back': EVOLUTION_ERROR_MESSAGES.EVOLUTION_ROLLBACK_UNAVAILABLE,
};

export default function EvolutionPanel({ agentId }: { agentId: string }) {
  const { t } = useI18n();
  const [rows, setRows] = useState<EvolutionProposal[]>([]);
  const [instruction, setInstruction] = useState('');
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.get<EvolutionProposal[]>(
        `/api/enterprise/agents/${encodeURIComponent(agentId)}/evolution/proposals?tenant_id=${encodeURIComponent(TENANT_ID)}`,
      );
      setRows(result);
    } catch (error) {
      notify.error(localizeEvolutionError(error, '加载自进化候选失败', t));
    } finally {
      setLoading(false);
    }
  }, [agentId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const activeCount = useMemo(
    () => rows.filter((item) => ['ready_for_review', 'evaluation_failed'].includes(item.status)).length,
    [rows],
  );

  async function analyze() {
    setBusyAction('analyze');
    try {
      await api.post(
        `/api/enterprise/agents/${encodeURIComponent(agentId)}/evolution:analyze`,
        { tenant_id: TENANT_ID, instruction: instruction.trim() || undefined },
      );
      setInstruction('');
      notify.success(t('已从真实反馈生成候选草稿'));
      await load();
    } catch (error) {
      notify.error(localizeEvolutionError(error, '生成自进化候选失败', t));
    } finally {
      setBusyAction('');
    }
  }

  async function act(proposal: EvolutionProposal, action: 'evaluate' | 'approve' | 'reject' | 'rollback') {
    const key = `${proposal.id}:${action}`;
    setBusyAction(key);
    try {
      const body = action === 'reject'
        ? { tenant_id: TENANT_ID, reason: '管理员在员工档案中拒绝该候选' }
        : { tenant_id: TENANT_ID };
      await api.post(`/api/enterprise/evolution/proposals/${encodeURIComponent(proposal.id)}:${action}`, body);
      notify.success(t({
        evaluate: '候选校验完成',
        approve: '候选已批准并应用到员工私有版本',
        reject: '候选已拒绝',
        rollback: '已回滚本次自进化修改',
      }[action]));
      await load();
    } catch (error) {
      notify.error(localizeEvolutionError(error, '操作失败', t));
    } finally {
      setBusyAction('');
    }
  }

  return (
    <section className="mt-[20px] rounded-[22px] border border-[#e5e9f2] bg-white p-[20px] shadow-[0_10px_30px_rgba(31,42,68,0.04)]">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="flex size-8 items-center justify-center rounded-xl bg-[#ecfbf5] text-[#168760]">
              <StaffdeckIcon name="spark" />
            </span>
            <h3 className="m-0 text-[16px] font-semibold text-[#202226]">反馈自进化</h3>
            {activeCount > 0 && (
              <span className="rounded-full bg-[#fff4da] px-2 py-0.5 text-[11px] text-[#9a6a08]">
                {activeCount} 个待审核
              </span>
            )}
          </div>
          <p className="mt-2 mb-0 max-w-[760px] text-[13px] leading-5 text-[#7b8499]">
            从点踩归因和执行轨迹生成最小修改候选。候选不会自动进入运行链路，只有管理员批准后才写入员工私有 Skill/SOP 版本。
          </p>
        </div>
        <Button disabled={busyAction !== ''} onClick={() => void analyze()}>
          {busyAction === 'analyze' ? '正在分析反馈…' : '扫描反馈并生成候选'}
        </Button>
      </div>

      <textarea
        value={instruction}
        onChange={(event) => setInstruction(event.target.value)}
        placeholder="可选：补充本次改进目标，例如“只修复确认节点，不修改工具绑定”"
        className="mt-4 min-h-[66px] w-full resize-y rounded-xl border border-[#e4e8f0] bg-[#fafbfc] px-3 py-2 text-[13px] leading-5 text-[#313642] outline-none focus:border-[#8fd6bb]"
      />

      <div className="mt-4 grid gap-3">
        {!loading && rows.length === 0 && (
          <div className="rounded-xl border border-dashed border-[#dfe4ec] px-4 py-5 text-center text-[13px] text-[#8b94a8]">
            暂无候选。产生真实反馈后可扫描生成；上方补充目标用于约束本次修改范围。
          </div>
        )}
        {rows.map((proposal) => {
          const passed = proposal.evaluation?.passed === true;
          return (
            <article key={proposal.id} className="rounded-2xl border border-[#e7eaf0] bg-[#fcfcfd] p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <strong className="text-[14px] text-[#202226]">{proposal.resource_name}</strong>
                    <span className="rounded-full bg-[#eef2f8] px-2 py-0.5 text-[11px] text-[#657087]">
                      {proposal.resource_type === 'sop' ? 'SOP' : 'Skill'}
                    </span>
                    <span className={`rounded-full px-2 py-0.5 text-[11px] ${riskClass(proposal.risk_level)}`}>
                      {RISK_LABELS[proposal.risk_level] || proposal.risk_level}
                    </span>
                    <span className="rounded-full bg-white px-2 py-0.5 text-[11px] text-[#657087] ring-1 ring-[#e1e5ec]">
                      {STATUS_LABELS[proposal.status] || proposal.status}
                    </span>
                  </div>
                  <p className="mt-2 mb-0 text-[13px] font-medium text-[#444b59]">{proposal.hypothesis}</p>
                  <p className="mt-1 mb-0 text-[12px] leading-5 text-[#7b8499]">
                    {proposal.source_feedback_ids.length} 条反馈证据 · {proposal.diff.length} 项修改 ·
                    {passed ? ' 静态校验通过' : ' 等待或未通过校验'}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {['ready_for_review', 'evaluation_failed'].includes(proposal.status) && (
                    <>
                      <Button
                        variant="outline"
                        disabled={busyAction !== ''}
                        onClick={() => void act(proposal, 'evaluate')}
                      >
                        重新校验
                      </Button>
                      <Button
                        disabled={busyAction !== ''}
                        onClick={() => void act(proposal, 'approve')}
                      >
                        批准应用
                      </Button>
                      <Button
                        variant="outline"
                        disabled={busyAction !== ''}
                        onClick={() => void act(proposal, 'reject')}
                      >
                        拒绝
                      </Button>
                    </>
                  )}
                  {proposal.status === 'published' && (
                    <Button
                      variant="outline"
                      disabled={busyAction !== ''}
                      onClick={() => void act(proposal, 'rollback')}
                    >
                      回滚
                    </Button>
                  )}
                </div>
              </div>
              <details className="mt-3 rounded-xl bg-white px-3 py-2 ring-1 ring-[#edf0f4]">
                <summary className="cursor-pointer text-[12px] text-[#5f6b80]">
                  查看证据与修改明细
                </summary>
                <div className="mt-3 grid gap-3 text-[12px] leading-5 text-[#667085]">
                  <div>
                    <strong className="text-[#394150]">改进依据</strong>
                    <p className="mt-1 mb-0 whitespace-pre-wrap">{proposal.rationale || '无'}</p>
                  </div>
                  <div>
                    <strong className="text-[#394150]">预期结果</strong>
                    <p className="mt-1 mb-0">{proposal.expected_outcome || '无'}</p>
                  </div>
                  <div>
                    <strong className="text-[#394150]">结构化 Diff</strong>
                    <div className="mt-1 max-h-[240px] overflow-auto rounded-lg bg-[#f7f8fa] p-2 font-mono text-[11px]">
                      {proposal.diff.length === 0 ? '无修改' : proposal.diff.map((item, index) => (
                        <div key={`${item.path}-${index}`} className="border-b border-[#e9edf3] py-1 last:border-0">
                          <span className="mr-2 text-[#12805c]">{item.op || 'change'}</span>
                          <span>{item.path || '/'}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </details>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function riskClass(risk: string): string {
  if (risk === 'high') return 'bg-[#ffe8e8] text-[#c13d3d]';
  if (risk === 'medium') return 'bg-[#fff4da] text-[#9a6a08]';
  return 'bg-[#e8f8f0] text-[#168760]';
}

function localizeEvolutionError(
  error: unknown,
  fallback: string,
  t: (source: string) => string,
): string {
  if (error instanceof ApiError && error.code && EVOLUTION_ERROR_MESSAGES[error.code]) {
    return t(EVOLUTION_ERROR_MESSAGES[error.code]);
  }
  if (error instanceof Error) {
    return t(LEGACY_EVOLUTION_ERRORS[error.message] || error.message);
  }
  return t(fallback);
}
