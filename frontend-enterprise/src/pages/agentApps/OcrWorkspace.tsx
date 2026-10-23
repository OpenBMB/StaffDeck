import type { DragEvent } from 'react';
import { useCallback, useMemo, useRef, useState, useEffect } from 'react';
import { Copy, Download, FileText, Image as ImageIcon, LoaderCircle, Upload, X } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { Checkbox, notify } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import type { EnterpriseAuthUser } from '@/auth';
import type { AgentCatalogEntry } from '@/lib/agentCatalog';
import { cn } from '@/lib/utils';
import {
  fetchOcrStatus,
  runOcr,
  type OcrFileResult,
  type OcrFormat,
  type OcrOptions,
  type OcrPage,
  type OcrResponse,
  type OcrStatus,
} from '@/api/ocr';
import AgentAppBackButton from './AgentAppBackButton';

export type OcrWorkspaceProps = {
  entry: AgentCatalogEntry;
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
};

/** 上游接受的格式（与 indra.md 一致）。 */
const ACCEPT = '.pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp';
const ACCEPT_LABEL = 'PDF / PNG / JPEG / TIFF / BMP / WebP';

const FALLBACK_LIMITS = { maxFiles: 10, maxFileMb: 50 };
const DEFAULT_DPI = 200;

const FIELD_LABEL_CLASS = 'flex items-center gap-[6px] text-[12px] font-medium leading-none text-[#18181a]';
const FIELD_INPUT_CLASS =
  'h-[34px] w-full rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none transition-colors placeholder:text-[#a3aab9] focus:border-[#18181a]';
const HINT_CLASS = 'text-[11px] leading-[1.5] text-[#a3aab9]';
const SEGMENT_CLASS = 'flex rounded-[8px] border-[0.5px] border-[#e3e7f1] p-[2px]';

const FORMAT_OPTIONS: { value: OcrFormat; label: string; hint: string }[] = [
  { value: 'json', label: '结构化', hint: '带行坐标与版面块，适合二次处理' },
  { value: 'md', label: 'Markdown', hint: '标题 / 列表 / 表格尽量保留结构' },
  { value: 'text', label: '纯文本', hint: '只要文字，体积最小' },
];

/** 每页的「来源」说明：这页到底走的哪条路，直接决定结果可不可信。 */
const SOURCE_META: Record<OcrPage['source'], { label: string; className: string; hint: string }> = {
  text: {
    label: '文本层',
    className: 'bg-[#eaf6ee] text-[#1a7f4b]',
    hint: '直接读 PDF 自带文字层，最快最准',
  },
  ocr: {
    label: 'OCR',
    className: 'bg-[#eef3ff] text-[#2b5bd7]',
    hint: '这一页没有可用文字层，走图像识别',
  },
  hybrid: {
    label: '混合',
    className: 'bg-[#f3efff] text-[#6b46c1]',
    hint: '文字层 + 图片内文字合并',
  },
  error: {
    label: '失败',
    className: 'bg-[#fdecec] text-[#c0392b]',
    hint: '这一页识别失败，原因见右侧说明',
  },
};

type PageView = 'markdown' | 'text' | 'lines' | 'blocks';

const PAGE_VIEW_OPTIONS: { value: PageView; label: string }[] = [
  { value: 'markdown', label: 'Markdown' },
  { value: 'text', label: '纯文本' },
  { value: 'lines', label: '行' },
  { value: 'blocks', label: '版面块' },
];

