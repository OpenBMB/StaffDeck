import { SaveOutlined, UserOutlined } from '@ant-design/icons';
import { Button, Card, Form, Input, InputNumber, Switch, Typography, message } from 'antd';
import { useEffect, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import type { AgentProfileRead, PersonaRead, UIConfigRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

function formatDateOnly(value: string): string {
  const normalized = /(?:z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return value.slice(0, 10);
  }
  return date.toISOString().slice(0, 10);
}

export default function PersonaPage() {
  const [form] = Form.useForm();
  const [uiForm] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [uiLoading, setUiLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [uiUpdatedAt, setUiUpdatedAt] = useState('');
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId) || null;
  const isOverallPersona = !selectedAgent || selectedAgent.is_overall;

  useEffect(() => {
    void loadPersonaScope();
    api
      .get<UIConfigRead>(`/api/enterprise/ui-config?tenant_id=${TENANT_ID}`)
      .then((row) => {
        uiForm.setFieldsValue(row);
        setUiUpdatedAt(row.updated_at);
      })
      .catch((error) => message.error(error.message));
  }, [uiForm]);

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
            form.setFieldsValue({
              agent_name: agent.name,
              agent_description: agent.description || '',
              system_prompt: agent.persona_prompt || row.system_prompt,
            });
            setUpdatedAt(agent.updated_at || row.updated_at);
          })
          .catch((error) => message.error(error.message));
        return;
      }
      form.setFieldsValue({
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
        form.setFieldsValue({ system_prompt: row.system_prompt });
        setUpdatedAt(row.updated_at);
      })
      .catch((error) => message.error(error.message));
  }, [agents, form, selectedAgentId]);

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
      message.error(error instanceof Error ? error.message : '加载员工域失败');
    }
  }

  async function save() {
    setLoading(true);
    try {
      const values = await form.validateFields();
      if (selectedAgent) {
        const row = await api.put<AgentProfileRead>(`/api/enterprise/agents/${selectedAgent.id}`, {
          tenant_id: TENANT_ID,
          name: values.agent_name,
          description: values.agent_description,
          persona_prompt: values.system_prompt,
          status: selectedAgent.status,
        });
        setAgents((prev) => prev.map((item) => (item.id === row.id ? { ...row, resources: item.resources } : item)));
        setUpdatedAt(row.updated_at);
        if (row.is_overall) {
          await api.put<PersonaRead>('/api/enterprise/persona', {
            tenant_id: TENANT_ID,
            system_prompt: values.system_prompt,
          });
        }
        window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: row.id } }));
        message.success('岗位人设已保存');
      } else {
        const row = await api.put<PersonaRead>('/api/enterprise/persona', {
          tenant_id: TENANT_ID,
          system_prompt: values.system_prompt,
        });
        setUpdatedAt(row.updated_at);
        message.success('组织默认岗位人设已保存');
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setLoading(false);
    }
  }

  async function saveUiConfig() {
    setUiLoading(true);
    try {
      const values = await uiForm.validateFields();
      const row = await api.put<UIConfigRead>('/api/enterprise/ui-config', {
        tenant_id: TENANT_ID,
        show_thinking_trace: values.show_thinking_trace,
        show_skill_trace: values.show_skill_trace,
        show_tool_trace: values.show_tool_trace,
        reflection_max_rounds: values.reflection_max_rounds,
        agent_loop_max_actions: values.agent_loop_max_actions,
      });
      setUiUpdatedAt(row.updated_at);
      message.success('展示设置已保存');
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setUiLoading(false);
    }
  }

  return (
    <>
      <div className="page-title">
        <div>
          <Typography.Title level={3}>岗位人设</Typography.Title>
        </div>
        <Button type="primary" icon={<SaveOutlined />} loading={loading} onClick={save}>保存</Button>
      </div>
      <Card className="editor-card" title={<><UserOutlined /> 岗位人设</>}>
        <Form form={form} layout="vertical">
          <Form.Item name="agent_name" label="名称" rules={[{ required: true }]}>
            <Input placeholder="员工姓名" />
          </Form.Item>
          <Form.Item name="agent_description" label="描述">
            <Input.TextArea rows={2} placeholder="员工岗位人设摘要" />
          </Form.Item>
          <Form.Item name="system_prompt" label="岗位 Prompt" rules={[{ required: true }]}>
            <Input.TextArea
              className="persona-editor"
              rows={12}
              placeholder={isOverallPersona ? '输入组织默认岗位人设' : '输入仅当前员工可见的岗位人设'}
            />
          </Form.Item>
        </Form>
        {updatedAt && <Typography.Text type="secondary">最后更新：{formatDateOnly(updatedAt)}</Typography.Text>}
      </Card>
      <Card className="editor-card settings-card" title="执行记录与展示设置">
        <Form
          form={uiForm}
          layout="vertical"
          initialValues={{
            show_thinking_trace: true,
            show_skill_trace: true,
            show_tool_trace: true,
            reflection_max_rounds: 1,
            agent_loop_max_actions: 6,
          }}
        >
          <Form.Item
            name="show_thinking_trace"
            label="展示思考状态"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="show_skill_trace"
            label="展示执行技能"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="show_tool_trace"
            label="展示工具调用"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="reflection_max_rounds"
            label="反思轮数"
            tooltip="设为 0 时关闭反思；每轮允许模型检查当前技能和工具结果，并决定是否重试其他技能或工具。"
            rules={[{ required: true, type: 'number', min: 0, max: 5 }]}
          >
            <InputNumber min={0} max={5} step={1} precision={0} />
          </Form.Item>
          <Form.Item
            name="agent_loop_max_actions"
            label="单轮最大动作数"
            tooltip="控制一次用户输入内员工可连续决策和调用工具的最大次数，用于避免无限循环。"
            rules={[{ required: true, type: 'number', min: 1, max: 20 }]}
          >
            <InputNumber min={1} max={20} step={1} precision={0} />
          </Form.Item>
          <Button type="primary" icon={<SaveOutlined />} loading={uiLoading} onClick={saveUiConfig}>
            保存设置
          </Button>
        </Form>
        {uiUpdatedAt && <Typography.Text type="secondary">最后更新：{formatDateOnly(uiUpdatedAt)}</Typography.Text>}
      </Card>
    </>
  );
}
