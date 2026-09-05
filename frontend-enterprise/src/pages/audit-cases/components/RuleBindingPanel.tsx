import { useEffect, useMemo, useState } from 'react';

import { ApiError } from '@/api/client';
import { Button, Card, CardContent, Textarea, notify } from '@/components/ui';

import {
  loadCurrentRuleBindings,
  loadPublishedRuleVersionOptions,
  migrateRuleBindings,
  previewRuleBindingMigration,
  replaceCurrentRuleBindings,
  type PublishedRuleVersionOption,
  type RuleBindingRead,
  type RuleMigrationPreviewRead,
} from '../ruleBindingApi';

const NOT_INITIALIZED_CODE = 'RULE_BINDING_NOT_INITIALIZED';

function selectionKey(ids: string[]): string {
  return ids.join('|');
}

function sameSelection(left: string[], right: string[]): boolean {
  return selectionKey(left) === selectionKey(right);
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return `${fallback}：${error.message}`;
  return fallback;
}

function versionLabel(option: PublishedRuleVersionOption | undefined, versionId: string): string {
  return option ? `${option.ruleSet.name} · 版本 ${option.version.version}` : `版本 ${versionId}`;
}

function listOrEmpty(values: string[]): string {
  return values.length ? values.join('、') : '无';
}

