import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { BookOpenCheck, Plus, Save, ShieldCheck, Trash2, Upload } from 'lucide-react';

import type { EnterpriseAuthUser } from '@/auth';
import AppHeader from '@/components/AppHeader';
import {
  Card,
  CardContent,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button as UIButton } from '@/components/ui/button';
import {
  DIALOG_CANCEL_BUTTON_CLASS,
  DIALOG_PRIMARY_BUTTON_CLASS,
  OUTLINE_ACTION_BUTTON_SM_CLASS,
  SELECT_TRIGGER_CLASS,
} from '@/lib/enterprise-ui';
import { cn } from '@/lib/utils';

import {
  createRuleSet,
  createRuleSetVersion,
  listRuleDefinitions,
  listRuleSets,
  listRuleSetVersions,
  publishRuleSetVersion,
  replaceRuleDefinitions,
  validateRuleSetVersion,
  type RuleDefinitionDraft,
  type RuleDefinitionRead,
  type RuleExecutionLevel,
  type RuleExecutionMethod,
  type RuleSetCreate,
  type RuleSetRead,
  type RuleSetVersionRead,
} from './ruleLibraryApi';

type EditableRule = RuleDefinitionDraft & { clientId: string };

type RuleSetForm = {
  key: string;
  name: string;
  description: string;
  managementSystems: string;
  auditTypes: string;
  businessDomain: string;
};

const EMPTY_RULE_SET_FORM: RuleSetForm = {
  key: '',
  name: '',
  description: '',
  managementSystems: '',
  auditTypes: '',
  businessDomain: '',
};

let nextClientRuleId = 0;

function clientRuleId(): string {
  nextClientRuleId += 1;
  return `rule-editor-${nextClientRuleId}`;
}

function splitList(value: string): string[] {
  return [...new Set(value.split(/[\n,，]/).map((item) => item.trim()).filter(Boolean))];
}

function blankRule(sequence: number): EditableRule {
  return {
    clientId: clientRuleId(),
    rule_key: '',
    name: '',
    description: '',
    workflow_nodes: [],
    information_domains: [],
    document_types: [],
    field_keys: [],
    execution_level: 'guidance',
    execution_method: 'deterministic',
    condition: { operator: '', field_key: '', value: '' },
    input_requirements: [],
    evidence_requirements: [],
    source_refs: [],
    sequence,
    enabled: true,
  };
}

function editableRule(rule: RuleDefinitionRead): EditableRule {
  return {
    rule_key: rule.rule_key,
    name: rule.name,
    description: rule.description,
    workflow_nodes: [...rule.workflow_nodes],
    information_domains: [...rule.information_domains],
    document_types: [...rule.document_types],
    field_keys: [...rule.field_keys],
    execution_level: rule.execution_level,
    execution_method: rule.execution_method,
    condition: { ...rule.condition },
    input_requirements: rule.input_requirements.map((item) => ({ ...item })),
    evidence_requirements: rule.evidence_requirements.map((item) => ({ ...item })),
    source_refs: rule.source_refs.map((item) => ({ ...item })),
    sequence: rule.sequence,
    enabled: rule.enabled,
    clientId: clientRuleId(),
  };
}

function definitionPayload(rule: EditableRule, sequence: number): RuleDefinitionDraft {
  const { clientId: _clientId, ...definition } = rule;
  return { ...definition, sequence };
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return `${fallback}：${error.message}`;
  return fallback;
}

function versionLabel(version: RuleSetVersionRead): string {
  return `v${version.version} · ${version.status === 'published' ? '已发布' : '草稿'}`;
}

async function loadRuleSetSnapshot(ruleSet: RuleSetRead) {
  const versions = await listRuleSetVersions(ruleSet.id);
  const selectedVersion = versions.find((item) => item.status === 'draft') ?? versions[0] ?? null;
  const rules = selectedVersion
    ? await listRuleDefinitions(ruleSet.id, selectedVersion.id)
    : [];
  return {
    ruleSet,
    versions,
    selectedVersion,
    rules: rules.map(editableRule),
  };
}

