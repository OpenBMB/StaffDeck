import {
  CheckCircleOutlined,
  CloudOutlined,
  CloseCircleOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
  UploadOutlined,
} from '@ant-design/icons';
import { Button, Card, Empty, Input, Select, Space, Tabs, Tag, Typography, Upload, message } from 'antd';
import type { UploadFile } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api, streamPost, TENANT_ID } from '../api/client';
import CodeBlock from '../components/CodeBlock';
import type { GeneralSkillRead, GeneralSkillRunResponse } from '../types';

const DEFAULT_MARKDOWN = `# 技能说明

这里粘贴任意格式的通用技能文档。系统不会从文档中自动抽取名称、Slug 或描述；这些信息由上方表单维护。`;

const DEFAULT_GENERAL_META = {
  name: '中国城市天气',
  slug: 'weather-zh',
  description: '中国城市天气查询工具',
  homepage: 'https://www.weather.com.cn/',
};

const PHASE_LABELS: Record<string, string> = {
  skill_loaded: '加载技能',
  planning: '生成执行方案',
  plan_created: '生成代码',
  attempt_started: '开始运行',
  running_code: '运行代码',
  stdout_chunk: '运行输出',
  stderr_chunk: '错误输出',
  code_finished: '读取运行结果',
  code_timeout: '运行超时',
  reflection_passed: '校验通过',
  reflection_retrying: '反思修复',
  reflection_stopped: '停止重试',
  repair_planning: '重新生成代码',
  repair_failed: '修复失败',
  plan_failed: '生成失败',
  replying: '生成回复',
  reply_created: '完成回复',
  reply_failed: '回复失败',
};

