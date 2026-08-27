import { useEffect, useState } from 'react';
import { Plus, RefreshCw } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import AppHeader from '@/components/AppHeader';
import { Paginator } from '@/components/Paginator';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { OUTLINE_ACTION_BUTTON_CLASS } from '@/lib/enterprise-ui';
import { isEnterpriseAdmin, type EnterpriseAuthUser } from '@/auth';
import type { AuditCaseManagementOptions } from '@/types';

import { loadAuditCaseManagementOptions } from './auditCaseApi';
import { auditCaseErrorMessage } from './auditCaseErrors';
import { AuditCaseFilters } from './components/AuditCaseFilters';
import { AuditCaseTable } from './components/AuditCaseTable';
import { CreateAuditCaseDialog } from './components/CreateAuditCaseDialog';
import { useAuditCaseList } from './hooks/useAuditCaseList';

const EMPTY_OPTIONS: AuditCaseManagementOptions = {
  knowledge_versions: [],
  supported_extensions: [],
  max_material_bytes: 50 * 1024 * 1024,
};

export default function AuditCasesPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
} = {}) {
  const navigate = useNavigate();
  const list = useAuditCaseList();
  const [options, setOptions] = useState<AuditCaseManagementOptions>(EMPTY_OPTIONS);
  const [optionsLoaded, setOptionsLoaded] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    let active = true;
    void loadAuditCaseManagementOptions()
      .then((result) => {
        if (active) {
          setOptions(result);
          setOptionsLoaded(true);
        }
      })
      .catch((error) => {
        if (active) {
          setOptionsLoaded(true);
          notify.error(auditCaseErrorMessage(error, '加载项目选项失败'));
        }
      });
    return () => { active = false; };
  }, []);

  if (currentUser && !isEnterpriseAdmin(currentUser)) {
    return (
      <div className="flex min-h-full items-center justify-center p-[32px] text-[14px] text-[#858b9c]">
        仅管理员可以维护认证项目
      </div>
    );
  }

  const pageCount = Math.ceil(list.total / (list.filters.limit || 20));

  return (
    <div className="flex min-h-full flex-col">
      <AppHeader title="认证项目" description="统一管理审核资料、知识库范围和项目成员" userName={currentUser?.display_name || currentUser?.username} onLogout={onLogout} />
      <main className="flex flex-1 flex-col gap-[18px] px-[24px] pb-[32px] pt-[8px]">
        <div className="flex flex-wrap items-center justify-between gap-[12px]">
          <AuditCaseFilters filters={list.filters} onFilter={list.setFilter} />
          <div className="flex items-center gap-[8px]">
            <UIButton type="button" variant="outline" className={OUTLINE_ACTION_BUTTON_CLASS} onClick={() => void list.reload()} disabled={list.loading}>
              <RefreshCw className="size-[14px]" /> 刷新
            </UIButton>
            <UIButton type="button" className="h-[34px] gap-[5px] rounded-[10px] bg-[#18181a] px-[16px] text-[12px] text-white hover:bg-[#303030]" onClick={() => setCreateOpen(true)}>
              <Plus className="size-[15px]" /> 新建认证项目
            </UIButton>
          </div>
        </div>

        {list.error ? (
          <div className="rounded-[12px] border border-[#ffd7d7] bg-[#fff7f7] px-[16px] py-[14px] text-[13px] text-[#c20d0d]" role="alert">
            <div className="flex items-center justify-between gap-[12px]">
              <span>{list.error}</span>
              <UIButton type="button" variant="outline" className="h-[30px] rounded-[8px] border-[#f0bcbc] px-[10px] text-[12px]" onClick={() => void list.reload()}>重试</UIButton>
            </div>
          </div>
        ) : list.rows.length === 0 && !list.loading ? (
          <div className="flex min-h-[300px] flex-col items-center justify-center rounded-[14px] border border-dashed border-[#e3e7f1] bg-white text-center">
            <div className="grid size-[46px] place-items-center rounded-[14px] bg-[#eef4ff] text-[#4d8cff]"><Plus className="size-[22px]" /></div>
            <p className="mt-[14px] text-[15px] font-medium text-[#464c5e]">暂无认证项目</p>
            <p className="mt-[6px] text-[12px] text-[#a0a6b5]">创建项目后即可集中维护审核材料和成员。</p>
            <UIButton type="button" variant="outline" className="mt-[16px] h-[32px] rounded-[9px] px-[14px] text-[12px]" onClick={() => setCreateOpen(true)}>创建第一个认证项目</UIButton>
          </div>
        ) : (
          <>
            <AuditCaseTable rows={list.rows} loading={list.loading} onOpen={(caseId) => navigate(`/enterprise/audit-cases/${encodeURIComponent(caseId)}`)} />
            <div className="flex items-center justify-between gap-[16px]">
              <p className="text-[12px] text-[#a0a6b5]">共 {list.total} 个认证项目</p>
              <Paginator page={list.page} pageCount={pageCount} onChange={list.setPage} aria-label="认证项目分页" />
            </div>
          </>
        )}
      </main>
      <CreateAuditCaseDialog open={createOpen} options={options} onOpenChange={setCreateOpen} />
      {!optionsLoaded && createOpen && <span className="sr-only">项目选项加载中</span>}
    </div>
  );
}
