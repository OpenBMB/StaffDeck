import {
  CheckCircleOutlined,
  CloudOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  MoreOutlined,
  PlayCircleOutlined,
  UploadOutlined,
  DownOutlined,
} from '@ant-design/icons';
import { Button, Card, Dropdown, Empty, Input, Modal, Select, Space, Tag, Typography, message } from 'antd';
import type { ChangeEvent, DragEvent } from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, streamPost, TENANT_ID } from '../api/client';
import CodeBlock, { renderCodeTokens } from '../components/CodeBlock';
import type { AgentProfileRead, GeneralSkillRead, GeneralSkillRunResponse } from '../types';

const DEFAULT_MARKDOWN = `# 技能说明

这里粘贴任意格式的技能文档。系统不会从文档中自动抽取名称、Slug 或描述；这些信息由上方表单维护。`;

const DEFAULT_GENERAL_META = {
  name: '中国城市天气',
  slug: 'weather-zh',
  description: '中国城市天气查询工具',
  homepage: 'https://www.weather.com.cn/',
};
const ENTERPRISE_AGENT_STORAGE_KEY = 'ultrarag_enterprise_agent_scope';

type GeneralSkillFile = {
  path: string;
  content: string;
  size?: number;
  mime_type?: string;
};

type DroppedSkillFile = {
  file: File;
  path: string;
};

type SkillFileSystemEntry = {
  name: string;
  fullPath: string;
  isFile: boolean;
  isDirectory: boolean;
};

type SkillFileEntry = SkillFileSystemEntry & {
  file: (success: (file: File) => void, failure?: (error: DOMException) => void) => void;
};

type SkillDirectoryEntry = SkillFileSystemEntry & {
  createReader: () => {
    readEntries: (
      success: (entries: SkillFileSystemEntry[]) => void,
      failure?: (error: DOMException) => void,
    ) => void;
  };
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

function codeLanguage(value: string, fallback = 'text'): string {
  const trimmed = value.trim();
  if (!trimmed) return fallback;
  try {
    JSON.parse(trimmed);
    return 'json';
  } catch {
    return fallback;
  }
}

function RunCodePanel({
  title,
  code,
  language,
  defaultOpen = false,
}: {
  title: string;
  code: string;
  language?: string;
  defaultOpen?: boolean;
}) {
  return (
    <details className="general-trace-code general-output-code" open={defaultOpen}>
      <summary>{title}</summary>
      <CodeBlock className="general-code-block" code={code} language={language || codeLanguage(code)} />
    </details>
  );
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

function statusLabel(status: GeneralSkillRead['status']): string {
  if (status === 'published') return '已发布';
  if (status === 'archived') return '已下线';
  return '草稿';
}

function statusColor(status: GeneralSkillRead['status']): string {
  if (status === 'published') return 'green';
  if (status === 'archived') return 'default';
  return 'gold';
}

function languageFromFilePath(path?: string): string {
  const extension = (path || '').split('.').pop()?.toLowerCase();
  if (extension === 'py') return 'python';
  if (extension === 'json') return 'json';
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  return 'text';
}

function normalizeSkillFilePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+/g, '/').trim();
}

function packagePathFromRaw(value: string): string {
  const normalized = value.replace(/\\/g, '/').replace(/^\/+/, '');
  const parts = normalized.split('/').filter(Boolean);
  return parts.length > 1 ? parts.slice(1).join('/') : normalized;
}