function formatJson(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value === 'string') {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function traceDetail(item: Record<string, unknown>): string {
  return [
    item.rationale,
    item.expected_output,
    item.phase === 'code_finished' ? item.stdout_preview : undefined,
    item.phase === 'code_finished' || item.phase === 'code_timeout' ? item.stderr_preview : undefined,
    item.run_id,
  ]
    .filter((value) => typeof value === 'string' && value.trim())
    .map(String)
    .join('\n');
}

function traceItemCode(item: Record<string, unknown>): string {
  return typeof item.code === 'string' && item.code.trim() ? item.code : '';
}

function resultSucceeded(result: Partial<GeneralSkillRunResponse> | null): boolean {
  if (!result) return false;
  const success = result.structured_result?.success;
  return success !== false && !result.stderr;
}

export default function GeneralSkillsPage({ embedded = false }: { embedded?: boolean }) {
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [markdown, setMarkdown] = useState(DEFAULT_MARKDOWN);
  const [skillName, setSkillName] = useState(DEFAULT_GENERAL_META.name);
  const [skillSlug, setSkillSlug] = useState(DEFAULT_GENERAL_META.slug);
  const [skillDescription, setSkillDescription] = useState(DEFAULT_GENERAL_META.description);
  const [skillHomepage, setSkillHomepage] = useState(DEFAULT_GENERAL_META.homepage);
  const [selectedSlug, setSelectedSlug] = useState<string>();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [query, setQuery] = useState('北京今天天气怎么样');
  const [runResult, setRunResult] = useState<GeneralSkillRunResponse | null>(null);
  const [liveResult, setLiveResult] = useState<Partial<GeneralSkillRunResponse> | null>(null);
  const [loading, setLoading] = useState(false);

  const selectedSkill = useMemo(
    () => rows.find((row) => row.slug === selectedSlug) || rows[0],
    [rows, selectedSlug],
  );
  const activeResult = runResult || liveResult;

  const load = () =>
    api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setRows(items);
        if (!selectedSlug && items.length) {
          setSelectedSlug(items[0].slug);
          setEditingSlug(items[0].slug);
          setMarkdown(items[0].skill_markdown);
          setSkillName(items[0].name);
          setSkillSlug(items[0].slug);
          setSkillDescription(items[0].description || '');
          setSkillHomepage(items[0].homepage || '');
        }
      })
      .catch((error) => message.error(error.message));

  useEffect(() => {
    load();
  }, []);

  async function importSkill() {
    if (!markdown.trim()) {
      message.warning('请先粘贴或上传 SKILL.md');
      return;
    }
    if (!skillName.trim() || !skillSlug.trim()) {
      message.warning('请填写通用技能名称和 Slug');
      return;
    }
    const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import', {
      tenant_id: TENANT_ID,
      name: skillName.trim(),
      slug: skillSlug.trim(),
      description: skillDescription.trim() || undefined,
      homepage: skillHomepage.trim() || undefined,
      markdown,
      status: 'published',
      original_slug: editingSlug || undefined,
    });
    message.success(editingSlug ? `已保存 ${row.name}` : `已导入 ${row.name}`);
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    load();
  }

  function newSkill() {
    setMarkdown(DEFAULT_MARKDOWN);
    setSkillName('');
    setSkillSlug('');
    setSkillDescription('');
    setSkillHomepage('');
    setEditingSlug(null);
    setRunResult(null);
  }

  function editSkill(row: GeneralSkillRead) {
    setMarkdown(row.skill_markdown);
    setSkillName(row.name);
    setSkillSlug(row.slug);
    setSkillDescription(row.description || '');
    setSkillHomepage(row.homepage || '');
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    setRunResult(null);
  }

  async function runSkill() {
    const slug = selectedSkill?.slug;
    if (!slug) {
      message.warning('请先导入通用技能');
      return;
    }
    if (!query.trim()) {
      message.warning('请输入测试问题');
      return;
    }
    setLoading(true);
    setRunResult(null);
    setLiveResult({
      skill_slug: slug,
      execution_trace: [],
      generated_code: '',
      stdout: '',
      stderr: '',
      structured_result: {},
      reply: '',
    });
    try {
      let completed = false;
      await streamPost(
        `/api/enterprise/general-skills/${slug}/run/stream`,
        {
          tenant_id: TENANT_ID,
          user_id: 'enterprise_demo',
          query,
          max_attempts: 10,
        },
        (item) => {
          if (item.event === 'trace') {
            const traceItem = item.data;
            setLiveResult((current) => {
              const previous = current || { skill_slug: slug, execution_trace: [] };
              const executionTrace = [...(previous.execution_trace || []), traceItem];
              const nextCode = typeof traceItem.code === 'string' && traceItem.code.trim()
                ? traceItem.code
                : previous.generated_code || '';
              const nextStructured = typeof traceItem.structured_result === 'object' && traceItem.structured_result
                ? traceItem.structured_result as Record<string, unknown>
                : previous.structured_result || {};
              const chunk = typeof traceItem.text === 'string' ? traceItem.text : '';
              const phase = typeof traceItem.phase === 'string' ? traceItem.phase : '';
              return {
                ...previous,
                execution_trace: executionTrace,
                generated_code: nextCode,
                stdout: phase === 'stdout_chunk'
                  ? `${previous.stdout || ''}${chunk}`
                  : typeof traceItem.stdout_preview === 'string' ? traceItem.stdout_preview : previous.stdout || '',
                stderr: phase === 'stderr_chunk'
                  ? `${previous.stderr || ''}${chunk}`
                  : typeof traceItem.stderr_preview === 'string' ? traceItem.stderr_preview : previous.stderr || '',
                structured_result: nextStructured,
              };
            });
          }
          if (item.event === 'complete') {
            const result = item.data as unknown as GeneralSkillRunResponse;
            completed = true;
            setRunResult(result);
            setLiveResult(null);
            message.success('运行完成');
          }
          if (item.event === 'error') {
            const text = typeof item.data.message === 'string' ? item.data.message : '运行失败';
            setLiveResult((current) => ({
              ...(current || { skill_slug: slug, execution_trace: [] }),
              stderr: text,
              structured_result: { success: false, error: text },
              reply: '运行失败',
            }));
            message.error(text);
          }
        },
      );
      if (!completed) {
        message.warning('运行流已结束，但未收到最终结果');
      }
    } catch (error) {
      message.error(error instanceof Error ? error.message : '运行失败');
    } finally {
      setLoading(false);
    }
  }

  async function beforeUpload(file: UploadFile | File) {
    const target = file as File;
    const text = await target.text();
    setMarkdown(text);
    message.success(`已读取 ${target.name}`);
    return false;
  }

  const isLiveRunning = loading && !runResult;

  return (
    <>
      {!embedded && (
        <div className="page-title">
          <Typography.Title level={3}>通用技能 Demo</Typography.Title>
          <Typography.Text type="secondary">导入 PilotDeck 风格 SKILL.md，验证模型选择、代码生成与运行链路。</Typography.Text>
        </div>
      )}
      <div className="general-skill-workbench">
        <Space direction="vertical" size={16} className="general-skill-main">
          <Card
            className="editor-card general-skill-editor"
            title={(
              <Space>
                <FileTextOutlined />
                <span>{editingSlug ? `编辑通用技能：${editingSlug}` : '新增通用技能'}</span>
              </Space>
            )}
            extra={(
              <Space wrap>
                <Button onClick={newSkill}>新建</Button>
                <Upload beforeUpload={beforeUpload} showUploadList={false} accept=".md,.txt">
                  <Button icon={<UploadOutlined />}>选择文件</Button>
                </Upload>
                <Button type="primary" icon={<CloudOutlined />} onClick={importSkill}>保存并发布</Button>
              </Space>
            )}
          >
            <div className="general-skill-meta-form">
              <Input
                value={skillName}
                onChange={(event) => setSkillName(event.target.value)}
                placeholder="技能名称，由用户填写"
              />
              <Input
                value={skillSlug}
                onChange={(event) => setSkillSlug(event.target.value)}
                placeholder="Slug，由用户填写，用于路由和接口路径"
              />
              <Input
                value={skillDescription}
                onChange={(event) => setSkillDescription(event.target.value)}
                placeholder="描述，用于模型选择技能"
              />
              <Input
                value={skillHomepage}
                onChange={(event) => setSkillHomepage(event.target.value)}
                placeholder="主页或参考链接，可选"
              />
            </div>
            <Input.TextArea
              className="general-skill-source-input"
              value={markdown}
              onChange={(event) => setMarkdown(event.target.value)}
              rows={20}
              spellCheck={false}
            />
          </Card>
          <Card
            className="editor-card general-skill-run-card"
            title="运行测试"
            extra={<Button type="primary" loading={loading} icon={<ExperimentOutlined />} onClick={runSkill}>运行</Button>}
          >
            <div className="general-run-form">
              <Select
                value={selectedSkill?.slug}
                placeholder="选择通用技能"
                options={rows.map((row) => ({ value: row.slug, label: `${row.name} / ${row.slug}` }))}
                onChange={setSelectedSlug}
              />
              <Input value={query} onChange={(event) => setQuery(event.target.value)} />
            </div>
          </Card>
          <Card
            className="editor-card general-result-card"
            title={(
              <Space>
                <PlayCircleOutlined />
                <span>运行结果</span>
                {activeResult && (
                  isLiveRunning
                    ? <Tag color="processing">运行中</Tag>
                    : resultSucceeded(activeResult)
                    ? <Tag color="green" icon={<CheckCircleOutlined />}>成功</Tag>
                    : <Tag color="red" icon={<CloseCircleOutlined />}>失败</Tag>
                )}
              </Space>
            )}
          >
            {activeResult ? (
              <div className="general-result-layout">
                {(() => {
                  const traceItems = activeResult.execution_trace || [];
                  const latestCodeIndex = traceItems.reduce(
                    (latest, traceItem, traceIndex) => (traceItemCode(traceItem) ? traceIndex : latest),
                    -1,
                  );
                  return (
                    <>
                <section className="general-reply-panel">
                  <div className="general-section-label">最终回复</div>
                  <Typography.Paragraph className="result-reply">
                    {activeResult.reply || (loading ? '正在运行通用技能...' : '暂无回复')}
                  </Typography.Paragraph>
                </section>

                <section>
                  <div className="general-section-label">执行流程</div>
                  <div className="general-trace-list">
                    {traceItems.map((item, index) => {
                      const phase = typeof item.phase === 'string' ? item.phase : '';
                      const detail = traceDetail(item);
                      const code = traceItemCode(item);
                      const codeTitle = typeof item.attempt === 'number'
                        ? `第 ${item.attempt} 次 Python runner`
                        : 'Python runner';
                      return (
                        <div className="general-trace-item" key={`${phase || 'phase'}-${index}`}>
                          <div className="general-trace-dot" />
                          <div>
                            <div className="general-trace-title">{PHASE_LABELS[phase] || String(item.message || phase || '执行')}</div>
                            <div className="general-trace-message">{String(item.message || '')}</div>
                            {detail && <pre className="general-trace-detail">{detail}</pre>}
                            {code && (
                              <details className="general-trace-code" open={index === latestCodeIndex}>
                                <summary>{codeTitle}</summary>
                                <CodeBlock className="general-code-block" code={code} language="python" />
                              </details>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                <section>
                  <div className="general-section-label">运行输出</div>
                  <Tabs
                    className="general-output-tabs"
                    items={[
                      {
                        key: 'structured',
                        label: '结构化结果',
                        children: <CodeBlock className="general-json-block" code={formatJson(activeResult.structured_result) || '无结构化结果'} language="json" />,
                      },
                      {
                        key: 'stdout',
                        label: 'stdout',
                        children: <CodeBlock className="general-json-block" code={formatJson(activeResult.stdout) || '无 stdout'} language="text" />,
                      },
                      {
                        key: 'stderr',
                        label: 'stderr',
                        children: <CodeBlock className="general-json-block" code={formatJson(activeResult.stderr) || '无 stderr'} language="text" />,
                      },
                    ]}
                  />
                </section>
                    </>
                  );
                })()}
              </div>
            ) : (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="运行后将在这里显示回复、执行流程、代码和输出" />
            )}
          </Card>
        </Space>
        <aside className="general-skill-side">
          <Card className="data-card general-skill-list-card" title="通用技能">
            <div className="general-skill-list">
              {rows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无通用技能" />}
              {rows.map((row) => {
                const active = row.slug === selectedSkill?.slug;
                return (
                  <button
                    type="button"
                    className={`general-skill-list-item ${active ? 'active' : ''}`}
                    key={row.id}
                    onClick={() => {
                      setSelectedSlug(row.slug);
                      editSkill(row);
                    }}
                  >
                    <span>
                      <strong>{row.name}</strong>
                      <small>{row.slug}</small>
                    </span>
                    <Tag color={row.status === 'published' ? 'green' : 'default'}>{row.status}</Tag>
                  </button>
                );
              })}
            </div>
          </Card>
        </aside>
      </div>
    </>
  );
}
