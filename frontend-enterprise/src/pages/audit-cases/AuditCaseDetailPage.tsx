import { useEffect, useState } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useNavigate, useParams } from 'react-router-dom';

import { isEnterpriseAdmin, type EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

import { replaceAuditCaseMembers, updateAuditCase } from './auditCaseApi';
import { auditCaseErrorMessage } from './auditCaseErrors';
import { auditCaseStatusClass, auditCaseStatusLabel } from './auditCasePresentation';
import type { AuditCaseCreateRequest } from './auditCaseTypes';
import { AuditCaseBasicForm } from './components/AuditCaseBasicForm';
import { AuditCaseEventTimeline } from './components/AuditCaseEventTimeline';
import { EvidenceReportPanel } from './components/EvidenceReportPanel';
import { KnowledgeVersionSelector } from './components/KnowledgeVersionSelector';
import { MemberSelector } from './components/MemberSelector';
import { MaterialManager } from './components/MaterialManager';
import { ProjectDocumentWorkspace } from './components/ProjectDocumentWorkspace';
import { RuleBindingPanel } from './components/RuleBindingPanel';
import { useAuditCaseDetail } from './hooks/useAuditCaseDetail';

type DetailTab = 'overview' | 'members' | 'materials' | 'evidence-report' | 'documents' | 'rules' | 'events';

export default function AuditCaseDetailPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const { caseId } = useParams();
  const navigate = useNavigate();
  const detail = useAuditCaseDetail(caseId);
  const [tab, setTab] = useState<DetailTab>('overview');
  const [draft, setDraft] = useState<AuditCaseCreateRequest | null>(null);
  const [selectedMembers, setSelectedMembers] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [savingMembers, setSavingMembers] = useState(false);
  const [knowledgeAdvanced, setKnowledgeAdvanced] = useState(false);
  const [knowledgeDirty, setKnowledgeDirty] = useState(false);

  useEffect(() => {
    if (!detail.project) return;
    setDraft({
      tenant_id: detail.project.tenant_id,
      agent_id: detail.project.agent_id ?? '',
      knowledge_scope_mode: detail.project.knowledge_scope_mode ?? 'custom',
      organization_name: detail.project.organization_name,
      report_type: detail.project.report_type,
      management_systems: detail.project.management_systems,
      knowledge_base_version_ids: detail.project.knowledge_base_version_ids,
      member_user_ids: detail.project.member_user_ids,
    });
    setSelectedMembers(detail.project.member_user_ids);
    setKnowledgeDirty(false);
  }, [detail.project]);

  const archived = detail.project?.status === 'archived';
  const disabled = Boolean(archived || saving);

  async function saveBasic() {
    if (!detail.project || !draft || archived) return;
    setSaving(true);
    try {
      await updateAuditCase(detail.project.id, {
        organization_name: draft.organization_name,
        report_type: draft.report_type,
        management_systems: draft.management_systems,
        ...(knowledgeDirty
          ? { knowledge_base_version_ids: draft.knowledge_base_version_ids }
          : {}),
      });
      notify.success('项目已保存');
      await detail.reload();
    } catch (error) {
      notify.error(auditCaseErrorMessage(error, '保存项目失败'));
    } finally {
      setSaving(false);
    }
  }

  async function saveMembers() {
    if (!detail.project || archived) return;
    setSavingMembers(true);
    try {
      await replaceAuditCaseMembers(detail.project.id, selectedMembers);
      notify.success('项目成员已保存');
      await detail.reload();
    } catch (error) {
      notify.error(auditCaseErrorMessage(error, '保存项目成员失败'));
    } finally {
      setSavingMembers(false);
    }
  }

  if (detail.loading && !detail.project) return <div className="p-[32px] text-[13px] text-[#858b9c]">正在加载认证项目…</div>;
  if (detail.error && !detail.project) return <div className="p-[32px] text-[13px] text-[#c20d0d]" role="alert">{detail.error}</div>;
  if (!detail.project || !draft) return null;
  if (currentUser && !isEnterpriseAdmin(currentUser)) return <div className="p-[32px] text-[14px] text-[#858b9c]">仅管理员可以维护认证项目</div>;

  return (
    <div className="flex min-h-full flex-col">
      <AppHeader
        left={
          <div className="flex min-w-0 items-center gap-[10px]">
            <button type="button" aria-label="返回认证项目" onClick={() => navigate('/enterprise/audit-cases')} className="grid size-[30px] shrink-0 place-items-center rounded-[9px] text-[#757f9c] hover:bg-[#f2f3f7]"><ArrowLeft className="size-[16px]" /></button>
            <div className="min-w-0"><p className="truncate text-[16px] font-medium text-[#464c5e]">{detail.project.organization_name}</p><p className="mt-[3px] truncate text-[12px] text-[#a0a6b5]">认证项目详情</p></div>
          </div>
        }
        userName={currentUser?.display_name || currentUser?.username}
        onLogout={onLogout}
      />
      <main className="flex flex-1 flex-col gap-[18px] px-[24px] pb-[32px] pt-[8px]">
        <div className="flex flex-wrap items-center justify-between gap-[12px] rounded-[14px] border border-[#edf0f5] bg-white px-[18px] py-[16px]">
          <div><p className="text-[12px] text-[#858b9c]">审核类型</p><p className="mt-[4px] text-[14px] font-medium text-[#464c5e]">{detail.project.report_type}</p></div>
          <span className={cn('rounded-full px-[12px] py-[5px] text-[11px]', auditCaseStatusClass(detail.project.status))}>{auditCaseStatusLabel(detail.project.status)}</span>
        </div>
        <div className="flex gap-[4px] border-b border-[#edf0f5]" role="tablist" aria-label="认证项目详情标签">
          {([['overview', '项目设置'], ['members', '项目成员'], ['materials', '审核材料'], ['evidence-report', '证据与报告'], ['documents', '项目文件库'], ['rules', '规则绑定'], ['events', '操作记录']] as const).map(([value, label]) => (
            <button key={value} type="button" role="tab" aria-selected={tab === value} onClick={() => setTab(value)} className={cn('border-b-2 px-[14px] py-[9px] text-[12px]', tab === value ? 'border-[#18181a] font-medium text-[#18181a]' : 'border-transparent text-[#858b9c]')}>{label}</button>
          ))}
        </div>

        {tab === 'overview' && (
          <section className="grid gap-[18px] rounded-[14px] border border-[#edf0f5] bg-white p-[18px]">
            <div className="flex items-center justify-between gap-[12px]"><h2 className="text-[14px] font-medium text-[#464c5e]">基本信息</h2><UIButton type="button" disabled={disabled} onClick={() => void saveBasic()} className="h-[32px] rounded-[9px] bg-[#18181a] px-[14px] text-[12px] text-white">{saving ? '保存中…' : '保存'}</UIButton></div>
            <AuditCaseBasicForm draft={draft} options={detail.options} onChange={(patch) => setDraft((previous) => previous ? { ...previous, ...patch } : previous)} />
            <div className="grid gap-[8px] rounded-[10px] border border-[#e3e7f1] bg-[#f8f9fc] p-[12px]">
              <div className="flex flex-wrap items-center justify-between gap-[8px]">
                <div>
                  <p className="text-[12px] font-medium text-[#464c5e]">已冻结 {draft.knowledge_base_version_ids.length} 个知识库版本</p>
                  <p className="mt-[2px] text-[11px] text-[#8b92a4]">项目继续使用创建时的版本快照，避免审核依据随员工配置变化。</p>
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
                <div className="border-t border-[#e3e7f1] pt-[10px]">
                  <KnowledgeVersionSelector
                    options={detail.options}
                    selected={draft.knowledge_base_version_ids}
                    onChange={(ids) => {
                      setDraft((previous) => previous ? {
                        ...previous,
                        knowledge_base_version_ids: ids,
                        knowledge_scope_mode: 'custom',
                      } : previous);
                      setKnowledgeDirty(true);
                    }}
                  />
                </div>
              )}
            </div>
          </section>
        )}
        {tab === 'members' && (
          <section className="grid gap-[16px] rounded-[14px] border border-[#edf0f5] bg-white p-[18px]">
            <div className="flex items-center justify-between gap-[12px]"><h2 className="text-[14px] font-medium text-[#464c5e]">项目成员</h2><UIButton type="button" disabled={Boolean(archived || savingMembers)} onClick={() => void saveMembers()} className="h-[32px] rounded-[9px] bg-[#18181a] px-[14px] text-[12px] text-white">{savingMembers ? '保存中…' : '保存成员'}</UIButton></div>
            <MemberSelector selected={selectedMembers} onChange={setSelectedMembers} />
          </section>
        )}
        {tab === 'materials' && (
          <section className="grid gap-[12px] rounded-[14px] border border-[#edf0f5] bg-white p-[18px]"><h2 className="text-[14px] font-medium text-[#464c5e]">审核材料</h2><MaterialManager caseId={detail.project.id} materials={detail.materials} coverage={detail.coverage} options={detail.options} disabled={Boolean(archived)} onChanged={detail.reloadMaterials} /></section>
        )}
        {tab === 'evidence-report' && (
          <EvidenceReportPanel
            caseId={detail.project.id}
            documents={detail.documents}
            coverage={detail.coverage}
            disabled={Boolean(archived)}
            onChanged={detail.reloadMaterials}
          />
        )}
        {tab === 'documents' && <ProjectDocumentWorkspace caseId={detail.project.id} documents={detail.documents} disabled={Boolean(archived)} onChanged={detail.reloadDocuments} />}
        {tab === 'rules' && <RuleBindingPanel caseId={detail.project.id} documents={detail.documents} disabled={Boolean(archived)} />}
        {tab === 'events' && <AuditCaseEventTimeline events={detail.events} />}
      </main>
    </div>
  );
}
