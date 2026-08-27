import { useState } from 'react';
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
  organization_name: '',
  report_type: '',
  management_systems: [],
  knowledge_base_version_ids: [],
  member_user_ids: [],
};

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
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  function update(patch: Partial<AuditCaseCreateRequest>) {
    setDraft((previous) => ({ ...previous, ...patch }));
    setError('');
  }

  async function submit() {
    if (!draft.organization_name.trim() || !draft.report_type.trim()) {
      setError('请填写企业名称和报告类型');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const created = await createAuditCase(draft);
      onOpenChange(false);
      setDraft(EMPTY_DRAFT);
      navigate(`/enterprise/audit-cases/${encodeURIComponent(created.id)}`);
    } catch (reason) {
      setError(auditCaseErrorMessage(reason, '创建认证项目失败'));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] flex-col gap-0 overflow-hidden rounded-[16px] p-0 sm:max-w-[560px]">
        <DialogTitle className="shrink-0 px-[24px] py-[18px] text-[16px] font-semibold text-[#18181a]">新建认证项目</DialogTitle>
        <div className="min-h-0 flex-1 space-y-[20px] overflow-y-auto px-[24px] pb-[20px]">
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">1 · 基本信息</p>
            <AuditCaseBasicForm draft={draft} onChange={update} />
          </section>
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">2 · 知识库范围</p>
            <KnowledgeVersionSelector options={options} selected={draft.knowledge_base_version_ids} onChange={(ids) => update({ knowledge_base_version_ids: ids })} />
          </section>
          <section className="grid gap-[10px]">
            <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-[#a0a6b5]">3 · 项目成员</p>
            <MemberSelector selected={draft.member_user_ids} onChange={(ids) => update({ member_user_ids: ids })} />
          </section>
          {error && <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">{error}</p>}
        </div>
        <div className={DIALOG_FOOTER_CLASS}>
          <UIButton type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} onClick={() => onOpenChange(false)}>取消</UIButton>
          <UIButton type="button" className={DIALOG_PRIMARY_BUTTON_CLASS} disabled={saving} onClick={() => void submit()}>{saving ? '创建中…' : '创建项目'}</UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}
