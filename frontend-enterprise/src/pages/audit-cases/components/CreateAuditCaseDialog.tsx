import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { Dialog, DialogContent, DialogTitle } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { TENANT_ID } from '@/api/client';
import { DIALOG_CANCEL_BUTTON_CLASS, DIALOG_FOOTER_CLASS, DIALOG_PRIMARY_BUTTON_CLASS } from '@/lib/enterprise-ui';
import type { AuditCaseManagementOptions } from '@/types';

import { createAuditCase } from '../auditCaseApi';
import { auditCaseErrorMessage } from '../auditCaseErrors';
import type { AuditCaseCreateRequest } from '../auditCaseTypes';
import { AuditCaseBasicForm } from './AuditCaseBasicForm';
import { KnowledgeVersionSelector } from './KnowledgeVersionSelector';
import { MemberSelector } from './MemberSelector';

const EMPTY_DRAFT: AuditCaseCreateRequest = {
  tenant_id: TENANT_ID,
  agent_id: '',
  knowledge_scope_mode: 'agent_default',
  organization_name: '',
  report_type: '',
  management_systems: [],
  knowledge_base_version_ids: [],
  member_user_ids: [],
};

function initialDraft(options: AuditCaseManagementOptions): AuditCaseCreateRequest {
  const firstAgent = options.agent_options?.[0];
  if (!firstAgent) return { ...EMPTY_DRAFT, knowledge_scope_mode: 'custom' };
  return {
    ...EMPTY_DRAFT,
    agent_id: firstAgent.id,
    knowledge_scope_mode: 'agent_default',
    knowledge_base_version_ids: [...firstAgent.knowledge_base_version_ids],
  };
}

export function CreateAuditCaseDialog({
  open,
  options,
  onOpenChange,
}: {
  open: boolean;
  options: AuditCaseManagementOptions;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(() => initialDraft(options));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [knowledgeAdvanced, setKnowledgeAdvanced] = useState(false);
  const agentOptions = options.agent_options ?? [];
  const selectedAgent = agentOptions.find((agent) => agent.id === draft.agent_id);

  useEffect(() => {
    if (!open || !agentOptions.length) return;
    setDraft((previous) => {
      if (agentOptions.some((agent) => agent.id === previous.agent_id)) return previous;
      const firstAgent = agentOptions[0];
      return {
        ...previous,
        agent_id: firstAgent.id,
        knowledge_scope_mode: 'agent_default',
        knowledge_base_version_ids: [...firstAgent.knowledge_base_version_ids],
      };
    });
  }, [agentOptions, open]);

  function update(patch: Partial<AuditCaseCreateRequest>) {
    setDraft((previous) => ({ ...previous, ...patch }));
    setError('');
  }

  function handleOpenChange(nextOpen: boolean) {
    if (!nextOpen) {
      setDraft(initialDraft(options));
      setKnowledgeAdvanced(false);
      setError('');
    }
    onOpenChange(nextOpen);
  }

  async function submit() {
    if (!draft.organization_name.trim() || !draft.report_type.trim()) {
      setError('请填写企业名称和审核类型');
      return;
    }
    if (!agentOptions.length) {
      setError('暂无可用数字员工，请先创建并启用数字员工');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const created = await createAuditCase(draft);
      handleOpenChange(false);
      navigate(`/enterprise/audit-cases/${encodeURIComponent(created.id)}`);
    } catch (reason) {
      setError(auditCaseErrorMessage(reason, '创建认证项目失败'));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[560px]">
        <DialogTitle className="shrink-0 px-[24px] py-[18px] text-[16px] font-semibold text-[#18181a]">新建认证项目</DialogTitle>
        <div className="min-h-0 flex-1 space-y-[20px] overflow-y-auto px-[24px] pb-[20px]">
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">1 · 基本信息</p>
            <AuditCaseBasicForm draft={draft} options={options} onChange={update} />
          </section>
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">2 · 知识库范围</p>
            <label className="grid gap-[6px] text-[12px] text-[#464c5e]">
              数字员工
              <select
                aria-label="数字员工"
                className="h-[36px] rounded-[9px] border border-[#e3e7f1] bg-white px-[10px] text-[13px] text-[#464c5e] outline-none focus:border-[#aebbd3]"
                value={draft.agent_id}
                onChange={(event) => {
                  const agent = agentOptions.find((option) => option.id === event.target.value);
                  update({
                    agent_id: event.target.value,
                    knowledge_scope_mode: agent ? 'agent_default' : 'custom',
                    knowledge_base_version_ids: agent ? [...agent.knowledge_base_version_ids] : [],
                  });
                  setKnowledgeAdvanced(false);
                }}
              >
                {!agentOptions.length && <option value="">暂无可用数字员工</option>}
                {agentOptions.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
              </select>
            </label>
            <div className="grid gap-[8px] rounded-[10px] border border-[#e3e7f1] bg-[#f8f9fc] p-[12px]">
              <div className="flex flex-wrap items-center justify-between gap-[8px]">
                <div>
                  <p className="text-[12px] font-medium text-[#464c5e]">
                    {draft.knowledge_scope_mode === 'agent_default'
                      ? `已自动继承 ${draft.knowledge_base_version_ids.length} 个知识库版本`
                      : `已自定义 ${draft.knowledge_base_version_ids.length} 个知识库版本`}
                  </p>
                  <p className="mt-[2px] text-[11px] text-[#8b92a4]">
                    创建后保存当前版本快照，不受员工知识库后续调整影响。
                  </p>
                </div>
                <UIButton
                  type="button"
                  variant="outline"
                  className="h-[30px] rounded-[8px] px-[10px] text-[11px]"
                  aria-label="调整知识范围"
                  onClick={() => setKnowledgeAdvanced((value) => !value)}
                >
                  {knowledgeAdvanced ? '收起高级设置' : '调整知识范围'}
                </UIButton>
              </div>
              {knowledgeAdvanced && (
                <div className="grid gap-[8px] border-t border-[#e3e7f1] pt-[10px]">
                  {selectedAgent && (
                    <div className="flex justify-end">
                      <UIButton
                        type="button"
                        variant="ghost"
                        className="h-[28px] px-[8px] text-[11px] text-[#3768c5]"
                        onClick={() => update({
                          knowledge_scope_mode: 'agent_default',
                          knowledge_base_version_ids: [...selectedAgent.knowledge_base_version_ids],
                        })}
                      >
                        恢复员工默认
                      </UIButton>
                    </div>
                  )}
                  <KnowledgeVersionSelector
                    options={options}
                    selected={draft.knowledge_base_version_ids}
                    onChange={(ids) => update({
                      knowledge_base_version_ids: ids,
                      knowledge_scope_mode: 'custom',
                    })}
                  />
                </div>
              )}
            </div>
          </section>
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">3 · 项目成员</p>
            <MemberSelector selected={draft.member_user_ids} onChange={(ids) => update({ member_user_ids: ids })} />
          </section>
          {error && <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">{error}</p>}
        </div>
        <div className={DIALOG_FOOTER_CLASS}>
          <UIButton type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => handleOpenChange(false)}>取消</UIButton>
          <UIButton type="button" className={DIALOG_PRIMARY_BUTTON_CLASS} disabled={saving} onClick={() => void submit()}>{saving ? '创建中…' : '创建项目'}</UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}
