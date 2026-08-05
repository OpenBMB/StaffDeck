import { SaveOutlined, UserOutlined } from '../icons';
import { useEffect, useState, type ReactNode } from 'react';
import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Textarea,
  notify,
} from '@/components/ui';
import { api, TENANT_ID } from '../api/client';
import type { AgentProfileRead, PersonaRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type PersonaForm = {
  agent_name: string;
  agent_description: string;
  system_prompt: string;
};

const BLANK_PERSONA: PersonaForm = { agent_name: '', agent_description: '', system_prompt: '' };

function formatDateOnly(value: string): string {
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return date.toISOString().slice(0, 10);
}

export default function PersonaPage() {
  const [form, setForm] = useState<PersonaForm>(BLANK_PERSONA);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) || null;
  const isOverallPersona = !selectedAgent || selectedAgent.is_overall;

  const updatePersona = (patch: Partial<PersonaForm>) => setForm((prev) => ({ ...prev, ...patch }));

  useEffect(() => {
    void loadPersonaScope();
  }, []);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const agentId = (event as CustomEvent<{ agentId?: string }>).detail?.agentId || '';
      if (agentId) setSelectedAgentId(agentId);
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    const agent = agents.find((item) => item.id === selectedAgentId);
    if (agent) {
      if (agent.is_overall) {
        api
          .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${TENANT_ID}`)
          .then((row) => {
            setForm({
              agent_name: agent.name,
              agent_description: agent.description || '',
              system_prompt: agent.persona_prompt || row.system_prompt,
            });
            setUpdatedAt(agent.updated_at || row.updated_at);
          })
          .catch((error) => notify.error(error.message));
        return;
      }
      setForm({
        agent_name: agent.name,
        agent_description: agent.description || '',
        system_prompt: agent.persona_prompt || '',
      });
      setUpdatedAt(agent.updated_at);
      return;
    }
    api
      .get<PersonaRead>(`/api/enterprise/persona?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setForm((prev) => ({ ...prev, system_prompt: row.system_prompt }));
        setUpdatedAt(row.updated_at);
      })
      .catch((error) => notify.error(error.message));
  }, [agents, selectedAgentId]);

  async function loadPersonaScope() {
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
      setSelectedAgentId((current) => {
        const stored = window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY);
        const candidate = current || stored || '';
        if (candidate && rows.some((agent) => agent.id === candidate)) return candidate;
        return rows.find((agent) => agent.is_overall)?.id || rows[0]?.id || '';
      });
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '加载员工域失败');
    }
  }

  async function save() {
    if (!form.system_prompt.trim() || (selectedAgent && !form.agent_name.trim())) {
      notify.error('请填写必填项');
      return;
    }
    setLoading(true);
    try {
      if (selectedAgent) {
        const row = await api.put<AgentProfileRead>(`/api/enterprise/agents/${selectedAgent.id}`, {
          tenant_id: TENANT_ID,
          name: form.agent_name,
          description: form.agent_description,
          persona_prompt: form.system_prompt,
          status: selectedAgent.status,
        });
        setAgents((prev) => prev.map((item) => (item.id === row.id ? { ...row, resources: item.resources } : item)));
        setUpdatedAt(row.updated_at);
        if (row.is_overall) {
          await api.put<PersonaRead>('/api/enterprise/persona', {
            tenant_id: TENANT_ID,
            system_prompt: form.system_prompt,
          });
        }
        window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: row.id } }));
        notify.success('岗位人设已保存');
      } else {
        const row = await api.put<PersonaRead>('/api/enterprise/persona', {
          tenant_id: TENANT_ID,
          system_prompt: form.system_prompt,
        });
        setUpdatedAt(row.updated_at);
        notify.success('组织默认岗位人设已保存');
      }
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div>
          <h3>岗位人设</h3>
        </div>
        <UIButton disabled={loading} onClick={() => void save()}>
          <SaveOutlined />
          保存
        </UIButton>
      </div>
      <Card className="editor-card">
        <CardHeader>
          <CardTitle className="flex items-center gap-[6px]"><UserOutlined /> 岗位人设</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[14px]">
          <LabeledField label="名称">
            <Input value={form.agent_name} placeholder="数字员工姓名" onChange={(event) => updatePersona({ agent_name: event.target.value })} />
          </LabeledField>
          <LabeledField label="描述">
            <Textarea rows={2} value={form.agent_description} placeholder="员工岗位描述" onChange={(event) => updatePersona({ agent_description: event.target.value })} />
          </LabeledField>
          <LabeledField label="岗位 Prompt">
            <Textarea
              className="persona-editor"
              rows={12}
              value={form.system_prompt}
              placeholder={isOverallPersona ? '输入组织默认岗位人设' : '输入仅当前员工可见的岗位人设'}
              onChange={(event) => updatePersona({ system_prompt: event.target.value })}
            />
          </LabeledField>
          {updatedAt && <span className="text-[12px] text-muted-foreground">最后更新：{formatDateOnly(updatedAt)}</span>}
        </CardContent>
      </Card>
    </>
  );
}

function LabeledField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
      {children}
    </label>
  );
}
