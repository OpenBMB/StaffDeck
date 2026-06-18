import {
  DeleteOutlined,
  MoreOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { Avatar, Button, Card, Dropdown, Modal, Space, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from '../api/client';
import { employeeDisplayName, employeeProfile, resourceCount } from '../employee';
import type { AgentProfileRead } from '../types';

const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentProfileRead[]>([]);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  async function load() {
    setLoading(true);
    try {
      const rows = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
      setAgents(rows);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载员工失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const overallAgent = agents.find((item) => item.is_overall);
  const employees = useMemo(() => agents.filter((item) => !item.is_overall), [agents]);

  function selectEmployee(row: AgentProfileRead) {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, row.id);
    window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: row.id } }));
    navigate('/enterprise/dashboard');
  }

  async function updateStatus(row: AgentProfileRead, status: 'active' | 'archived') {
    try {
      await api.put<AgentProfileRead>(`/api/enterprise/agents/${row.id}`, {
        tenant_id: TENANT_ID,
        status,
        metadata: row.metadata || {},
      });
      message.success(status === 'active' ? '员工已上线' : '员工已下线');
      await load();
      window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
    } catch (error) {
      message.error(error instanceof Error ? error.message : '更新员工状态失败');
    }
  }

  function deleteEmployee(row: AgentProfileRead) {
    Modal.confirm({
      title: `删除员工「${employeeDisplayName(row)}」？`,
      content: '删除后会移除该员工的资料、SOP 和技能绑定；组织资源库不受影响。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          await api.delete(`/api/enterprise/agents/${row.id}?tenant_id=${TENANT_ID}`);
          if (window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) === row.id && overallAgent) {
            window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, overallAgent.id);
            window.dispatchEvent(new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId: overallAgent.id } }));
          }
          message.success('员工已删除');
          await load();
          window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
        } catch (error) {
          message.error(error instanceof Error ? error.message : '删除员工失败');
        }
      },
    });
  }

  return (
    <div className="page agents-page">
      <div className="page-title">
        <div>
          <Typography.Title level={2}>员工名册</Typography.Title>
          <Typography.Paragraph type="secondary">查看每位数字员工的岗位、掌握能力和可用资源，点击员工进入个人看板。</Typography.Paragraph>
        </div>
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      </div>

      <div className="agents-summary-grid">
        <Card className="agent-summary-card">
          <span>组织资源库</span>
          <strong>{overallAgent ? '组织资源库' : '-'}</strong>
          <small>资料、SOP、技能和工具的组织级底座</small>
        </Card>
        <Card className="agent-summary-card">
          <span>员工总数</span>
          <strong>{employees.length}</strong>
          <small>{employees.filter((item) => item.status === 'active').length} 位在线</small>
        </Card>
        <Card className="agent-summary-card">
          <span>下线员工</span>
          <strong>{employees.filter((item) => item.status !== 'active').length}</strong>
          <small>下线后任务派发台不可选择</small>
        </Card>
      </div>

      <div className="employee-roster-grid">
        {employees.map((employee) => (
          <EmployeeCard
            key={employee.id}
            employee={employee}
            onOpen={() => selectEmployee(employee)}
            onStatus={(status) => void updateStatus(employee, status)}
            onDelete={() => deleteEmployee(employee)}
          />
        ))}
      </div>
    </div>
  );
}

function EmployeeCard({
  employee,
  onOpen,
  onStatus,
  onDelete,
}: {
  employee: AgentProfileRead;
  onOpen: () => void;
  onStatus: (status: 'active' | 'archived') => void;
  onDelete: () => void;
}) {
  const profile = employeeProfile(employee);
  const sopCount = resourceCount(employee.resources, 'skill');
  const skillCount = resourceCount(employee.resources, 'general_skill');
  const kbCount = resourceCount(employee.resources, 'knowledge_base');
  return (
    <Card className="employee-roster-card" hoverable onClick={onOpen}>
      <div className="employee-roster-head">
        <Avatar className={`employee-avatar tone-${profile.avatarTone}`} size={54}>{profile.avatarText}</Avatar>
        <div className="employee-roster-title">
          <strong>{employeeDisplayName(employee)}</strong>
          <span>{profile.roleName}</span>
        </div>
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              employee.status === 'active'
                ? { key: 'archive', icon: <PauseCircleOutlined />, label: '下线' }
                : { key: 'active', icon: <PlayCircleOutlined />, label: '上线' },
              { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
            ],
            onClick: ({ key, domEvent }) => {
              domEvent.stopPropagation();
              if (key === 'active') onStatus('active');
              if (key === 'archive') onStatus('archived');
              if (key === 'delete') onDelete();
            },
          }}
        >
          <Button
            type="text"
            icon={<MoreOutlined />}
            aria-label="员工操作"
            onClick={(event) => event.stopPropagation()}
          />
        </Dropdown>
      </div>
      <Typography.Paragraph ellipsis={{ rows: 2 }}>
        {employee.description || '负责接收任务、调用资料和 SOP 完成企业服务。'}
      </Typography.Paragraph>
      <Space wrap className="employee-roster-tags">
        <Tag color={employee.status === 'active' ? 'green' : 'default'}>{employee.status === 'active' ? '在线' : '下线'}</Tag>
        <Tag>SOP {sopCount}</Tag>
        <Tag>技能 {skillCount}</Tag>
        <Tag>资料 {kbCount}</Tag>
      </Space>
      <div className="employee-roster-styles">
        {profile.workStyles.slice(0, 3).map((item) => <span key={item}>{item}</span>)}
      </div>
    </Card>
  );
}
