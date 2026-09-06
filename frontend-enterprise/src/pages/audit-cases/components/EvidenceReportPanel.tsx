import { useEffect, useState } from 'react';

import type { AuditCaseCoverageRead, AuditCaseDocumentRead, AuditReportRead } from '@/types';
import { Button, Card, CardContent } from '@/components/ui';

import { processAuditCaseEvidence } from '../auditCaseApi';
import { auditCaseErrorMessage } from '../auditCaseErrors';
import {
  createAuditCaseReport,
  downloadAuditCaseReport,
  listAuditCaseReports,
  publishAuditCaseReport,
} from '../auditReportApi';
import { CoveragePanel } from './CoveragePanel';
import { reportToWorkDocument } from '../workbenchApi';

type BusyAction = '' | 'process' | 'generate' | 'publish' | 'download' | 'export';

const REPORT_READY_STATUSES = new Set(['draft', 'review']);
const EMPTY_DOCUMENTS: AuditCaseDocumentRead[] = [];

function reportCanBeConfirmed(report: AuditReportRead | null): boolean {
  return Boolean(
    report
    && REPORT_READY_STATUSES.has(report.status)
    && report.sections.length > 0
    && report.sections.every((section) => section.status === 'succeeded')
    && report.rule_traceability_status === 'complete',
  );
}

function ruleStatusLabel(status: AuditReportRead['rule_traceability_status']): string {
  if (status === 'complete') return '规则已冻结';
  if (status === 'incomplete') return '规则快照不完整';
  return '规则尚未配置';
}

function actionError(error: unknown, fallback: string): string {
  const message = auditCaseErrorMessage(error, '');
  return message ? `${fallback}：${message}` : fallback;
}