function formatMs(ms: number | null | undefined): string {
  if (!ms || ms <= 0) return '';
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function copyText(text: string, label = '已复制') {
  void navigator.clipboard
    .writeText(text)
    .then(() => notify.success(label))
    .catch(() => notify.error('复制失败，请手动选中'));
}

function downloadText(name: string, text: string, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function stripExtension(name: string): string {
  return name.replace(/\.[^.]+$/, '') || 'ocr';
}

/** 一页里的 Markdown / 纯文本 / 行 / 版面块 —— 四个视图共用同一个 tab 组。 */
function PageBody({ page, view }: { page: OcrPage; view: PageView }) {
  if (view === 'lines') {
    if (page.lines.length === 0) {
      return <span className={HINT_CLASS}>这页没有返回行坐标（可能关闭了「行坐标」或用了纯文本输出格式）。</span>;
    }
    return (
      <div className="flex flex-col gap-[2px] font-mono text-[11px] leading-[1.7] text-[#4f5669]">
        {page.lines.map((line, index) => (
          <div key={index} className="flex min-w-0 gap-[8px]">
            <span className="w-[132px] shrink-0 text-[#a3aab9]">
              {Array.isArray(line.box) ? line.box.slice(0, 4).join(', ') : '—'}
            </span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{line.text || ''}</span>
            {typeof line.conf === 'number' && (
              <span className="w-[46px] shrink-0 text-right text-[#a3aab9]">{line.conf.toFixed(2)}</span>
            )}
          </div>
        ))}
      </div>
    );
  }

  if (view === 'blocks') {
    if (page.blocks.length === 0) {
      return <span className={HINT_CLASS}>这页没有返回版面块（可能关闭了「版面块」或用了纯文本输出格式）。</span>;
    }
    return (
      <div className="flex flex-col gap-[6px]">
        {page.blocks.map((block, index) => (
          <div key={index} className="flex min-w-0 items-start gap-[8px] rounded-[6px] bg-[#f7f8fa] px-[8px] py-[6px]">
            <span className="shrink-0 rounded-[4px] bg-white px-[6px] py-[1px] text-[10px] text-[#6b7280] shadow-[0_0_0_0.5px_#e3e7f1]">
              {block.label || 'Text'}
            </span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[12px] leading-[1.6] text-[#18181a]">
              {block.text || ''}
            </span>
          </div>
        ))}
      </div>
    );
  }

  const content = view === 'markdown' ? page.markdown : page.text || page.markdown;
  if (!content.trim()) {
    return <span className={HINT_CLASS}>这一页没有识别出文字。</span>;
  }
  return (
    <pre className="m-0 max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-[8px] bg-[#f7f8fa] p-[12px] font-mono text-[12px] leading-[1.7] text-[#18181a]">
      {content}
    </pre>
  );
}

function PageCard({
  page,
  view,
  onViewChange,
}: {
  page: OcrPage;
  view: PageView;
  onViewChange: (view: PageView) => void;
}) {
  const source = SOURCE_META[page.source] || SOURCE_META.ocr;
  const meta: string[] = [];
  if (page.rotation) meta.push(`旋转 ${page.rotation}°`);
  if (page.skew) meta.push(`倾斜 ${page.skew.toFixed(1)}°`);
  if (page.vertical) meta.push('竖排');
  if (page.n_lines) meta.push(`${page.n_lines} 行`);
  const timing = formatMs(page.timings?.total_ms);
  if (timing) meta.push(timing);

  return (
    <section className="rounded-[12px] border-[0.5px] border-[#eef0f5] bg-white">
      <header className="flex flex-wrap items-center gap-[8px] border-b-[0.5px] border-[#eef0f5] px-[12px] py-[8px]">
        <span className="text-[12px] font-medium text-[#18181a]">第 {page.index + 1} 页</span>
        <span
          title={source.hint}
          className={cn('rounded-[4px] px-[6px] py-[1px] text-[10px] font-medium', source.className)}
        >
          {source.label}
        </span>
        {meta.length > 0 && <span className="text-[11px] text-[#a3aab9]">{meta.join(' · ')}</span>}
        <div className="ml-auto flex items-center gap-[6px]">
          <div className={SEGMENT_CLASS}>
            {PAGE_VIEW_OPTIONS.map((option) => {
              const disabled =
                (option.value === 'lines' && page.lines.length === 0) ||
                (option.value === 'blocks' && page.blocks.length === 0);
              return (
                <button
                  key={option.value}
                  type="button"
                  disabled={disabled}
                  aria-pressed={view === option.value}
                  onClick={() => onViewChange(option.value)}
                  className={cn(
                    'rounded-[6px] px-[8px] py-[2px] text-[11px] transition-colors',
                    view === option.value
                      ? 'bg-[#18181a] font-medium text-white'
                      : 'text-[#757f9c] hover:text-[#18181a]',
                    disabled && 'cursor-not-allowed text-[#d3d8e2] hover:text-[#d3d8e2]',
                  )}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            title="复制这一页的 Markdown"
            onClick={() => copyText(page.markdown || page.text, `已复制第 ${page.index + 1} 页`)}
            className="flex size-[24px] items-center justify-center rounded-[6px] text-[#858b9c] transition-colors hover:bg-[#f2f4f8] hover:text-[#18181a]"
          >
            <Copy className="size-[13px]" />
          </button>
        </div>
      </header>

      {page.source === 'error' && page.note && (
        <div className="mx-[12px] mt-[8px] rounded-[6px] bg-[#fdecec] px-[10px] py-[6px] text-[11px] leading-[1.6] text-[#c0392b]">
          {page.note}
        </div>
      )}

      <div className="px-[12px] py-[10px]">
        <PageBody page={page} view={view} />
      </div>
    </section>
  );
}

function FileBlock({
  file,
  view,
  onViewChange,
}: {
  file: OcrFileResult;
  view: PageView;
  onViewChange: (view: PageView) => void;
}) {
  return (
    <div className="flex flex-col gap-[10px]">
      {file.error && (
        <div className="rounded-[8px] bg-[#fdecec] px-[10px] py-[8px] text-[11px] leading-[1.6] text-[#c0392b]">
          这份文件整体失败：{file.error}
        </div>
      )}
      {file.pages.map((page) => (
        <PageCard key={page.index} page={page} view={view} onViewChange={onViewChange} />
      ))}
      {file.pages.length === 0 && !file.error && (
        <div className="rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] px-[16px] py-[28px] text-center text-[12px] text-[#a3aab9]">
          没有识别出任何页面
        </div>
      )}
    </div>
  );
}

/**
 * OCR 助手工作台。
 *
 * 左栏是「文件 + 识别参数」，右栏是按页的结果。参数区完全对齐 `indra.md` 的
 * `/v1/ocr`：输出格式、识别模式、页数上限、DPI、密码，以及四个开关
 * （公式 / 页眉页脚 / 图片内文字 / 行坐标 / 版面块 / 耗时）。**没有模型选择器** ——
 * 这个能力不调大模型，不需要用户配置模型。
 *
 * 右栏刻意保留每页的「来源」徽章（文本层 / OCR / 混合 / 失败）：同一次识别里
 * 不同页的可信度并不相同，混在一起展示会让人误以为整份都是 OCR 出来的。
 */
export default function OcrWorkspace({ entry, currentUser, onLogout }: OcrWorkspaceProps) {
  const [status, setStatus] = useState<OcrStatus | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<OcrResponse | null>(null);
  const [activeFile, setActiveFile] = useState(0);
  const [pageView, setPageView] = useState<PageView>('markdown');
  const [error, setError] = useState('');

  const [format, setFormat] = useState<OcrFormat>('json');
  const [mode, setMode] = useState<'' | 'ocr'>('');
  const [dpi, setDpi] = useState<number | null>(DEFAULT_DPI);
  const [maxPages, setMaxPages] = useState<number | null>(null);
  const [password, setPassword] = useState('');
  const [math, setMath] = useState(false);
  const [furniture, setFurniture] = useState(false);
  const [imageOcr, setImageOcr] = useState(false);
  const [includeLines, setIncludeLines] = useState(true);
  const [includeBlocks, setIncludeBlocks] = useState(true);
  const [includeTimings, setIncludeTimings] = useState(false);

  const inputRef = useRef<HTMLInputElement | null>(null);

  const limits = {
    maxFiles: status?.max_files ?? FALLBACK_LIMITS.maxFiles,
    maxFileMb: status?.max_file_mb ?? FALLBACK_LIMITS.maxFileMb,
  };

  useEffect(() => {
    let alive = true;
    fetchOcrStatus()
      .then((row) => {
        if (alive) setStatus(row);
      })
      .catch(() => {
        // 状态接口失败不阻断使用：真正识别时会拿到更准确的错误
        if (alive) setStatus(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  const addFiles = useCallback(
    (incoming: File[]) => {
      if (incoming.length === 0) return;
      const accepted: File[] = [];
      for (const file of incoming) {
        if (file.size > limits.maxFileMb * 1024 * 1024) {
          notify.warning(`「${file.name}」超过 ${limits.maxFileMb}MB 上限，已跳过`);
          continue;
        }
        accepted.push(file);
      }
      setFiles((current) => {
        const merged = [...current];
        for (const file of accepted) {
          if (!merged.some((row) => row.name === file.name && row.size === file.size)) {
            merged.push(file);
          }
        }
        if (merged.length > limits.maxFiles) {
          notify.warning(`单次最多 ${limits.maxFiles} 个文件，多余的已忽略`);
          return merged.slice(0, limits.maxFiles);
        }
        return merged;
      });
    },
    [limits.maxFileMb, limits.maxFiles],
  );

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    addFiles(Array.from(event.dataTransfer.files || []));
  }

  function removeFile(index: number) {
    setFiles((current) => current.filter((_, i) => i !== index));
  }

  const canRun = files.length > 0 && !running && (status?.configured ?? true);

  async function start() {
    if (!canRun) return;
    setRunning(true);
    setError('');
    const options: OcrOptions = {
      format,
      mode,
      dpi,
      maxPages,
      password,
      math: math || null,
      furniture: furniture || null,
      imageOcr: imageOcr || null,
      // 「行坐标 / 版面块」默认是 true：只有用户主动取消才发 false
      includeLines,
      includeBlocks,
      includeTimings: includeTimings || null,
    };
    try {
      const response = await runOcr(files, options);
      setResult(response);
      setActiveFile(0);
      setPageView(format === 'text' ? 'text' : 'markdown');
      const summary = response.summary;
      if (summary.error_count > 0) {
        notify.warning(`识别完成，${summary.error_count} 个文件有失败页`);
      } else {
        notify.success(`识别完成：${summary.page_count} 页`);
      }
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : '识别失败';
      setError(message);
      notify.error(message);
    } finally {
      setRunning(false);
    }
  }

  const active = result?.results[activeFile] || null;
  const allMarkdown = useMemo(() => {
    if (!result) return '';
    return result.results
      .map((file) => {
        const body = file.pages.map((page) => page.markdown || page.text).join('\n\n');
        return result.results.length > 1 ? `# ${file.filename}\n\n${body}` : body;
      })
      .join('\n\n---\n\n')
      .trim();
  }, [result]);

  return (
    <div className="min-h-full box-border px-[48px] pt-[20px] pb-[43px] max-[900px]:px-[16px]">
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title={`Agent 广场 · ${entry.name}`}
      />

      <div className="flex h-[calc(100vh-190px)] min-h-[560px] flex-col gap-[14px] rounded-[20px] bg-white p-[16px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-center justify-end gap-[10px]">
          <AgentAppBackButton />
        </div>

        <div className="flex min-h-0 flex-1 gap-[18px] max-[900px]:flex-col">
          {/* ---------------- 左栏：文件 + 参数 ---------------- */}
          <div className="flex w-[320px] shrink-0 flex-col border-r-[0.5px] border-[#eef0f5] pr-[16px] max-[900px]:w-full max-[900px]:border-r-0 max-[900px]:border-b-[0.5px] max-[900px]:pb-[16px]">
            <div className="flex min-h-0 flex-1 flex-col gap-[12px] overflow-y-auto">
              {status && !status.configured && (
                <div className="rounded-[8px] bg-[#fff7ed] px-[10px] py-[8px] text-[11px] leading-[1.6] text-[#c2410c]">
                  OCR 服务尚未配置，请联系平台维护者设置服务地址后再使用。
                </div>
              )}

              <div
                onDragOver={(event) => {
                  event.preventDefault();
                  setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={handleDrop}
                onClick={() => inputRef.current?.click()}
                className={cn(
                  'flex cursor-pointer flex-col items-center justify-center gap-[6px] rounded-[10px] border-[0.8px] border-dashed px-[12px] py-[18px] text-center transition-colors',
                  dragging
                    ? 'border-[#18181a] bg-[#f6f7f9]'
                    : 'border-[#d8dee9] bg-[#fbfbfc] hover:border-[#b9c2d2] hover:bg-[#f6f7f9]',
                )}
              >
                <input
                  ref={inputRef}
                  type="file"
                  multiple
                  accept={ACCEPT}
                  aria-label="选择要识别的文件"
                  className="hidden"
                  onChange={(event) => {
                    addFiles(Array.from(event.target.files || []));
                    event.target.value = '';
                  }}
                />
                <Upload className="size-[16px] text-[#858b9c]" />
                <span className="text-[12px] font-medium text-[#18181a]">拖入文件，或点击选择</span>
                <span className={HINT_CLASS}>
                  {ACCEPT_LABEL}；单次 ≤ {limits.maxFiles} 个、单个 ≤ {limits.maxFileMb}MB
                </span>
              </div>

              {files.length > 0 && (
                <div className="flex flex-col gap-[6px]">
                  {files.map((file, index) => (
                    <div
                      key={`${file.name}-${file.size}`}
                      className="group flex items-center gap-[8px] rounded-[8px] border-[0.5px] border-[#eef0f5] px-[8px] py-[6px]"
                    >
                      {file.type.startsWith('image/') ? (
                        <ImageIcon className="size-[13px] shrink-0 text-[#858b9c]" />
                      ) : (
                        <FileText className="size-[13px] shrink-0 text-[#858b9c]" />
                      )}
                      <span className="min-w-0 flex-1 truncate text-[11px] text-[#4f5669]" title={file.name}>
                        {file.name}
                      </span>
                      <span className="shrink-0 text-[10px] text-[#a3aab9]">{formatSize(file.size)}</span>
                      <button
                        type="button"
                        aria-label={`移除 ${file.name}`}
                        onClick={() => removeFile(index)}
                        className="flex size-[18px] shrink-0 items-center justify-center rounded-[4px] text-[#a3aab9] transition-colors hover:bg-[#f2f4f8] hover:text-[#18181a]"
                      >
                        <X className="size-[11px]" />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex flex-col gap-[6px]">
                <div className={FIELD_LABEL_CLASS}>输出形态</div>
                <div className={cn(SEGMENT_CLASS, 'w-full')}>
                  {FORMAT_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      title={option.hint}
                      aria-pressed={format === option.value}
                      onClick={() => setFormat(option.value)}
                      className={cn(
                        'flex-1 rounded-[6px] py-[4px] text-[11px] transition-colors',
                        format === option.value
                          ? 'bg-[#18181a] font-medium text-white'
                          : 'text-[#757f9c] hover:text-[#18181a]',
                      )}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <div className={HINT_CLASS}>{FORMAT_OPTIONS.find((row) => row.value === format)?.hint}</div>
              </div>

              <div className="flex flex-col gap-[6px]">
                <div className={FIELD_LABEL_CLASS}>识别模式</div>
                <div className={cn(SEGMENT_CLASS, 'w-full')}>
                  {[
                    { value: '' as const, label: '自动', hint: '有文字层就用文字层，没有才 OCR' },
                    { value: 'ocr' as const, label: '强制 OCR', hint: '忽略文字层，整页走图文识别' },
                  ].map((option) => (
                    <button
                      key={option.value || 'auto'}
                      type="button"
                      title={option.hint}
                      aria-pressed={mode === option.value}
                      onClick={() => setMode(option.value)}
                      className={cn(
                        'flex-1 rounded-[6px] py-[4px] text-[11px] transition-colors',
                        mode === option.value
                          ? 'bg-[#18181a] font-medium text-white'
                          : 'text-[#757f9c] hover:text-[#18181a]',
                      )}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-[8px]">
                <div className="flex flex-col gap-[6px]">
                  <div className={FIELD_LABEL_CLASS}>页数上限</div>
                  <input
                    type="number"
                    min={1}
                    max={2000}
                    value={maxPages ?? ''}
                    placeholder="全部"
                    onChange={(event) => {
                      const next = event.target.value.trim();
                      setMaxPages(next ? Math.min(2000, Math.max(1, Number(next) || 1)) : null);
                    }}
                    className={FIELD_INPUT_CLASS}
                  />
                </div>
                <div className="flex flex-col gap-[6px]">
                  <div className={FIELD_LABEL_CLASS}>DPI</div>
                  <input
                    type="number"
                    min={72}
                    max={600}
                    value={dpi ?? ''}
                    placeholder={String(DEFAULT_DPI)}
                    onChange={(event) => {
                      const next = event.target.value.trim();
                      setDpi(next ? Math.min(600, Math.max(72, Number(next) || DEFAULT_DPI)) : null);
                    }}
                    className={FIELD_INPUT_CLASS}
                  />
                </div>
              </div>

              <div className="flex flex-col gap-[6px]">
                <div className={FIELD_LABEL_CLASS}>
                  <span>PDF 密码</span>
                  <span className="rounded-[4px] border-[0.5px] border-[#e3e7f1] px-[5px] py-[1px] text-[10px] font-normal text-[#858b9c]">
                    选填
                  </span>
                </div>
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="加密 PDF 才需要"
                  className={FIELD_INPUT_CLASS}
                />
              </div>

              <div className="flex flex-col gap-[8px]">
                <div className={FIELD_LABEL_CLASS}>识别内容</div>
                {[
                  { label: '公式', hint: '把公式转成 LaTeX，适合论文与教材', checked: math, onChange: setMath },
                  { label: '页眉页脚', hint: '保留页眉页脚（默认会过滤掉）', checked: furniture, onChange: setFurniture },
                  { label: '图片内文字', hint: '识别插图里的文字', checked: imageOcr, onChange: setImageOcr },
                ].map((row) => (
                  <label key={row.label} className="flex cursor-pointer items-start gap-[8px]">
                    <Checkbox
                      checked={row.checked}
                      onCheckedChange={() => row.onChange(!row.checked)}
                      aria-label={row.label}
                      className="mt-[2px]"
                    />
                    <span className="min-w-0">
                      <span className="block text-[12px] leading-[1.5] text-[#4f5669]">{row.label}</span>
                      <span className="block text-[10px] leading-[1.4] text-[#a3aab9]">{row.hint}</span>
                    </span>
                  </label>
                ))}
              </div>

              <div className="flex flex-col gap-[8px]">
                <div className={FIELD_LABEL_CLASS}>结构化附加</div>
                {[
                  { label: '行坐标', hint: '每行给 [x0,y0,x1,y1] 与置信度', checked: includeLines, onChange: setIncludeLines },
                  { label: '版面块', hint: '标题 / 表格 / 段落等版面标签', checked: includeBlocks, onChange: setIncludeBlocks },
                  { label: '耗时明细', hint: '每页各阶段耗时，排障用', checked: includeTimings, onChange: setIncludeTimings },
                ].map((row) => (
                  <label key={row.label} className="flex cursor-pointer items-start gap-[8px]">
                    <Checkbox
                      checked={row.checked}
                      onCheckedChange={() => row.onChange(!row.checked)}
                      aria-label={row.label}
                      className="mt-[2px]"
                    />
                    <span className="min-w-0">
                      <span className="block text-[12px] leading-[1.5] text-[#4f5669]">{row.label}</span>
                      <span className="block text-[10px] leading-[1.4] text-[#a3aab9]">{row.hint}</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            <div className="flex shrink-0 flex-col gap-[8px] border-t-[0.5px] border-[#eef0f5] pt-[12px]">
              <span className={HINT_CLASS}>识别在 OCR 服务内完成，不消耗大模型额度，也不需要配置模型。</span>
              <UIButton
                onClick={() => void start()}
                disabled={!canRun}
                className="h-[38px] w-full justify-center gap-[6px] rounded-[10px] bg-[#18181a] px-[12px] text-[13px] font-normal text-white hover:bg-[#303030] disabled:cursor-not-allowed disabled:bg-[#c9cfda]"
              >
                {running && <LoaderCircle className="size-[14px] animate-spin" />}
                {running ? '识别中…' : '开始识别'}
              </UIButton>
            </div>
          </div>

          {/* ---------------- 右栏：结果 ---------------- */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {!result && !running && !error && (
              <div className="grid min-h-[320px] flex-1 place-items-center content-center gap-[8px] rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] px-[24px] py-[40px] text-center">
                <FileText className="size-[20px] text-[#c2c8d4]" />
                <span className="text-[14px] font-medium text-[#18181a]">上传文件后开始识别</span>
                <span className="max-w-[420px] text-[12px] leading-[1.7] text-[#858b9c]">
                  左边选好输出形态与识别内容，点「开始识别」。结果按页返回：
                  Markdown、纯文本，以及可选的逐行坐标与版面块。
                </span>
                <div className="mt-[6px] flex flex-wrap items-center justify-center gap-[6px]">
                  {(['text', 'ocr', 'hybrid'] as const).map((key) => (
                    <span
                      key={key}
                      className={cn('rounded-[4px] px-[6px] py-[1px] text-[10px]', SOURCE_META[key].className)}
                    >
                      {SOURCE_META[key].label} · {SOURCE_META[key].hint}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {running && (
              <div className="flex flex-1 flex-col items-center justify-center gap-[10px] rounded-[12px] border-[0.5px] border-[#eef0f5]">
                <LoaderCircle className="size-[18px] animate-spin text-[#858b9c]" />
                <span className="text-[12px] text-[#4f5669]">正在识别 {files.length} 个文件…</span>
                <span className={HINT_CLASS}>扫描件按页处理，页数多时会久一些</span>
              </div>
            )}

            {!running && error && (
              <div className="flex flex-1 flex-col items-start justify-center gap-[8px] rounded-[12px] border-[0.5px] border-[#f3d0d0] bg-[#fdf7f7] px-[20px] py-[24px]">
                <span className="text-[13px] font-medium text-[#c0392b]">识别失败</span>
                <span className="text-[12px] leading-[1.7] text-[#8a4b4b]">{error}</span>
                <UIButton
                  variant="outline"
                  onClick={() => void start()}
                  className="mt-[4px] h-[30px] rounded-[8px] px-[12px] text-[12px]"
                >
                  重试
                </UIButton>
              </div>
            )}

            {!running && result && (
              <>
                <div className="flex flex-wrap items-center gap-[8px] border-b-[0.5px] border-[#eef0f5] pb-[10px]">
                  <div className="flex min-w-0 items-center gap-[6px]">
                    <span className="truncate text-[13px] font-medium text-[#18181a]" title={active?.filename}>
                      {active?.filename || '识别结果'}
                    </span>
                    <span className="shrink-0 text-[11px] text-[#858b9c]">
                      {active ? `${active.n_pages} 页` : ''}
                      {active?.elapsed_ms ? ` · ${formatMs(active.elapsed_ms)}` : ''}
                    </span>
                  </div>
                  <span className="rounded-[4px] bg-[#f2f4f8] px-[6px] py-[1px] text-[10px] text-[#6b7280]">
                    {result.model}
                  </span>
                  <div className="ml-auto flex items-center gap-[6px]">
                    <button
                      type="button"
                      onClick={() => copyText(allMarkdown, '已复制全部 Markdown')}
                      disabled={!allMarkdown}
                      className="flex h-[28px] items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[8px] text-[11px] text-[#4f5669] transition-colors hover:border-[#cbd3e6] hover:bg-[#f6f7f9] disabled:opacity-40"
                    >
                      <Copy className="size-[12px]" />
                      复制全部
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        downloadText(`${stripExtension(active?.filename || 'ocr')}.md`, allMarkdown, 'text/markdown;charset=utf-8')
                      }
                      disabled={!allMarkdown}
                      className="flex h-[28px] items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[8px] text-[11px] text-[#4f5669] transition-colors hover:border-[#cbd3e6] hover:bg-[#f6f7f9] disabled:opacity-40"
                    >
                      <Download className="size-[12px]" />
                      .md
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        downloadText(
                          `${stripExtension(active?.filename || 'ocr')}.json`,
                          JSON.stringify(result, null, 2),
                          'application/json;charset=utf-8',
                        )
                      }
                      className="flex h-[28px] items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] px-[8px] text-[11px] text-[#4f5669] transition-colors hover:border-[#cbd3e6] hover:bg-[#f6f7f9]"
                    >
                      <Download className="size-[12px]" />
                      .json
                    </button>
                  </div>
                </div>

                {result.results.length > 1 && (
                  <div className="mt-[8px] flex flex-wrap gap-[6px]">
                    {result.results.map((file, index) => (
                      <button
                        key={`${file.filename}-${index}`}
                        type="button"
                        onClick={() => setActiveFile(index)}
                        className={cn(
                          'flex max-w-[220px] items-center gap-[6px] rounded-[8px] border-[0.5px] px-[8px] py-[3px] text-[11px] transition-colors',
                          index === activeFile
                            ? 'border-[#18181a] bg-[#18181a] text-white'
                            : 'border-[#e3e7f1] text-[#4f5669] hover:border-[#cbd3e6] hover:bg-[#f6f7f9]',
                        )}
                      >
                        <span className="truncate">{file.filename}</span>
                        <span
                          className={cn(
                            'shrink-0 text-[10px]',
                            index === activeFile ? 'text-white/70' : 'text-[#a3aab9]',
                          )}
                        >
                          {file.n_pages} 页
                        </span>
                        {file.error && <span className="shrink-0 text-[10px] text-[#f0a3a3]">失败</span>}
                      </button>
                    ))}
                  </div>
                )}

                <div className="mt-[10px] min-h-0 flex-1 overflow-y-auto pr-[2px]">
                  {active ? (
                    <FileBlock file={active} view={pageView} onViewChange={setPageView} />
                  ) : (
                    <div className="grid place-items-center py-[40px] text-[12px] text-[#a3aab9]">没有结果</div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
