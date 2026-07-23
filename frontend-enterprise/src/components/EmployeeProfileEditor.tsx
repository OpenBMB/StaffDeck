import { IdcardOutlined } from '../icons';
import { X as XIcon } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  Button as UIButton,
  Dialog,
  DialogContent,
  DialogTitle,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
  notify,
} from '@/components/ui';
import { SELECT_TRIGGER_CLASS } from '@/lib/enterprise-ui';
import { api, TENANT_ID } from '../api/client';
import type { EnterpriseAuthUser } from '../auth';
import { employeeDisplayName, employeeProfile } from '../employee';
import type { AgentProfileRead } from '../types';
import EmployeeAvatar from './EmployeeAvatar';

type EmployeeProfileFormValues = {
  name: string;
  roleName: string;
  onboardedAt: string;
  description: string;
  personaPrompt: string;
  systemPromptSummary: string;
  workStyles: string[];
  expertiseTags: string[];
  workModes: string[];
  status: 'active' | 'archived';
  publishedToGallery: boolean;
};

const STYLE_OPTIONS = ['目标明确', '证据优先', '动作可追溯', '事实先行', '流程推进', '风险克制', '及时追问'];
const EXPERTISE_OPTIONS = ['业务问答', 'SOP 执行', '工具调用', '代码检索', '报销核对', '事务跟进', '资料维护'];
const WORK_MODE_OPTIONS = ['识别意图', '补齐信息', '调用 SOP', '查询资料', '执行并复盘', '确认后执行', '必要时转人工'];

const BLANK_FORM: EmployeeProfileFormValues = {
  name: '',
  roleName: '',
  onboardedAt: '',
  description: '',
  personaPrompt: '',
  systemPromptSummary: '',
  workStyles: [],
  expertiseTags: [],
  workModes: [],
  status: 'active',
  publishedToGallery: false,
};

export default function EmployeeProfileEditor({
  agent,
  open,
  onClose,
  onSaved,
  currentUser,
}: {
  agent?: AgentProfileRead | null;
  open: boolean;
  onClose: () => void;
  onSaved?: (agent: AgentProfileRead) => void;
  currentUser?: EnterpriseAuthUser;
}) {
  const [form, setForm] = useState<EmployeeProfileFormValues>(BLANK_FORM);
  const [saving, setSaving] = useState(false);
  const profile = useMemo(() => employeeProfile(agent), [agent]);

  const update = (patch: Partial<EmployeeProfileFormValues>) => setForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    if (!open || !agent) return;
    setForm({
      name: employeeDisplayName(agent),
      roleName: profile.roleName === '待补充岗位' ? '' : profile.roleName,
      onboardedAt: profile.onboardedAt === '-' ? new Date().toISOString().slice(0, 10) : profile.onboardedAt,
      description: agent.description || '',
      personaPrompt: agent.persona_prompt || '',
      systemPromptSummary: typeof agent.metadata?.system_prompt_summary === 'string' ? agent.metadata.system_prompt_summary : '',
      workStyles: profile.workStyles,
      expertiseTags: profile.expertiseTags,
      workModes: profile.workModes,
      status: agent.status === 'archived' ? 'archived' : 'active',
      publishedToGallery: agent.metadata?.published_to_gallery === true,
    });
  }, [agent, open, profile]);

  async function save() {
    if (!agent) return;
    if (!form.name.trim()) {
      notify.error('请输入数字员工姓名');
      return;
    }
    setSaving(true);
    try {
      const wasPublished = agent.metadata?.published_to_gallery === true;
      const metadata: Record<string, unknown> = {
        ...(agent.metadata || {}),
        blank_onboarding: false,
        role_name: form.roleName.trim() || '待补充岗位',
        onboarded_at: form.onboardedAt || new Date().toISOString().slice(0, 10),
        system_prompt_summary: form.systemPromptSummary.trim(),
        work_styles: compactTags(form.workStyles),
        expertise_tags: compactTags(form.expertiseTags),
        work_modes: compactTags(form.workModes),
        published_to_gallery: form.publishedToGallery,
      };
      if (form.publishedToGallery && !wasPublished) {
        metadata.gallery_published_at = new Date().toISOString();
        metadata.gallery_published_by = currentUser?.username;
      }
      if (!form.publishedToGallery) {
        delete metadata.gallery_published_at;
        delete metadata.gallery_published_by;
      }

      const saved = await api.put<AgentProfileRead>(`/api/enterprise/agents/${agent.id}`, {
        tenant_id: TENANT_ID,
        name: form.name.trim(),
        description: form.description.trim(),
        persona_prompt: form.personaPrompt.trim(),
        status: form.status,
        metadata,
      });
      notify.success('数字员工档案已更新');
      onSaved?.(saved);
      onClose();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存数字员工档案失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !saving) onClose(); }}>
      <DialogContent
        aria-describedby={undefined}
        className="employee-profile-modal flex max-h-[calc(100dvh-4rem)] w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[860px]"
      >
        <DialogTitle className="px-[12px] text-[14px] font-normal leading-none text-[#757f9c]">
          {agent ? `编辑数字员工档案：${employeeDisplayName(agent)}` : '编辑数字员工档案'}
        </DialogTitle>

        <div className="min-h-0 flex-1 overflow-y-auto px-[12px]">
          <div className="employee-profile-editor">
            <div className="employee-profile-preview">
              <EmployeeAvatar agent={agent} size={92} />
              <div>
                <span className="m-0 block text-[12px] text-muted-foreground">数字员工档案</span>
                <h4 className="mt-[4px] mb-[6px] text-[18px] font-semibold text-[#18181a]">{agent ? employeeDisplayName(agent) : '数字员工'}</h4>
                <span className="m-0 block text-[12px] text-muted-foreground">{profile.roleName}</span>
              </div>
              <span className="employee-profile-preview-icon"><IdcardOutlined /></span>
            </div>

            <div className="employee-profile-form flex flex-col gap-[14px]">
              <div className="employee-profile-form-grid">
                <LabeledField label="数字员工姓名">
                  <Input value={form.name} placeholder="例如：默认员工" onChange={(event) => update({ name: event.target.value })} />
                </LabeledField>
                <LabeledField label="岗位">
                  <Input value={form.roleName} placeholder="例如：研发" onChange={(event) => update({ roleName: event.target.value })} />
                </LabeledField>
                <LabeledField label="入职时间">
                  <Input type="date" value={form.onboardedAt} onChange={(event) => update({ onboardedAt: event.target.value })} />
                </LabeledField>
                <LabeledField label="工作状态">
                  <Select value={form.status} onValueChange={(value) => update({ status: value as 'active' | 'archived' })}>
                    <SelectTrigger className={`${SELECT_TRIGGER_CLASS} w-full`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="active">在线</SelectItem>
                      <SelectItem value="archived">下线</SelectItem>
                    </SelectContent>
                  </Select>
                </LabeledField>
              </div>

              <LabeledField label="岗位描述">
                <Textarea rows={3} value={form.description} placeholder="概括这个数字员工的岗位边界、服务风格和执行重点" onChange={(event) => update({ description: event.target.value })} />
              </LabeledField>
              <LabeledField label="看板摘要">
                <Textarea rows={2} value={form.systemPromptSummary} placeholder="用于数字员工档案页顶部展示的 system prompt 摘要" onChange={(event) => update({ systemPromptSummary: event.target.value })} />
              </LabeledField>
              <LabeledField label="岗位执行约束">
                <Textarea rows={4} value={form.personaPrompt} placeholder="员工在对话中的角色、人设、回复风格和执行边界" onChange={(event) => update({ personaPrompt: event.target.value })} />
              </LabeledField>

              <div className="employee-profile-form-grid is-tags">
                <LabeledField label="掌握方向">
                  <TagsField value={form.expertiseTags} options={EXPERTISE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ expertiseTags: next })} />
                </LabeledField>
                <LabeledField label="工作风格">
                  <TagsField value={form.workStyles} options={STYLE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ workStyles: next })} />
                </LabeledField>
                <LabeledField label="工作模式">
                  <TagsField value={form.workModes} options={WORK_MODE_OPTIONS} placeholder="输入后回车添加" onChange={(next) => update({ workModes: next })} />
                </LabeledField>
              </div>

              <div className="employee-profile-publish">
                <div>
                  <strong className="text-[13px] text-[#18181a]">发布到广场</strong>
                  <p className="m-0 mt-[4px] text-[12px] text-muted-foreground">
                    开启后，其他账号可以在对话端和数字员工广场中选择这个员工。
                  </p>
                </div>
                <Switch checked={form.publishedToGallery} onCheckedChange={(next) => update({ publishedToGallery: next })} />
              </div>
            </div>
          </div>
        </div>

        <div className="flex items-center justify-end gap-[8px] px-[12px]">
          <UIButton
            variant="outline"
            disabled={saving}
            onClick={onClose}
            className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
          >
            取消
          </UIButton>
          <UIButton
            disabled={saving}
            onClick={() => void save()}
            className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
          >
            保存
          </UIButton>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function LabeledField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}