function packagePath(file: File): string {
  return packagePathFromRaw((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name);
}

function readEntryFile(entry: SkillFileEntry): Promise<File> {
  return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readDirectoryEntries(entry: SkillDirectoryEntry): Promise<SkillFileSystemEntry[]> {
  const reader = entry.createReader();
  const output: SkillFileSystemEntry[] = [];

  return new Promise((resolve, reject) => {
    const readNext = () => {
      reader.readEntries((entries) => {
        if (!entries.length) {
          resolve(output);
          return;
        }
        output.push(...entries);
        readNext();
      }, reject);
    };
    readNext();
  });
}

async function collectDroppedEntryFiles(entry: SkillFileSystemEntry): Promise<DroppedSkillFile[]> {
  if (entry.isFile) {
    const file = await readEntryFile(entry as SkillFileEntry);
    return [{ file, path: packagePathFromRaw(entry.fullPath || file.name) }];
  }
  if (!entry.isDirectory) return [];
  const entries = await readDirectoryEntries(entry as SkillDirectoryEntry);
  const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
  return nested.flat();
}

function dataTransferEntry(item: DataTransferItem): SkillFileSystemEntry | null {
  const getter = (item as unknown as { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry;
  const entry = getter?.call(item);
  if (!entry || typeof entry !== 'object') return null;
  return entry as SkillFileSystemEntry;
}

async function droppedSkillFiles(dataTransfer: DataTransfer): Promise<DroppedSkillFile[]> {
  const entries = Array.from(dataTransfer.items || [])
    .map(dataTransferEntry)
    .filter((entry): entry is SkillFileSystemEntry => Boolean(entry));
  if (entries.length) {
    const nested = await Promise.all(entries.map(collectDroppedEntryFiles));
    return nested.flat();
  }
  return Array.from(dataTransfer.files || []).map((file) => ({ file, path: packagePath(file) }));
}

function parseMetadata(markdownText: string): Record<string, string> {
  const lines = markdownText.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return {};
  const result: Record<string, string> = {};
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index].trim();
    if (line === '---') break;
    const colon = line.indexOf(':');
    if (colon < 0) continue;
    const key = line.slice(0, colon).trim();
    const value = line.slice(colon + 1).trim().replace(/^['"]|['"]$/g, '');
    if (key && value) result[key] = value;
  }
  return result;
}

function applyMetadata(
  markdownText: string,
  setters: {
    setSkillName: (value: string) => void;
    setSkillSlug: (value: string) => void;
    setSkillDescription: (value: string) => void;
    setSkillHomepage: (value: string) => void;
  },
) {
  const metadata = parseMetadata(markdownText);
  if (metadata.name || metadata.title) setters.setSkillName(metadata.name || metadata.title);
  if (metadata.slug || metadata.id) setters.setSkillSlug(metadata.slug || metadata.id);
  if (metadata.description || metadata.summary) setters.setSkillDescription(metadata.description || metadata.summary);
  if (metadata.homepage || metadata.url) setters.setSkillHomepage(metadata.homepage || metadata.url);
}

function normalizedSkillFiles(files: GeneralSkillFile[] = []): string {
  return JSON.stringify(
    [...files]
      .map((file) => ({
        path: file.path,
        content: file.content,
        mime_type: file.mime_type || '',
      }))
      .sort((a, b) => a.path.localeCompare(b.path)),
  );
}

export default function GeneralSkillsPage({ embedded = false }: { embedded?: boolean }) {
  const [rows, setRows] = useState<GeneralSkillRead[]>([]);
  const [markdown, setMarkdown] = useState(DEFAULT_MARKDOWN);
  const [skillName, setSkillName] = useState(DEFAULT_GENERAL_META.name);
  const [skillSlug, setSkillSlug] = useState(DEFAULT_GENERAL_META.slug);
  const [skillDescription, setSkillDescription] = useState(DEFAULT_GENERAL_META.description);
  const [skillHomepage, setSkillHomepage] = useState(DEFAULT_GENERAL_META.homepage);
  const [skillFiles, setSkillFiles] = useState<GeneralSkillFile[]>([
    { path: 'SKILL.md', content: DEFAULT_MARKDOWN, size: DEFAULT_MARKDOWN.length, mime_type: 'text/markdown' },
  ]);
  const [selectedSlug, setSelectedSlug] = useState<string>();
  const [editingSlug, setEditingSlug] = useState<string | null>(null);
  const [query, setQuery] = useState('北京今天天气怎么样');
  const [runResult, setRunResult] = useState<GeneralSkillRunResponse | null>(null);
  const [liveResult, setLiveResult] = useState<Partial<GeneralSkillRunResponse> | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | GeneralSkillRead['status']>('all');
  const [selectedFilePath, setSelectedFilePath] = useState('SKILL.md');
  const [editorScroll, setEditorScroll] = useState({ top: 0, left: 0 });
  const [clawhubModalOpen, setClawhubModalOpen] = useState(false);
  const [clawhubSource, setClawhubSource] = useState('');
  const [clawhubLoading, setClawhubLoading] = useState(false);
  const [agentImportOpen, setAgentImportOpen] = useState(false);
  const [agentImportLoading, setAgentImportLoading] = useState(false);
  const [agentImportAgents, setAgentImportAgents] = useState<AgentProfileRead[]>([]);
  const [agentImportSourceAgentId, setAgentImportSourceAgentId] = useState('');
  const [agentImportSourceSkills, setAgentImportSourceSkills] = useState<GeneralSkillRead[]>([]);
  const [agentImportSelectedSkillIds, setAgentImportSelectedSkillIds] = useState<string[]>([]);
  const [agentId, setAgentId] = useState(() => window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
  const [isOverallAgent, setIsOverallAgent] = useState(true);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  const selectedSkill = useMemo(
    () => rows.find((row) => row.slug === selectedSlug) || rows[0],
    [rows, selectedSlug],
  );
  const activeResult = runResult || liveResult;
  const selectedFile = useMemo(
    () => skillFiles.find((file) => file.path === selectedFilePath) || skillFiles[0],
    [skillFiles, selectedFilePath],
  );
  const selectedFileLanguage = useMemo(() => languageFromFilePath(selectedFile?.path), [selectedFile?.path]);
  const filteredRows = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    return rows.filter((row) => {
      const matchesStatus = statusFilter === 'all' || row.status === statusFilter;
      const haystack = [row.name, row.slug, row.description, row.homepage].filter(Boolean).join(' ').toLowerCase();
      const matchesKeyword = !keyword || haystack.includes(keyword);
      return matchesStatus && matchesKeyword;
    });
  }, [rows, searchText, statusFilter]);

  const load = () => {
    const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
    return api
      .get<GeneralSkillRead[]>(`/api/enterprise/general-skills?tenant_id=${TENANT_ID}${agentSuffix}`)
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
          setSkillFiles(items[0].skill_files?.length ? items[0].skill_files : [{ path: 'SKILL.md', content: items[0].skill_markdown }]);
          setSelectedFilePath((items[0].skill_files?.length ? items[0].skill_files : [{ path: 'SKILL.md' }])[0].path);
        }
      })
      .catch((error) => message.error(error.message));
  };

  useEffect(() => {
    void load();
  }, [agentId]);

  useEffect(() => {
    api
      .get<Array<{ id: string; is_overall: boolean }>>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((items) => {
        setIsOverallAgent(Boolean(items.find((item) => item.id === agentId)?.is_overall ?? true));
      })
      .catch(() => setIsOverallAgent(true));
  }, [agentId]);

  useEffect(() => {
    const onScopeChange = (event: Event) => {
      const detail = (event as CustomEvent<{ agentId?: string }>).detail;
      setAgentId(detail?.agentId || window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY) || '');
    };
    window.addEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
    return () => window.removeEventListener('ultrarag-enterprise-agent-scope-change', onScopeChange);
  }, []);

  useEffect(() => {
    folderInputRef.current?.setAttribute('webkitdirectory', '');
    folderInputRef.current?.setAttribute('directory', '');
  }, []);

  useEffect(() => {
    if (!skillFiles.length) return;
    if (!skillFiles.some((file) => file.path === selectedFilePath)) {
      const skillFile = skillFiles.find((file) => file.path.split('/').pop()?.toLowerCase() === 'skill.md');
      setSelectedFilePath(skillFile?.path || skillFiles[0].path);
    }
  }, [skillFiles, selectedFilePath]);

  useEffect(() => {
    setEditorScroll({ top: 0, left: 0 });
  }, [selectedFilePath]);

  function hasUnsavedEditingChanges(): boolean {
    if (!editingSlug) return false;
    const original = rows.find((row) => row.slug === editingSlug);
    if (!original) return false;
    return (
      markdown !== original.skill_markdown
      || skillName !== original.name
      || skillSlug !== original.slug
      || skillDescription !== (original.description || '')
      || skillHomepage !== (original.homepage || '')
      || normalizedSkillFiles(skillFiles) !== normalizedSkillFiles(
        original.skill_files?.length ? original.skill_files : [{ path: 'SKILL.md', content: original.skill_markdown }],
      )
    );
  }

  async function importSkill(): Promise<GeneralSkillRead | null> {
    if (!markdown.trim()) {
      message.warning('请先粘贴或上传 SKILL.md');
      return null;
    }
    setSaving(true);
    try {
      const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import', {
        tenant_id: TENANT_ID,
        name: skillName.trim() || undefined,
        slug: skillSlug.trim() || undefined,
        description: skillDescription.trim() || undefined,
        homepage: skillHomepage.trim() || undefined,
        markdown,
        files: skillFiles.length ? skillFiles : [{ path: 'SKILL.md', content: markdown }],
        status: 'published',
        original_slug: editingSlug || undefined,
      });
      message.success(editingSlug ? `已保存 ${row.name}` : `已导入 ${row.name}`);
      setSelectedSlug(row.slug);
      setEditingSlug(row.slug);
      setMarkdown(row.skill_markdown);
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
      setRows((current) => {
        const withoutSaved = current.filter((item) => item.id !== row.id && item.slug !== row.slug);
        return [row, ...withoutSaved];
      });
      void load();
      return row;
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存技能失败');
      return null;
    } finally {
      setSaving(false);
    }
  }

  function newSkill() {
    setMarkdown(DEFAULT_MARKDOWN);
    setSkillName('');
    setSkillSlug('');
    setSkillDescription('');
    setSkillHomepage('');
    setSkillFiles([{ path: 'SKILL.md', content: DEFAULT_MARKDOWN, size: DEFAULT_MARKDOWN.length, mime_type: 'text/markdown' }]);
    setSelectedFilePath('SKILL.md');
    setEditingSlug(null);
    setRunResult(null);
  }

  function editSkill(row: GeneralSkillRead) {
    setMarkdown(row.skill_markdown);
    setSkillName(row.name);
    setSkillSlug(row.slug);
    setSkillDescription(row.description || '');
    setSkillHomepage(row.homepage || '');
    setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
    setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    setSelectedSlug(row.slug);
    setEditingSlug(row.slug);
    setRunResult(null);
  }

  function replaceRow(row: GeneralSkillRead) {
    setRows((current) => current.map((item) => (item.id === row.id ? row : item)));
    if (editingSlug === row.slug) {
      setSkillName(row.name);
      setSkillSlug(row.slug);
      setSkillDescription(row.description || '');
      setSkillHomepage(row.homepage || '');
      setMarkdown(row.skill_markdown);
      setSkillFiles(row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md', content: row.skill_markdown }]);
      setSelectedFilePath((row.skill_files?.length ? row.skill_files : [{ path: 'SKILL.md' }])[0].path);
    }
  }

  async function setSkillPublished(row: GeneralSkillRead, published: boolean) {
    try {
      const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
      const next = await api.post<GeneralSkillRead>(
        `/api/enterprise/general-skills/${row.slug}/${published ? 'publish' : 'archive'}?tenant_id=${TENANT_ID}${agentSuffix}`,
      );
      replaceRow(next);
      message.success(published ? '已启用技能' : '已停用技能');
    } catch (error) {
      message.error(error instanceof Error ? error.message : published ? '发布失败' : '下线失败');
    }
  }

  function confirmDeleteSkill(row: GeneralSkillRead) {
    const branchMode = !isOverallAgent;
    Modal.confirm({
      title: branchMode ? `从当前员工移除技能：${row.name}` : `删除技能：${row.name}`,
      content: branchMode
        ? '这只会在当前员工中隐藏该技能；组织资源库和其他员工仍然保留。'
        : '删除后该技能不会再出现在组织技能库中，此操作不可撤销。',
      okText: branchMode ? '移除' : '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      async onOk() {
        try {
          const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
          await api.delete(`/api/enterprise/general-skills/${row.slug}?tenant_id=${TENANT_ID}${agentSuffix}`);
          const nextRows = rows.filter((item) => item.id !== row.id);
          setRows(nextRows);
          if (selectedSlug === row.slug || editingSlug === row.slug) {
            const next = nextRows[0];
            if (next) {
              setSelectedSlug(next.slug);
              editSkill(next);
            } else {
              setSelectedSlug(undefined);
              newSkill();
            }
          }
          message.success(branchMode ? '已从当前员工移除技能' : '已删除技能');
        } catch (error) {
          message.error(error instanceof Error ? error.message : '删除失败');
        }
      },
    });
  }

  function startImportedDraft() {
    setEditingSlug(null);
    setSelectedSlug(undefined);
    setRunResult(null);
    setLiveResult(null);
  }

  async function withImportPreparation(importAction: () => void | Promise<void>) {
    if (!hasUnsavedEditingChanges()) {
      await importAction();
      return;
    }

    Modal.confirm({
      title: '导入新技能前是否保存当前技能？',
      content: '你正在编辑现有技能。导入会进入新建状态，不会覆盖当前技能。',
      okText: '保存并发布',
      cancelText: '不保存，继续导入',
      async onOk() {
        const saved = await importSkill();
        if (saved) await importAction();
      },
      async onCancel() {
        await importAction();
      },
    });
  }

  function requestImport(kind: 'file' | 'folder') {
    void withImportPreparation(() => {
      if (kind === 'folder') {
        setSkillFiles([]);
        folderInputRef.current?.click();
        return;
      }
      fileInputRef.current?.click();
    });
  }

  function requestClawHubImport() {
    void withImportPreparation(() => {
      setClawhubSource('');
      setClawhubModalOpen(true);
    });
  }

  function requestAgentImport() {
    void withImportPreparation(async () => {
      try {
        const agents = await api.get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`);
        const candidates = agents.filter((item) => item.id !== agentId);
        const firstSource = candidates[0]?.id || '';
        setAgentImportAgents(candidates);
        setAgentImportSourceAgentId(firstSource);
        setAgentImportSelectedSkillIds([]);
        setAgentImportOpen(true);
        if (firstSource) {
          await loadAgentImportSourceSkills(firstSource);
        } else {
          setAgentImportSourceSkills([]);
        }
      } catch (error) {
        message.error(error instanceof Error ? error.message : '加载员工列表失败');
      }
    });
  }

  async function loadAgentImportSourceSkills(sourceAgentId: string) {
    setAgentImportSourceSkills([]);
    setAgentImportSelectedSkillIds([]);
    if (!sourceAgentId) return;
    try {
      const sourceRows = await api.get<GeneralSkillRead[]>(
        `/api/enterprise/general-skills?tenant_id=${TENANT_ID}&agent_id=${encodeURIComponent(sourceAgentId)}`,
      );
      const existingIds = new Set(rows.map((item) => item.id));
      setAgentImportSourceSkills(sourceRows.filter((item) => !existingIds.has(item.id)));
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载来源技能失败');
    }
  }

  async function submitAgentImportSkills() {
    if (!agentId) {
      message.warning('请先选择目标员工');
      return;
    }
    if (!agentImportSourceAgentId) {
      message.warning('请选择来源员工');
      return;
    }
    if (!agentImportSelectedSkillIds.length) {
      message.warning('请选择要学习的技能');
      return;
    }
    setAgentImportLoading(true);
    try {
      await api.post(`/api/enterprise/agents/${encodeURIComponent(agentId)}/resources/import`, {
        tenant_id: TENANT_ID,
        source_agent_id: agentImportSourceAgentId,
        resource_type: 'general_skill',
        resource_ids: agentImportSelectedSkillIds,
      });
      message.success(`已学习 ${agentImportSelectedSkillIds.length} 个技能`);
      setAgentImportOpen(false);
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : '学习技能失败');
    } finally {
      setAgentImportLoading(false);
    }
  }

  async function importClawHubSource() {
    if (!clawhubSource.trim()) {
      message.warning('请输入 ClawHub/GitHub/zip 来源');
      return;
    }
    setClawhubLoading(true);
    try {
      const row = await api.post<GeneralSkillRead>('/api/enterprise/general-skills/import-clawhub', {
        tenant_id: TENANT_ID,
        source: clawhubSource.trim(),
        status: 'published',
      });
      message.success(`已导入 ${row.name}`);
      setRows((current) => [row, ...current.filter((item) => item.id !== row.id && item.slug !== row.slug)]);
      setSelectedSlug(row.slug);
      editSkill(row);
      setClawhubModalOpen(false);
      void load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'ClawHub 导入失败');
    } finally {
      setClawhubLoading(false);
    }
  }

  function updateSelectedFile(text: string) {
    if (!selectedFile) return;
    setSkillFiles((current) => current.map((file) => (
      file.path === selectedFile.path
        ? { ...file, content: text, size: text.length }
        : file
    )));
    if (selectedFile.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      setMarkdown(text);
    }
  }

  function addSkillFile() {
    const base = 'notes.md';
    let candidate = base;
    let index = 2;
    while (skillFiles.some((file) => file.path === candidate)) {
      candidate = `notes-${index}.md`;
      index += 1;
    }
    setSkillFiles((current) => [...current, { path: candidate, content: '', size: 0, mime_type: 'text/markdown' }]);
    setSelectedFilePath(candidate);
  }

  function deleteSelectedFile() {
    if (!selectedFile) return;
    deleteSkillFile(selectedFile);
  }

  function deleteSkillFile(target: GeneralSkillFile) {
    if (target.path.split('/').pop()?.toLowerCase() === 'skill.md') {
      message.warning('SKILL.md 是技能入口，不能删除');
      return;
    }
    Modal.confirm({
      title: `删除文件：${target.path}`,
      content: '删除后需要重新导入或手动新建该文件。',
      okText: '删除',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk() {
        setSkillFiles((current) => current.filter((file) => file.path !== target.path));
      },
    });
  }

  function renameSkillFile(target: GeneralSkillFile) {
    let nextPath = target.path;
    Modal.confirm({
      title: '重命名文件',
      content: (
        <Input
          autoFocus
          defaultValue={target.path}
          onChange={(event) => {
            nextPath = event.target.value;
          }}
        />
      ),
      okText: '重命名',
      cancelText: '取消',
      onOk() {
        const normalized = normalizeSkillFilePath(nextPath);
        if (!normalized) {
          message.error('文件名不能为空');
          return Promise.reject();
        }
        if (normalized === target.path) return undefined;
        if (skillFiles.some((file) => file.path === normalized)) {
          message.error('已存在同名文件');
          return Promise.reject();
        }
        setSkillFiles((current) => current.map((file) => (
          file.path === target.path
            ? { ...file, path: normalized }
            : file
        )));
        if (selectedFilePath === target.path) {
          setSelectedFilePath(normalized);
        }
        return undefined;
      },
    });
  }

  async function runSkill() {
    const slug = selectedSkill?.slug;
    if (!slug) {
      message.warning('请先导入技能');
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

  async function importSingleFile(target: File) {
    const text = await target.text();
    const nextFile = { path: 'SKILL.md', content: text, size: target.size, mime_type: target.type || 'text/markdown' };
    startImportedDraft();
    setSkillFiles([nextFile]);
    setSelectedFilePath('SKILL.md');
    setMarkdown(text);
    applyMetadata(text, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
    message.success(`已读取 ${target.name}`);
  }

  async function importSkillPackage(targets: DroppedSkillFile[]) {
    if (!targets.length) return;
    const nextFiles = await Promise.all(
      targets.map(async ({ file, path }) => {
        const text = await file.text();
        return {
          path,
          content: text,
          size: file.size,
          mime_type: file.type || undefined,
        };
      }),
    );
    nextFiles.sort((a, b) => a.path.localeCompare(b.path));
    startImportedDraft();
    setSkillFiles(nextFiles);
    const skillFile = nextFiles.find((item) => item.path.split('/').pop()?.toLowerCase() === 'skill.md');
    if (skillFile) {
      setMarkdown(skillFile.content);
      setSelectedFilePath(skillFile.path);
      applyMetadata(skillFile.content, { setSkillName, setSkillSlug, setSkillDescription, setSkillHomepage });
      message.success(`已读取 ${nextFiles.length} 个文件`);
    } else {
      setSelectedFilePath(nextFiles[0]?.path || 'SKILL.md');
      message.warning('文件夹中没有找到 SKILL.md');
    }
  }

  async function importFolderFiles(fileList: FileList | null) {
    await importSkillPackage(Array.from(fileList || []).map((file) => ({ file, path: packagePath(file) })));
  }

  async function handleFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    const target = event.target.files?.[0];
    if (target) await importSingleFile(target);
    event.target.value = '';
  }

  async function handleFolderInputChange(event: ChangeEvent<HTMLInputElement>) {
    await importFolderFiles(event.target.files);
    event.target.value = '';
  }

  function acceptsFileDrop(event: DragEvent<HTMLElement>): boolean {
    return Array.from(event.dataTransfer.types || []).includes('Files');
  }

  function handleDragEnter(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(true);
  }

  function handleDragOver(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    setDragActive(true);
  }

  function handleDragLeave(event: DragEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    setDragActive(false);
  }

  async function handleDrop(event: DragEvent<HTMLElement>) {
    if (!acceptsFileDrop(event)) return;
    event.preventDefault();
    setDragActive(false);
    const dropped = await droppedSkillFiles(event.dataTransfer);
    if (!dropped.length) return;
    await withImportPreparation(async () => {
      if (dropped.length === 1 && !dropped[0].path.includes('/')) {
        await importSingleFile(dropped[0].file);
        return;
      }
      await importSkillPackage(dropped);
    });
  }

  const isLiveRunning = loading && !runResult;

  return (
    <>
      {!embedded && (
        <div className="page-title">
          <Typography.Title level={3}>已掌握技能</Typography.Title>
          <Typography.Text type="secondary">维护员工可直接调用的通用能力，验证模型选择、代码生成与运行链路。</Typography.Text>
        </div>
      )}
      <div className="general-skill-workbench">
        <Space direction="vertical" size={16} className="general-skill-main">
          <Card
            className={`editor-card general-skill-editor ${dragActive ? 'drag-active' : ''}`}
            onDragEnter={handleDragEnter}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            title={(
              <Space>
                <FileTextOutlined />
                <span>{editingSlug ? `编辑技能：${editingSlug}` : '新增技能'}</span>
              </Space>
            )}
            extra={(
              <Space wrap>
                <Button onClick={newSkill}>新建</Button>
                <Dropdown
                  trigger={['click']}
                  menu={{
                    items: [
                      { key: 'file', label: '选择文件' },
                      { key: 'folder', label: '选择文件夹' },
                      { key: 'clawhub', label: '从 ClawHub / GitHub 导入' },
                      { key: 'agent', label: '向其他员工学习技能' },
                    ],
                    onClick: ({ key }) => {
                      if (key === 'clawhub') {
                        requestClawHubImport();
                        return;
                      }
                      if (key === 'agent') {
                        requestAgentImport();
                        return;
                      }
                      requestImport(key === 'folder' ? 'folder' : 'file');
                    },
                  }}
                >
                  <Button icon={<UploadOutlined />}>
                    导入 <DownOutlined />
                  </Button>
                </Dropdown>
                <Button type="primary" loading={saving} icon={<CloudOutlined />} onClick={importSkill}>保存并发布</Button>
              </Space>
            )}
          >
            <input
              ref={fileInputRef}
              className="visually-hidden-file-input"
              type="file"
              accept=".md,.txt"
              onChange={handleFileInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            <input
              ref={folderInputRef}
              className="visually-hidden-file-input"
              type="file"
              multiple
              onChange={handleFolderInputChange}
              hidden
              aria-hidden="true"
              tabIndex={-1}
            />
            {dragActive && (
              <div className="general-skill-drop-hint">
                <UploadOutlined />
                <span>释放以导入 SKILL.md 或完整技能文件夹</span>
              </div>
            )}
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
                placeholder="描述，用于员工选择技能"
              />
              <Input
                value={skillHomepage}
                onChange={(event) => setSkillHomepage(event.target.value)}
                placeholder="主页或参考链接，可选"
              />
            </div>
            <div className="general-skill-file-editor">
              <aside className="general-skill-file-tree">
                <div className="general-skill-file-tree-title">
                  <FolderOpenOutlined />
                  <span>文件</span>
                </div>
                <div className="general-skill-file-tree-list">
                  {skillFiles.map((file) => (
                    <Dropdown
                      key={file.path}
                      trigger={['contextMenu']}
                      menu={{
                        items: [
                          { key: 'rename', icon: <EditOutlined />, label: '重命名' },
                          { key: 'delete', icon: <DeleteOutlined />, label: '删除', danger: true },
                        ],
                        onClick: ({ key }) => {
                          if (key === 'rename') {
                            renameSkillFile(file);
                            return;
                          }
                          deleteSkillFile(file);
                        },
                      }}
                    >
                      <button
                        type="button"
                        className={`general-skill-file-node ${file.path === selectedFile?.path ? 'active' : ''}`}
                        onClick={() => setSelectedFilePath(file.path)}
                        onContextMenu={() => setSelectedFilePath(file.path)}
                        title={file.path}
                      >
                        <FileTextOutlined />
                        <span>{file.path}</span>
                      </button>
                    </Dropdown>
                  ))}
                </div>
                <div className="general-skill-file-actions">
                  <Button size="small" onClick={addSkillFile}>新建文件</Button>
                  <Button size="small" icon={<DeleteOutlined />} onClick={deleteSelectedFile} />
                </div>
              </aside>
              <section className="general-skill-file-pane">
                <div className="general-skill-file-tab">
                  <FileTextOutlined />
                  <span>{selectedFile?.path || '未选择文件'}</span>
                </div>
                <div className="general-skill-code-editor" data-language={selectedFileLanguage}>
                  <pre className="general-skill-code-highlight" aria-hidden="true">
                    <code
                      style={{
                        transform: `translate(${-editorScroll.left}px, ${-editorScroll.top}px)`,
                      }}
                    >
                      {renderCodeTokens(selectedFile?.content || '\u200b', selectedFileLanguage)}
                    </code>
                  </pre>
                  <textarea
                    className="general-skill-code-input"
                    value={selectedFile?.content || ''}
                    onChange={(event) => updateSelectedFile(event.target.value)}
                    onScroll={(event) => setEditorScroll({
                      top: event.currentTarget.scrollTop,
                      left: event.currentTarget.scrollLeft,
                    })}
                    spellCheck={false}
                  />
                </div>
              </section>
            </div>
          </Card>
          <Card
            className="editor-card general-skill-run-card"
            title="运行测试"
            extra={<Button type="primary" loading={loading} icon={<ExperimentOutlined />} onClick={runSkill}>运行</Button>}
          >
            <div className="general-run-form">
              <Select
                value={selectedSkill?.slug}
                placeholder="选择技能"
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
                    {activeResult.reply || (loading ? '正在运行技能...' : '暂无回复')}
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
                            {detail && (
                              <RunCodePanel
                                title={phase === 'code_finished' ? '查看执行结果' : phase === 'stdout_chunk' ? '查看运行输出' : '查看详情'}
                                code={detail}
                                language={codeLanguage(detail)}
                                defaultOpen={phase === 'code_finished' || phase === 'code_timeout'}
                              />
                            )}
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
                  <div className="general-output-stack">
                    <RunCodePanel
                      title="结构化结果"
                      code={formatJson(activeResult.structured_result) || '无结构化结果'}
                      language="json"
                      defaultOpen
                    />
                    <RunCodePanel
                      title="stdout"
                      code={formatJson(activeResult.stdout) || '无 stdout'}
                      language={codeLanguage(formatJson(activeResult.stdout), 'text')}
                    />
                    <RunCodePanel
                      title="stderr"
                      code={formatJson(activeResult.stderr) || '无 stderr'}
                      language={codeLanguage(formatJson(activeResult.stderr), 'text')}
                    />
                  </div>
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
          <Card className="data-card general-skill-list-card" title="技能库">
            <div className="general-skill-list-toolbar">
              <div className="general-skill-search-combo">
                <Input.Search
                  allowClear
                  size="small"
                  placeholder="搜索名称、Slug、描述"
                  value={searchText}
                  onChange={(event) => setSearchText(event.target.value)}
                />
                <Select
                  size="small"
                  value={statusFilter}
                  onChange={setStatusFilter}
                  options={[
                    { label: '全部状态', value: 'all' },
                    { label: '已发布', value: 'published' },
                    { label: '草稿', value: 'draft' },
                    { label: '已下线', value: 'archived' },
                  ]}
                />
              </div>
            </div>
            <div className="general-skill-list">
              {filteredRows.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={rows.length === 0 ? '暂无技能' : '没有符合筛选的技能'} />}
              {filteredRows.map((row) => {
                const active = row.slug === selectedSkill?.slug;
                return (
                  <div
                    className={`general-skill-list-item ${active ? 'active' : ''}`}
                    key={row.id}
                  >
                    <button
                      type="button"
                      className="general-skill-list-main"
                      onClick={() => {
                        setSelectedSlug(row.slug);
                        editSkill(row);
                      }}
                    >
                      <span>
                        <strong>{row.name}</strong>
                        <small>{row.slug}</small>
                      </span>
                    </button>
                    <div className="general-skill-list-actions">
                      <Tag color={statusColor(row.status)}>{statusLabel(row.status)}</Tag>
                      <Dropdown
                        trigger={['click']}
                        menu={{
                          items: [
                            row.status === 'published'
                              ? { key: 'archive', label: '下线' }
                              : { key: 'publish', label: '发布' },
                            { key: 'delete', label: isOverallAgent ? '删除' : '从当前员工移除', danger: true },
                          ].filter(Boolean),
                          onClick: ({ key, domEvent }) => {
                            domEvent.stopPropagation();
                            if (key === 'publish') {
                              void setSkillPublished(row, true);
                              return;
                            }
                            if (key === 'archive') {
                              void setSkillPublished(row, false);
                              return;
                            }
                            confirmDeleteSkill(row);
                          },
                        }}
                      >
                        <Button
                          className="general-skill-list-more"
                          type="text"
                          size="small"
                          icon={<MoreOutlined />}
                          onClick={(event) => event.stopPropagation()}
                        />
                      </Dropdown>
                    </div>
                  </div>
                );
              })}
            </div>
          </Card>
        </aside>
      </div>
      <Modal
        title="从 ClawHub / GitHub 导入技能"
        open={clawhubModalOpen}
        onOk={importClawHubSource}
        confirmLoading={clawhubLoading}
        onCancel={() => setClawhubModalOpen(false)}
        okText="导入"
        cancelText="取消"
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            支持 GitHub repo/tree/raw SKILL.md、zip 包地址，或 owner/repo 形式。导入会新建技能，不覆盖当前内容。
          </Typography.Text>
          <Input
            value={clawhubSource}
            onChange={(event) => setClawhubSource(event.target.value)}
            placeholder="例如 OpenBMB/PilotDeck/path/to/skill 或 https://github.com/owner/repo/tree/main/skill"
          />
        </Space>
      </Modal>
      <Modal
        title="向其他员工学习技能"
        open={agentImportOpen}
        okText="导入"
        cancelText="取消"
        confirmLoading={agentImportLoading}
        onOk={() => void submitAgentImportSkills()}
        onCancel={() => setAgentImportOpen(false)}
      >
        <Space direction="vertical" size={14} style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            学习会把来源员工可见的技能绑定到当前员工；不会覆盖当前编辑区内容。
          </Typography.Text>
          <Select
            value={agentImportSourceAgentId || undefined}
            placeholder="选择来源员工"
            onChange={(value) => {
              setAgentImportSourceAgentId(value);
              void loadAgentImportSourceSkills(value);
            }}
            options={agentImportAgents.map((item) => ({
              value: item.id,
              label: `${item.name}${item.is_overall ? '（整体）' : ''}`,
            }))}
            style={{ width: '100%' }}
          />
          <Select
            mode="multiple"
            value={agentImportSelectedSkillIds}
            placeholder="选择一个或多个技能"
            onChange={setAgentImportSelectedSkillIds}
            options={agentImportSourceSkills.map((item) => ({
              value: item.id,
              label: `${item.name} · ${item.slug} · ${statusLabel(item.status)}`,
            }))}
            optionFilterProp="label"
            notFoundContent={agentImportSourceAgentId ? '没有可学习的技能' : '请先选择来源员工'}
            style={{ width: '100%' }}
          />
        </Space>
      </Modal>
    </>
  );
}