export default function RuleLibraryPage({
  currentUser,
  onLogout,
}: {
  currentUser: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const [ruleSets, setRuleSets] = useState<RuleSetRead[]>([]);
  const [selectedRuleSet, setSelectedRuleSet] = useState<RuleSetRead | null>(null);
  const [versions, setVersions] = useState<RuleSetVersionRead[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<RuleSetVersionRead | null>(null);
  const [rules, setRules] = useState<EditableRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<RuleSetForm>(EMPTY_RULE_SET_FORM);
  const [validationErrors, setValidationErrors] = useState<string[] | null>(null);

  const isDraft = selectedVersion?.status === 'draft';
  const selectedRuleSetId = selectedRuleSet?.id ?? '';

  useEffect(() => {
    let active = true;
    async function loadInitialData() {
      setLoading(true);
      try {
        const rows = await listRuleSets();
        if (!active) return;
        setRuleSets(rows);
        if (rows.length === 0) {
          setSelectedRuleSet(null);
          setVersions([]);
          setSelectedVersion(null);
          setRules([]);
          return;
        }
        const snapshot = await loadRuleSetSnapshot(rows[0]);
        if (!active) return;
        applySnapshot(snapshot);
      } catch (error) {
        if (active) notify.error(errorMessage(error, '加载规则库失败，请重试'));
      } finally {
        if (active) setLoading(false);
      }
    }
    void loadInitialData();
    return () => {
      active = false;
    };
  }, []);

  function applySnapshot(snapshot: Awaited<ReturnType<typeof loadRuleSetSnapshot>>) {
    setSelectedRuleSet(snapshot.ruleSet);
    setVersions(snapshot.versions);
    setSelectedVersion(snapshot.selectedVersion);
    setRules(snapshot.rules);
    setValidationErrors(null);
  }

  async function selectRuleSet(ruleSet: RuleSetRead) {
    if (ruleSet.id === selectedRuleSetId || busy) return;
    setBusy(true);
    try {
      const snapshot = await loadRuleSetSnapshot(ruleSet);
      applySnapshot(snapshot);
    } catch (error) {
      notify.error(errorMessage(error, '加载规则版本失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  async function selectVersion(versionId: string) {
    if (!selectedRuleSet || versionId === selectedVersion?.id || busy) return;
    const version = versions.find((item) => item.id === versionId);
    if (!version) return;
    setBusy(true);
    try {
      const rows = await listRuleDefinitions(selectedRuleSet.id, version.id);
      setSelectedVersion(version);
      setRules(rows.map(editableRule));
      setValidationErrors(null);
    } catch (error) {
      notify.error(errorMessage(error, '加载规则内容失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  async function submitRuleSet() {
    const key = createForm.key.trim();
    const name = createForm.name.trim();
    if (!key || !name) {
      notify.error('请填写规则集标识和名称');
      return;
    }
    const request: RuleSetCreate = {
      key,
      name,
      description: createForm.description.trim(),
      management_systems: splitList(createForm.managementSystems),
      audit_types: splitList(createForm.auditTypes),
      business_domain: createForm.businessDomain.trim(),
    };
    setBusy(true);
    try {
      const created = await createRuleSet(request);
      const snapshot = await loadRuleSetSnapshot(created);
      setRuleSets((current) => [...current, created]);
      applySnapshot(snapshot);
      setCreateForm(EMPTY_RULE_SET_FORM);
      setCreateOpen(false);
      notify.success('规则集创建成功');
    } catch (error) {
      notify.error(errorMessage(error, '创建规则集失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  async function createDraft() {
    if (!selectedRuleSet || busy) return;
    setBusy(true);
    try {
      const payload = rules.map(definitionPayload);
      const created = await createRuleSetVersion(selectedRuleSet.id, payload);
      setVersions((current) => [created, ...current]);
      setSelectedVersion(created);
      setValidationErrors(null);
      notify.success('草稿版本已创建');
    } catch (error) {
      notify.error(errorMessage(error, '创建草稿版本失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  function updateRule(index: number, patch: Partial<EditableRule>) {
    setRules((current) => current.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch } : item
    )));
    setValidationErrors(null);
  }

  function updateCondition(index: number, key: 'operator' | 'field_key' | 'value', value: string) {
    const current = rules[index];
    if (!current) return;
    updateRule(index, { condition: { ...current.condition, [key]: value } });
  }

  function addRule() {
    setRules((current) => [...current, blankRule(current.length)]);
    setValidationErrors(null);
  }

  function removeRule(index: number) {
    setRules((current) => current.filter((_, itemIndex) => itemIndex !== index));
    setValidationErrors(null);
  }

  function draftPayload(): RuleDefinitionDraft[] | null {
    const payload = rules.map(definitionPayload);
    if (payload.some((item) => !item.rule_key.trim() || !item.name.trim())) {
      notify.error('每条规则都必须填写规则标识和名称');
      return null;
    }
    return payload;
  }

  async function saveDraft() {
    if (!selectedRuleSet || !selectedVersion || !isDraft || busy) return;
    const payload = draftPayload();
    if (!payload) return;
    setBusy(true);
    try {
      const updated = await replaceRuleDefinitions(selectedRuleSet.id, selectedVersion.id, payload);
      setSelectedVersion(updated);
      setVersions((current) => current.map((item) => item.id === updated.id ? updated : item));
      setValidationErrors(null);
      notify.success('草稿已保存');
    } catch (error) {
      notify.error(errorMessage(error, '保存草稿失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  async function validateDraft() {
    if (!selectedRuleSet || !selectedVersion || !isDraft || busy) return;
    setBusy(true);
    try {
      const result = await validateRuleSetVersion(selectedRuleSet.id, selectedVersion.id);
      setValidationErrors(result.errors);
      if (result.errors.length === 0) notify.success('规则校验通过');
      else notify.error(`规则校验发现 ${result.errors.length} 个问题，请处理后重试`);
    } catch (error) {
      notify.error(errorMessage(error, '校验规则失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  async function publishDraft() {
    if (!selectedRuleSet || !selectedVersion || !isDraft || busy) return;
    setBusy(true);
    try {
      const published = await publishRuleSetVersion(selectedRuleSet.id, selectedVersion.id);
      setSelectedVersion(published);
      setVersions((current) => current.map((item) => item.id === published.id ? published : item));
      setValidationErrors(null);
      notify.success('规则版本已发布');
    } catch (error) {
      notify.error(errorMessage(error, '发布规则版本失败，请重试'));
    } finally {
      setBusy(false);
    }
  }

  const versionOptions = useMemo(
    () => versions.map((version) => ({ value: version.id, label: versionLabel(version) })),
    [versions],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-[18px]">
      <AppHeader
        title="规则库管理"
        description="集中维护认证项目使用的规则集、草稿与已发布版本"
        userName={currentUser.display_name || currentUser.username}
        onLogout={onLogout}
      />
      <div className="flex justify-end">
        <UIButton type="button" onClick={() => setCreateOpen(true)}>
          <Plus className="size-[15px]" />
          新建规则集
        </UIButton>
      </div>

      {loading ? (
        <Card className="grid min-h-[220px] place-items-center border-[#e9ecf3] shadow-none">
          <p className="text-[13px] text-[#858b9c]">正在加载规则库…</p>
        </Card>
      ) : (
        <div className="grid min-h-0 flex-1 gap-[16px] lg:grid-cols-[240px_minmax(0,1fr)]">
          <Card className="min-h-0 border-[#e9ecf3] shadow-none">
            <CardContent className="flex h-full flex-col gap-[10px] p-[14px]">
              <div className="flex items-center gap-[8px] px-[4px] text-[12px] font-medium text-[#464c5e]">
                <BookOpenCheck className="size-[15px]" />
                规则集
              </div>
              {ruleSets.length === 0 ? (
                <div className="rounded-[10px] border border-dashed border-[#dfe3ec] px-[12px] py-[24px] text-center text-[12px] text-[#858b9c]">
                  暂无规则集
                </div>
              ) : (
                <div className="flex flex-col gap-[6px]">
                  {ruleSets.map((ruleSet) => (
                    <button
                      key={ruleSet.id}
                      type="button"
                      aria-label={ruleSet.name}
                      aria-pressed={selectedRuleSetId === ruleSet.id}
                      disabled={busy}
                      onClick={() => void selectRuleSet(ruleSet)}
                      className={cn(
                        'flex min-w-0 flex-col items-start gap-[3px] rounded-[10px] px-[12px] py-[10px] text-left transition-colors disabled:opacity-60',
                        selectedRuleSetId === ruleSet.id
                          ? 'bg-[#eef4ff] text-[#155ec2]'
                          : 'text-[#464c5e] hover:bg-[#f6f7fa]',
                      )}
                    >
                      <span className="w-full truncate text-[13px] font-medium">{ruleSet.name}</span>
                      <span className="w-full truncate text-[11px] opacity-70">{ruleSet.key}</span>
                    </button>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {selectedRuleSet ? (
            <div className="flex min-w-0 flex-col gap-[14px]">
              <Card className="border-[#e9ecf3] shadow-none">
                <CardContent className="flex flex-col gap-[14px] p-[18px] sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <h2 className="truncate text-[18px] font-semibold text-[#252936]">{selectedRuleSet.name}</h2>
                    <p className="mt-[4px] text-[12px] text-[#858b9c]">
                      {selectedRuleSet.key}{selectedRuleSet.description ? ` · ${selectedRuleSet.description}` : ''}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-[8px]">
                    {versions.length > 0 ? (
                      <Select value={selectedVersion?.id} onValueChange={(value) => void selectVersion(value)} disabled={busy}>
                        <SelectTrigger aria-label="规则版本" className={cn(SELECT_TRIGGER_CLASS, 'min-w-[140px]')}>
                          <SelectValue placeholder="选择版本" />
                        </SelectTrigger>
                        <SelectContent>
                          {versionOptions.map((option) => (
                            <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <span className="rounded-[9px] bg-[#f4f5f8] px-[12px] py-[8px] text-[12px] text-[#858b9c]">暂无版本</span>
                    )}
                    {!isDraft && (
                      <UIButton
                        type="button"
                        variant="outline"
                        className={OUTLINE_ACTION_BUTTON_SM_CLASS}
                        disabled={busy}
                        onClick={() => void createDraft()}
                      >
                        <Plus className="size-[14px]" />
                        {selectedVersion ? '基于当前版本新建草稿' : '新建草稿版本'}
                      </UIButton>
                    )}
                  </div>
                </CardContent>
              </Card>

              {selectedVersion ? (
                isDraft ? (
                  <DraftEditor
                    rules={rules}
                    busy={busy}
                    validationErrors={validationErrors}
                    onUpdate={updateRule}
                    onUpdateCondition={updateCondition}
                    onAdd={addRule}
                    onRemove={removeRule}
                    onSave={() => void saveDraft()}
                    onValidate={() => void validateDraft()}
                    onPublish={() => void publishDraft()}
                  />
                ) : (
                  <PublishedRules rules={rules} />
                )
              ) : (
                <Card className="grid min-h-[260px] place-items-center border-[#e9ecf3] shadow-none">
                  <div className="flex flex-col items-center gap-[10px] text-center">
                    <p className="text-[14px] font-medium text-[#464c5e]">尚未创建任何版本</p>
                    <p className="text-[12px] text-[#858b9c]">创建草稿版本后即可添加和维护规则。</p>
                  </div>
                </Card>
              )}
            </div>
          ) : (
            <Card className="grid min-h-[260px] place-items-center border-[#e9ecf3] shadow-none">
              <p className="text-[13px] text-[#858b9c]">新建规则集后开始维护规则。</p>
            </Card>
          )}
        </div>
      )}

      <CreateRuleSetDialog
        open={createOpen}
        busy={busy}
        form={createForm}
        onOpenChange={setCreateOpen}
        onChange={setCreateForm}
        onSubmit={() => void submitRuleSet()}
      />
    </div>
  );
}

function CreateRuleSetDialog({
  open,
  busy,
  form,
  onOpenChange,
  onChange,
  onSubmit,
}: {
  open: boolean;
  busy: boolean;
  form: RuleSetForm;
  onOpenChange: (open: boolean) => void;
  onChange: (form: RuleSetForm) => void;
  onSubmit: () => void;
}) {
  const update = (patch: Partial<RuleSetForm>) => onChange({ ...form, ...patch });
  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-[560px] gap-0 overflow-hidden p-0">
        <DialogHeader className="px-[24px] pt-[22px] pb-[16px]">
          <DialogTitle>新建规则集</DialogTitle>
          <DialogDescription>规则集创建后，再建立草稿版本并添加规则。</DialogDescription>
        </DialogHeader>
        <div className="grid gap-[14px] px-[24px] pb-[20px] sm:grid-cols-2">
          <FormField label="规则集标识" required>
            <Input aria-label="规则集标识" value={form.key} onChange={(event) => update({ key: event.target.value })} placeholder="energy.audit" />
          </FormField>
          <FormField label="规则集名称" required>
            <Input aria-label="规则集名称" value={form.name} onChange={(event) => update({ name: event.target.value })} placeholder="能源审核规则" />
          </FormField>
          <FormField label="规则集描述" className="sm:col-span-2">
            <Textarea value={form.description} onChange={(event) => update({ description: event.target.value })} rows={3} />
          </FormField>
          <FormField label="管理体系">
            <Input value={form.managementSystems} onChange={(event) => update({ managementSystems: event.target.value })} placeholder="EnMS, QMS" />
          </FormField>
          <FormField label="审核类型">
            <Input value={form.auditTypes} onChange={(event) => update({ auditTypes: event.target.value })} placeholder="recertification" />
          </FormField>
          <FormField label="业务领域" className="sm:col-span-2">
            <Input value={form.businessDomain} onChange={(event) => update({ businessDomain: event.target.value })} placeholder="energy" />
          </FormField>
        </div>
        <DialogFooter className="border-t border-[#eef0f4]">
          <UIButton type="button" variant="outline" className={DIALOG_CANCEL_BUTTON_CLASS} disabled={busy} onClick={() => onOpenChange(false)}>
            取消
          </UIButton>
          <UIButton type="button" className={DIALOG_PRIMARY_BUTTON_CLASS} disabled={busy} onClick={onSubmit}>
            创建规则集
          </UIButton>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DraftEditor({
  rules,
  busy,
  validationErrors,
  onUpdate,
  onUpdateCondition,
  onAdd,
  onRemove,
  onSave,
  onValidate,
  onPublish,
}: {
  rules: EditableRule[];
  busy: boolean;
  validationErrors: string[] | null;
  onUpdate: (index: number, patch: Partial<EditableRule>) => void;
  onUpdateCondition: (index: number, key: 'operator' | 'field_key' | 'value', value: string) => void;
  onAdd: () => void;
  onRemove: (index: number) => void;
  onSave: () => void;
  onValidate: () => void;
  onPublish: () => void;
}) {
  return (
    <div className="flex flex-col gap-[12px]">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <div>
          <h3 className="text-[15px] font-medium text-[#252936]">草稿规则</h3>
          <p className="mt-[2px] text-[11px] text-[#858b9c]">使用逗号或换行分隔多值字段。</p>
        </div>
        <div className="flex flex-wrap gap-[8px]">
          <UIButton type="button" variant="outline" className={OUTLINE_ACTION_BUTTON_SM_CLASS} disabled={busy} onClick={onAdd}>
            <Plus className="size-[14px]" />添加规则
          </UIButton>
          <UIButton type="button" variant="outline" className={OUTLINE_ACTION_BUTTON_SM_CLASS} disabled={busy} onClick={onSave}>
            <Save className="size-[14px]" />保存草稿
          </UIButton>
          <UIButton type="button" variant="outline" className={OUTLINE_ACTION_BUTTON_SM_CLASS} disabled={busy} onClick={onValidate}>
            <ShieldCheck className="size-[14px]" />校验规则
          </UIButton>
          <UIButton type="button" disabled={busy} onClick={onPublish}>
            <Upload className="size-[14px]" />发布版本
          </UIButton>
        </div>
      </div>

      {validationErrors !== null && (
        validationErrors.length === 0 ? (
          <div role="status" className="rounded-[10px] border border-[#96d9b0] bg-[#e9f7ef] px-[14px] py-[10px] text-[12px] text-[#018434]">
            校验通过，可以发布。
          </div>
        ) : (
          <div role="alert" className="rounded-[10px] border border-[#f3c6c6] bg-[#fff3f3] px-[14px] py-[10px] text-[12px] text-[#b42318]">
            <p className="font-medium">请处理以下校验问题：</p>
            <ul className="mt-[5px] list-disc pl-[18px]">
              {validationErrors.map((error) => <li key={error}>{error}</li>)}
            </ul>
          </div>
        )
      )}

      {rules.length === 0 ? (
        <Card className="grid min-h-[180px] place-items-center border-dashed border-[#dfe3ec] shadow-none">
          <p className="text-[12px] text-[#858b9c]">草稿中暂无规则，请添加第一条规则。</p>
        </Card>
      ) : rules.map((rule, index) => (
        <RuleEditorCard
          key={rule.clientId}
          rule={rule}
          index={index}
          busy={busy}
          onUpdate={onUpdate}
          onUpdateCondition={onUpdateCondition}
          onRemove={onRemove}
        />
      ))}
    </div>
  );
}

function RuleEditorCard({
  rule,
  index,
  busy,
  onUpdate,
  onUpdateCondition,
  onRemove,
}: {
  rule: EditableRule;
  index: number;
  busy: boolean;
  onUpdate: (index: number, patch: Partial<EditableRule>) => void;
  onUpdateCondition: (index: number, key: 'operator' | 'field_key' | 'value', value: string) => void;
  onRemove: (index: number) => void;
}) {
  const number = index + 1;
  return (
    <Card className="border-[#e9ecf3] shadow-none">
      <CardContent className="flex flex-col gap-[16px] p-[18px]">
        <div className="flex items-center justify-between gap-[12px]">
          <h4 className="text-[14px] font-medium text-[#252936]">规则 {number}</h4>
          <UIButton
            type="button"
            variant="ghost"
            aria-label={`删除规则 ${number}`}
            disabled={busy}
            onClick={() => onRemove(index)}
            className="size-[30px] p-0 text-[#a0a8bd] hover:bg-[#fff0f0] hover:text-[#d20b0b]"
          >
            <Trash2 className="size-[14px]" />
          </UIButton>
        </div>
        <div className="grid gap-[14px] md:grid-cols-2 xl:grid-cols-3">
          <FormField label={`规则标识 ${number}`} required>
            <Input aria-label={`规则标识 ${number}`} value={rule.rule_key} onChange={(event) => onUpdate(index, { rule_key: event.target.value })} />
          </FormField>
          <FormField label={`规则名称 ${number}`} required>
            <Input aria-label={`规则名称 ${number}`} value={rule.name} onChange={(event) => onUpdate(index, { name: event.target.value })} />
          </FormField>
          <FormField label={`启用状态 ${number}`}>
            <div className="flex h-[32px] items-center gap-[8px]">
              <Switch aria-label={`启用规则 ${number}`} checked={rule.enabled} onCheckedChange={(checked) => onUpdate(index, { enabled: checked })} />
              <span className="text-[12px] text-[#646b80]">{rule.enabled ? '已启用' : '已停用'}</span>
            </div>
          </FormField>
          <FormField label={`规则描述 ${number}`} className="md:col-span-2 xl:col-span-3">
            <Textarea value={rule.description} onChange={(event) => onUpdate(index, { description: event.target.value })} rows={2} />
          </FormField>
          <ListField label={`工作流节点 ${number}`} values={rule.workflow_nodes} onChange={(values) => onUpdate(index, { workflow_nodes: values })} />
          <ListField label={`信息域 ${number}`} values={rule.information_domains} onChange={(values) => onUpdate(index, { information_domains: values })} />
          <ListField label={`文档类型 ${number}`} values={rule.document_types} onChange={(values) => onUpdate(index, { document_types: values })} />
          <ListField label={`字段键 ${number}`} values={rule.field_keys} onChange={(values) => onUpdate(index, { field_keys: values })} />
          <FormField label={`执行级别 ${number}`}>
            <Select value={rule.execution_level} onValueChange={(value) => onUpdate(index, { execution_level: value as RuleExecutionLevel })}>
              <SelectTrigger aria-label={`执行级别 ${number}`} className={cn(SELECT_TRIGGER_CLASS, 'w-full')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="mandatory">强制</SelectItem>
                <SelectItem value="warning">警告</SelectItem>
                <SelectItem value="guidance">指引</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          <FormField label={`执行方式 ${number}`}>
            <Select value={rule.execution_method} onValueChange={(value) => onUpdate(index, { execution_method: value as RuleExecutionMethod })}>
              <SelectTrigger aria-label={`执行方式 ${number}`} className={cn(SELECT_TRIGGER_CLASS, 'w-full')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="deterministic">确定性</SelectItem>
                <SelectItem value="model_assisted">模型辅助</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          <FormField label={`条件操作符 ${number}`}>
            <Input value={String(rule.condition.operator ?? '')} onChange={(event) => onUpdateCondition(index, 'operator', event.target.value)} />
          </FormField>
          <FormField label={`条件字段 ${number}`}>
            <Input value={String(rule.condition.field_key ?? '')} onChange={(event) => onUpdateCondition(index, 'field_key', event.target.value)} />
          </FormField>
          <FormField label={`条件值 ${number}`}>
            <Input value={String(rule.condition.value ?? '')} onChange={(event) => onUpdateCondition(index, 'value', event.target.value)} />
          </FormField>
          <FormField label={`证据要求 ${number}`} className="md:col-span-2 xl:col-span-3">
            <DelimitedTextarea
              values={rule.evidence_requirements.map((item) => String(item.kind ?? '')).filter(Boolean)}
              onChange={(values) => onUpdate(index, {
                evidence_requirements: values.map((kind) => ({ kind })),
              })}
            />
          </FormField>
        </div>
      </CardContent>
    </Card>
  );
}

function PublishedRules({ rules }: { rules: EditableRule[] }) {
  return (
    <div className="flex flex-col gap-[12px]">
      <div className="rounded-[10px] border border-[#cfe0ff] bg-[#f1f6ff] px-[14px] py-[10px] text-[12px] text-[#155ec2]">
        已发布版本仅供查看
      </div>
      {rules.length === 0 ? (
        <Card className="grid min-h-[180px] place-items-center border-[#e9ecf3] shadow-none">
          <p className="text-[12px] text-[#858b9c]">该版本没有规则。</p>
        </Card>
      ) : rules.map((rule, index) => (
        <Card key={rule.clientId} className="border-[#e9ecf3] shadow-none">
          <CardContent className="p-[18px]">
            <div className="flex flex-wrap items-center justify-between gap-[8px]">
              <div>
                <p className="text-[11px] text-[#858b9c]">{rule.rule_key}</p>
                <h4 className="mt-[3px] text-[15px] font-medium text-[#252936]">{rule.name}</h4>
              </div>
              <span className={cn('rounded-full px-[10px] py-[4px] text-[11px]', rule.enabled ? 'bg-[#e9f7ef] text-[#018434]' : 'bg-[#f2f3f7] text-[#858b9c]')}>
                {rule.enabled ? '已启用' : '已停用'}
              </span>
            </div>
            {rule.description && <p className="mt-[10px] text-[12px] leading-[19px] text-[#646b80]">{rule.description}</p>}
            <dl className="mt-[14px] grid gap-[10px] text-[12px] sm:grid-cols-2 xl:grid-cols-3">
              <ReadOnlyField label="工作流节点" value={rule.workflow_nodes.join(', ')} />
              <ReadOnlyField label="信息域" value={rule.information_domains.join(', ')} />
              <ReadOnlyField label="文档类型" value={rule.document_types.join(', ')} />
              <ReadOnlyField label="字段键" value={rule.field_keys.join(', ')} />
              <ReadOnlyField label="执行级别" value={rule.execution_level} />
              <ReadOnlyField label="执行方式" value={rule.execution_method} />
              <ReadOnlyField label="条件操作符" value={String(rule.condition.operator ?? '')} />
              <ReadOnlyField label="条件字段" value={String(rule.condition.field_key ?? '')} />
              <ReadOnlyField label="条件值" value={String(rule.condition.value ?? '')} />
              <ReadOnlyField label="证据要求" value={rule.evidence_requirements.map((item) => String(item.kind ?? '')).filter(Boolean).join(', ')} />
            </dl>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function FormField({
  label,
  required = false,
  className,
  children,
}: {
  label: string;
  required?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <label className={cn('flex min-w-0 flex-col gap-[6px]', className)}>
      <span className="text-[12px] font-medium text-[#464c5e]">
        {label}{required ? <span aria-hidden="true" className="ml-[3px] text-[#d20b0b]">*</span> : null}
      </span>
      {children}
    </label>
  );
}

function ListField({ label, values, onChange }: { label: string; values: string[]; onChange: (values: string[]) => void }) {
  const [text, setText] = useState(() => values.join(', '));
  return (
    <FormField label={label}>
      <Input
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          onChange(splitList(event.target.value));
        }}
      />
    </FormField>
  );
}

function DelimitedTextarea({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  const [text, setText] = useState(() => values.join(', '));
  return (
    <Textarea
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        onChange(splitList(event.target.value));
      }}
      rows={2}
      placeholder="project_field, document"
    />
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-[8px] bg-[#f7f8fa] px-[10px] py-[8px]">
      <dt className="text-[10px] text-[#9299aa]">{label}</dt>
      <dd className="mt-[3px] break-words text-[#464c5e]">{value || '—'}</dd>
    </div>
  );
}