export function EvidenceReportPanel({
  caseId,
  documents = EMPTY_DOCUMENTS,
  coverage,
  disabled = false,
  onChanged,
  selectedDocumentId: controlledDocumentId,
  onSelectedDocumentChange,
  canPublish = true,
  onWorkDocumentCreated,
}: {
  caseId: string;
  documents?: AuditCaseDocumentRead[];
  coverage: AuditCaseCoverageRead | null;
  disabled?: boolean;
  onChanged?: () => void | Promise<void>;
  selectedDocumentId?: string;
  onSelectedDocumentChange?: (id: string) => void;
  canPublish?: boolean;
  onWorkDocumentCreated?: (document: AuditCaseDocumentRead) => void | Promise<void>;
}) {
  const [reports, setReports] = useState<AuditReportRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<BusyAction>('');
  const [error, setError] = useState('');
  const [internalDocumentId, setSelectedDocumentId] = useState(
    () => documents.find((document) => document.status === 'active' && document.active_version_id)?.id || '',
  );
  const selectedDocumentId = controlledDocumentId ?? internalDocumentId;

  const activeDocuments = documents.filter(
    (document) => document.status === 'active' && document.active_version_id,
  );
  const selectedDocument = activeDocuments.find((document) => document.id === selectedDocumentId);
  const scopeRequired = (controlledDocumentId !== undefined || documents.length > 0) && !selectedDocument;

  useEffect(() => {
    setSelectedDocumentId((current) => (
      activeDocuments.some((document) => document.id === current)
        ? current
        : activeDocuments[0]?.id || ''
    ));
  }, [documents]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void listAuditCaseReports(caseId)
      .then((items) => {
        if (active) setReports(items);
      })
      .catch((loadError: unknown) => {
        if (active) setError(auditCaseErrorMessage(loadError, '报告列表加载失败，请稍后重试'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [caseId]);

  const scopedReports = reports.filter((report) => report.source_document_id === selectedDocumentId);
  const legacyReports = reports.filter((report) => !report.source_document_id);
  const visibleReports = controlledDocumentId !== undefined ? scopedReports : documents.length > 0
    ? (scopedReports.length ? scopedReports : legacyReports)
    : reports;
  const latestReport = visibleReports[0] ?? null;
  const pendingEvidence = Boolean(
    coverage
    && coverage.pending_chunk_ids?.length
    && coverage.file_coverage === 1
    && coverage.chunk_coverage === 1,
  );
  const canGenerate = Boolean(coverage?.publish_allowed === true);
  const actionDisabled = disabled || loading || Boolean(busy);

  async function refreshCase() {
    try {
      await onChanged?.();
    } catch {
      // Keep the successful action visible even if the parent refresh is stale.
    }
  }

  async function handleProcess() {
    if (!pendingEvidence || actionDisabled) return;
    setBusy('process');
    setError('');
    try {
      await processAuditCaseEvidence(caseId);
      await refreshCase();
    } catch (processError) {
      setError(actionError(processError, '证据处理失败，请稍后重试'));
    } finally {
      setBusy('');
    }
  }

  async function handleGenerate() {
    if (!canGenerate || actionDisabled || scopeRequired) return;
    setBusy('generate');
    setError('');
    try {
      const nextReport = selectedDocument
        ? await createAuditCaseReport(caseId, undefined, selectedDocument.id, selectedDocument.active_version_id)
        : await createAuditCaseReport(caseId);
      setReports((current) => [nextReport, ...current.filter((item) => item.id !== nextReport.id)]);
    } catch (generateError) {
      setError(actionError(generateError, '报告草稿生成失败，请稍后重试'));
    } finally {
      setBusy('');
    }
  }

  async function handlePublish() {
    if (!latestReport || !reportCanBeConfirmed(latestReport) || actionDisabled || !canPublish) return;
    setBusy('publish');
    setError('');
    try {
      const published = await publishAuditCaseReport(caseId, latestReport.id);
      setReports((current) => [published, ...current.filter((item) => item.id !== published.id)]);
    } catch (publishError) {
      setError(actionError(publishError, '报告确认发布失败，请稍后重试'));
    } finally {
      setBusy('');
    }
  }

  async function handleDownload() {
    if (!latestReport || !latestReport.final_storage_key || actionDisabled) return;
    setBusy('download');
    setError('');
    let objectUrl = '';
    try {
      const blob = await downloadAuditCaseReport(caseId, latestReport.id);
      objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = `audit-report-v${latestReport.version}.docx`;
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch (downloadError) {
      setError(actionError(downloadError, '报告下载失败，请稍后重试'));
    } finally {
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
      setBusy('');
    }
  }

  async function handleWorkDocument() {
    if (!latestReport || actionDisabled || !onWorkDocumentCreated) return;
    setBusy('export'); setError('');
    try { const document = await reportToWorkDocument(caseId, latestReport.id); await onWorkDocumentCreated(document); }
    catch (exportError) { setError(actionError(exportError, '转换复核文件失败')); }
    finally { setBusy(''); }
  }

  return (
    <div className="grid gap-[16px]">
      {disabled && (
        <p className="rounded-[9px] bg-[#f5f6f8] px-[12px] py-[10px] text-[12px] text-[#858b9c]">
          {controlledDocumentId === undefined ? '项目已归档，证据与报告不可修改' : '当前证据与报告只读：请检查项目角色或归档状态'}
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">
          {error}
        </p>
      )}
      <Card>
        <CardContent className="grid gap-[12px] p-[14px]">
          {documents.length > 0 && controlledDocumentId === undefined && (
            <label className="grid gap-[5px] text-[12px] font-medium text-[#464c5e]">
              报告规则文件
              <select
                aria-label="报告规则文件"
                value={selectedDocumentId}
                onChange={(event) => { setSelectedDocumentId(event.currentTarget.value); onSelectedDocumentChange?.(event.currentTarget.value); }}
                disabled={actionDisabled || !activeDocuments.length}
                className="rounded-[8px] border border-[#dfe3eb] bg-white px-[10px] py-[8px] text-[12px] font-normal"
              >
                {!activeDocuments.length && <option value="">暂无可用文件</option>}
                {activeDocuments.map((document) => (
                  <option key={document.id} value={document.id}>
                    {document.title} · {document.document_key}
                  </option>
                ))}
              </select>
              {scopeRequired && (
                <span className="text-[11px] font-normal text-[#a15c00]">请先选择一个可用文件</span>
              )}
            </label>
          )}
          <div>
            <h2 className="text-[14px] font-medium text-[#464c5e]">证据处理</h2>
            <p className="mt-[3px] text-[11px] text-[#a0a6b5]">只处理已完成提取和分块的当前材料，保留证据台账与知识库版本。</p>
          </div>
          <Button
            type="button"
            disabled={actionDisabled || !pendingEvidence}
            onClick={() => void handleProcess()}
          >
            {busy === 'process' ? '处理中…' : '处理证据'}
          </Button>
          {!pendingEvidence && coverage && coverage.pending_chunk_ids?.length ? (
            <p className="text-[11px] text-[#a15c00]">文件或分块覆盖率未完成，暂不能处理证据。</p>
          ) : null}
          {!coverage && <p className="text-[12px] text-[#858b9c]">覆盖率暂不可用</p>}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="grid gap-[12px] p-[14px]">
          <div>
            <h2 className="text-[14px] font-medium text-[#464c5e]">报告版本</h2>
            <p className="mt-[3px] text-[11px] text-[#a0a6b5]">
              当前状态：{ruleStatusLabel(latestReport?.rule_traceability_status)}
            </p>
          </div>
          <Button
            type="button"
            disabled={actionDisabled || !canGenerate || scopeRequired}
            onClick={() => void handleGenerate()}
          >
            {busy === 'generate' ? '生成中…' : '生成待确认草稿'}
          </Button>
          {latestReport && (
            <div className="grid gap-[8px] rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px]">
              <p className="text-[12px] font-medium text-[#464c5e]">版本 {latestReport.version} · {latestReport.status}</p>
              <p className="text-[11px] text-[#858b9c]">章节完成 {latestReport.sections.filter((section) => section.status === 'succeeded').length} / {latestReport.sections.length}</p>
              <div className="flex flex-wrap gap-[8px]">
                {onWorkDocumentCreated && <Button type="button" disabled={actionDisabled || !latestReport.sections.length || latestReport.sections.some((section) => section.status !== 'succeeded')} onClick={() => void handleWorkDocument()}>{busy === 'export' ? '转换中…' : '转为复核文件'}</Button>}
                <Button
                  type="button"
                  disabled={actionDisabled || !canPublish || !reportCanBeConfirmed(latestReport)}
                  onClick={() => void handlePublish()}
                >
                  {busy === 'publish' ? '发布中…' : '确认发布'}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled={actionDisabled || latestReport.status !== 'published' || !latestReport.final_storage_key}
                  onClick={() => void handleDownload()}
                >
                  {busy === 'download' ? '下载中…' : '下载报告'}
                </Button>
              </div>
            </div>
          )}
          {!latestReport && !loading && <p className="text-[12px] text-[#858b9c]">尚未生成报告版本。</p>}
        </CardContent>
      </Card>

      <CoveragePanel coverage={coverage} />
    </div>
  );
}