export function RuleBindingPanel({
  caseId,
  disabled = false,
}: {
  caseId: string;
  disabled?: boolean;
}) {
  const [candidates, setCandidates] = useState<PublishedRuleVersionOption[]>([]);
  const [currentBindings, setCurrentBindings] = useState<RuleBindingRead[]>([]);
  const [selectedVersionIds, setSelectedVersionIds] = useState<string[]>([]);
  const [preview, setPreview] = useState<RuleMigrationPreviewRead | null>(null);
  const [previewSelection, setPreviewSelection] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [busy, setBusy] = useState<'bind' | 'preview' | 'migrate' | ''>('');
  const [error, setError] = useState('');

  const candidateByVersionId = useMemo(
    () => new Map(candidates.map((candidate) => [candidate.version.id, candidate])),
    [candidates],
  );
  const currentVersionIds = useMemo(
    () => currentBindings.map((binding) => binding.rule_set_version_id),
    [currentBindings],
  );
  const isBound = currentBindings.length > 0;
  const hasSelectionChanged = isBound && !sameSelection(selectedVersionIds, currentVersionIds);
  const canConfirmPreview = Boolean(
    preview && previewSelection === selectionKey(selectedVersionIds) && reason.trim(),
  );

  useEffect(() => {
    let active = true;
    setLoading(true);
    setLoadFailed(false);

    void Promise.allSettled([
      loadPublishedRuleVersionOptions(),
      loadCurrentRuleBindings(caseId),
    ]).then(([candidateResult, bindingResult]) => {
      if (!active) return;
      let failed = false;

      if (candidateResult.status === 'fulfilled') {
        setCandidates(candidateResult.value.filter((candidate) => candidate.version.status === 'published'));
      } else {
        failed = true;
        const message = errorMessage(candidateResult.reason, '规则版本候选加载失败，请稍后重试');
        setError(message);
        notify.error(message);
      }

      if (bindingResult.status === 'fulfilled') {
        setCurrentBindings(bindingResult.value);
        setSelectedVersionIds(bindingResult.value.map((binding) => binding.rule_set_version_id));
      } else if (bindingResult.reason instanceof ApiError && bindingResult.reason.code === NOT_INITIALIZED_CODE) {
        setCurrentBindings([]);
        setSelectedVersionIds([]);
      } else {
        failed = true;
        const message = errorMessage(bindingResult.reason, '当前规则绑定加载失败，请稍后重试');
        setError(message);
        notify.error(message);
      }

      setLoadFailed(failed);
      setLoading(false);
    });

    return () => {
      active = false;
    };
  }, [caseId]);

  function toggleVersion(versionId: string) {
    if (disabled || loadFailed || busy) return;
    setSelectedVersionIds((current) => (
      current.includes(versionId)
        ? current.filter((id) => id !== versionId)
        : [...current, versionId]
    ));
  }

  async function handleBind() {
    if (disabled || loadFailed || busy || !selectedVersionIds.length) return;
    setBusy('bind');
    setError('');
    try {
      const bindings = await replaceCurrentRuleBindings(caseId, selectedVersionIds, 'manual');
      setCurrentBindings(bindings);
      setSelectedVersionIds(bindings.map((binding) => binding.rule_set_version_id));
      notify.success('规则版本绑定成功');
    } catch (bindError) {
      const message = errorMessage(bindError, '规则版本绑定失败，请稍后重试');
      setError(message);
      notify.error(message);
    } finally {
      setBusy('');
    }
  }

  async function handlePreview() {
    if (disabled || loadFailed || busy || !hasSelectionChanged) return;
    setBusy('preview');
    setError('');
    try {
      const nextPreview = await previewRuleBindingMigration(caseId, selectedVersionIds);
      setPreview(nextPreview);
      setPreviewSelection(selectionKey(selectedVersionIds));
    } catch (previewError) {
      const message = errorMessage(previewError, '迁移影响预览失败，请稍后重试');
      setError(message);
      notify.error(message);
    } finally {
      setBusy('');
    }
  }

  async function handleMigrate() {
    if (disabled || loadFailed || busy || !canConfirmPreview) return;
    setBusy('migrate');
    setError('');
    try {
      const bindings = await migrateRuleBindings(caseId, selectedVersionIds, reason.trim());
      setCurrentBindings(bindings);
      setSelectedVersionIds(bindings.map((binding) => binding.rule_set_version_id));
      setPreview(null);
      setPreviewSelection('');
      setReason('');
      notify.success('规则版本迁移成功');
    } catch (migrationError) {
      const message = errorMessage(migrationError, '规则版本迁移失败，请检查原因后重试');
      setError(message);
      notify.error(message);
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="grid gap-[16px]">
      {disabled && (
        <p className="rounded-[9px] bg-[#f5f6f8] px-[12px] py-[10px] text-[12px] text-[#858b9c]">
          项目已归档，规则绑定不可修改
        </p>
      )}
      {error && (
        <p role="alert" className="rounded-[9px] bg-[#fff1f1] px-[12px] py-[10px] text-[12px] text-[#c20d0d]">
          {error}
        </p>
      )}
      <Card>
        <CardContent className="grid gap-[12px] p-[14px]">
          <div>
            <h2 className="text-[14px] font-medium text-[#464c5e]">规则版本候选</h2>
            <p className="mt-[3px] text-[11px] text-[#a0a6b5]">仅展示已发布版本，项目绑定后不会自动切换到更新版本。</p>
          </div>
          {loading && <p className="text-[12px] text-[#858b9c]">加载规则版本中…</p>}
          {!loading && candidates.length === 0 && (
            <p className="rounded-[9px] border border-dashed border-[#e1e5ed] px-[12px] py-[12px] text-center text-[12px] text-[#858b9c]">
              暂无已发布规则版本
            </p>
          )}
          {!loading && candidates.map((candidate) => {
            const label = `${candidate.ruleSet.name} · 版本 ${candidate.version.version}`;
            return (
              <label key={candidate.version.id} className="flex cursor-pointer items-start gap-[9px] rounded-[9px] border border-[#edf0f5] px-[10px] py-[9px] hover:bg-[#fafbfc]">
                <input
                  type="checkbox"
                  aria-label={label}
                  checked={selectedVersionIds.includes(candidate.version.id)}
                  disabled={disabled || loading || loadFailed || Boolean(busy)}
                  onChange={() => toggleVersion(candidate.version.id)}
                  className="mt-[2px]"
                />
                <span className="min-w-0">
                  <span className="block text-[12px] font-medium text-[#464c5e]">{label}</span>
                  <span className="block text-[11px] text-[#a0a6b5]">{candidate.ruleSet.key} · 已发布</span>
                </span>
              </label>
            );
          })}
          {!isBound ? (
            <Button type="button" disabled={disabled || loading || loadFailed || Boolean(busy) || !selectedVersionIds.length} onClick={() => void handleBind()}>
              {busy === 'bind' ? '绑定中…' : '绑定选中版本'}
            </Button>
          ) : (
            <div className="flex flex-wrap gap-[8px]">
              <Button type="button" disabled={disabled || loading || loadFailed || Boolean(busy) || !hasSelectionChanged} onClick={() => void handlePreview()}>
                {busy === 'preview' ? '预览中…' : '预览迁移影响'}
              </Button>
              <Button type="button" disabled={disabled || loading || loadFailed || Boolean(busy) || !canConfirmPreview} onClick={() => void handleMigrate()}>
                {busy === 'migrate' ? '迁移中…' : '确认迁移'}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="grid gap-[10px] p-[14px]">
          <div>
            <h2 className="text-[14px] font-medium text-[#464c5e]">当前绑定</h2>
            <p className="mt-[3px] text-[11px] text-[#a0a6b5]">当前绑定是项目实际使用的固定版本，不会自动切换。</p>
          </div>
          {!isBound ? (
            <p className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px] text-[12px] text-[#858b9c]">
              {loadFailed ? '当前绑定状态不可用，请重新加载后再编辑' : '尚未绑定规则版本'}
            </p>
          ) : currentBindings.map((binding) => (
            <div key={binding.id} className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px]">
              <p className="text-[12px] font-medium text-[#464c5e]">{versionLabel(candidateByVersionId.get(binding.rule_set_version_id), binding.rule_set_version_id)}</p>
              <p className="mt-[3px] text-[11px] text-[#858b9c]">来源：{binding.selection_source} · 已固定</p>
            </div>
          ))}
        </CardContent>
      </Card>

      {preview && (
        <Card>
          <CardContent className="grid gap-[8px] p-[14px]">
            <h2 className="text-[14px] font-medium text-[#464c5e]">迁移影响预览</h2>
            <p className="text-[12px] text-[#697189]">新增规则：{listOrEmpty(preview.added_rule_keys)}</p>
            <p className="text-[12px] text-[#697189]">移除规则：{listOrEmpty(preview.removed_rule_keys)}</p>
            <p className="text-[12px] text-[#697189]">变更规则：{listOrEmpty(preview.changed_rule_keys)}</p>
            <p className="text-[12px] text-[#697189]">影响信息域：{listOrEmpty(preview.impacted_information_domains)}</p>
            <p className="text-[12px] text-[#697189]">影响流程节点：{listOrEmpty(preview.impacted_workflow_nodes)}</p>
            <label className="grid gap-[5px] text-[12px] font-medium text-[#464c5e]">
              迁移原因
              <Textarea value={reason} onChange={(event) => setReason(event.currentTarget.value)} disabled={disabled || Boolean(busy)} placeholder="请输入迁移原因" />
            </label>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