function TagsField({
  value,
  options,
  placeholder,
  onChange,
}: {
  value: string[];
  options: string[];
  placeholder?: string;
  onChange: (next: string[]) => void;
}) {
  const [draft, setDraft] = useState('');
  const addTags = (raw: string) => {
    const parts = raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    if (parts.length) onChange(Array.from(new Set([...value, ...parts])));
    setDraft('');
  };
  const removeTag = (tag: string) => onChange(value.filter((item) => item !== tag));
  const suggestions = options.filter((item) => !value.includes(item));

  return (
    <div className="flex flex-col gap-[8px]">
      <div className="flex min-h-[34px] flex-wrap items-center gap-[6px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[8px] py-[5px] transition-colors focus-within:border-[#18181a]">
        {value.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-[4px] rounded-[6px] bg-[#f2f3f7] px-[8px] py-[2px] text-[12px] text-[#18181a]"
          >
            {tag}
            <button
              type="button"
              aria-label={`移除 ${tag}`}
              onClick={() => removeTag(tag)}
              className="grid place-items-center text-[#858b9c] hover:text-[#18181a]"
            >
              <XIcon className="size-[12px]" />
            </button>
          </span>
        ))}
        <input
          autoComplete="off"
          data-1p-ignore="true"
          data-lpignore="true"
          data-bwignore="true"
          value={draft}
          placeholder={value.length ? '' : placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
              event.preventDefault();
              addTags(draft);
            } else if (event.key === 'Backspace' && !draft && value.length) {
              removeTag(value[value.length - 1]);
            }
          }}
          onBlur={() => draft.trim() && addTags(draft)}
          className="h-[22px] min-w-[80px] flex-1 bg-transparent text-[12px] text-[#17191f] outline-none placeholder:text-[#c0c6d4]"
        />
      </div>
      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-[6px]">
          {suggestions.map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => addTags(item)}
              className="rounded-[6px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px] text-[12px] text-[#858b9c] hover:border-[#18181a] hover:text-[#18181a]"
            >
              + {item}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function compactTags(values: string[] | undefined): string[] {
  return Array.from(new Set((values || []).map((item) => item.trim()).filter(Boolean))).slice(0, 12);
}
