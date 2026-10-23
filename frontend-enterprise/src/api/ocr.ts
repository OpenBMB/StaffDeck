import { ApiError, api, authHeader, API_BASE_URL, TENANT_ID } from './client';

/**
 * OCR 助手（Agent 广场 `ocr` 能力）的前端 API。
 *
 * 上游是 Indra OCR 服务（契约见仓库根目录 `indra.md`），由后端 `/ocr:run` 代理：
 * 密钥在内网，浏览器直连不了，也不该拿到 `X-API-Key`。
 *
 * 注意这个能力**不需要模型配置**：识别在 OCR 服务里完成，不消耗大模型额度。
 */

/** 输出形态：`json` 带行坐标与版面块；`md` / `text` 是纯文本，体积小得多。 */
export type OcrFormat = 'json' | 'md' | 'text';

/** 一页的识别结果。`source` 说明这页最终用的哪条路（文本层 / OCR / 混合）。 */
export type OcrPage = {
  index: number;
  source: 'ocr' | 'text' | 'hybrid' | 'error';
  note: string;
  rotation: number;
  skew: number;
  vertical: boolean;
  n_lines: number;
  markdown: string;
  text: string;
  lines: OcrLine[];
  blocks: OcrBlock[];
  timings: Record<string, number>;
};

/** 一行文字 + 归一化后的坐标 `[x0, y0, x1, y1]`（上游按页内像素给）。 */
export type OcrLine = {
  box?: number[];
  text?: string;
  conf?: number;
};

/** 版面块（标题 / 表格 / 段落…），标签由上游版面模型给出。 */
export type OcrBlock = {
  label?: string;
  bbox?: number[];
  text?: string;
};

export type OcrFileResult = {
  filename: string;
  n_pages: number;
  elapsed_ms: number | null;
  /** 该文件整体失败时的原因；为 null 表示整份成功。 */
  error: string | null;
  pages: OcrPage[];
};

export type OcrResponse = {
  model: string;
  results: OcrFileResult[];
  summary: {
    file_count: number;
    page_count: number;
    elapsed_ms: number | null;
    error_count: number;
  };
};

export type OcrStatus = {
  configured: boolean;
  formats: string[];
  max_files: number;
  max_file_mb: number;
};

/**
 * 识别参数。字段名与 `indra.md` 的 `/v1/ocr` 一一对应；
 * `null` / `undefined` 表示不传，由上游用默认值。
 */
export type OcrOptions = {
  format: OcrFormat;
  mode: '' | 'ocr';
  dpi: number | null;
  maxPages: number | null;
  password: string;
  math: boolean | null;
  furniture: boolean | null;
  imageOcr: boolean | null;
  includeLines: boolean | null;
  includeBlocks: boolean | null;
  includeTimings: boolean | null;
};

export async function fetchOcrStatus(): Promise<OcrStatus> {
  return api.get<OcrStatus>('/api/enterprise/agent-apps/ocr:status');
}

/**
 * 上传若干文件并识别。multipart 走 fetch —— `api.post` 系列只发 JSON body。
 */
export async function runOcr(
  files: File[],
  options: OcrOptions,
  signal?: AbortSignal,
): Promise<OcrResponse> {
  const form = new FormData();
  form.append('tenant_id', TENANT_ID);
  files.forEach((file) => form.append('files', file));
  form.append('format', options.format);
  if (options.mode) form.append('mode', options.mode);
  if (options.dpi) form.append('dpi', String(options.dpi));
  if (options.maxPages) form.append('max_pages', String(options.maxPages));
  if (options.password.trim()) form.append('password', options.password.trim());
  const flags: Array<[string, boolean | null]> = [
    ['math', options.math],
    ['furniture', options.furniture],
    ['image_ocr', options.imageOcr],
    ['include_lines', options.includeLines],
    ['include_blocks', options.includeBlocks],
    ['include_timings', options.includeTimings],
  ];
  flags.forEach(([key, value]) => {
    // 只有用户显式选过才发：上游对 include_lines/include_blocks 的默认值是 true，
    // 一律发 false 会把行坐标悄悄关掉。
    if (value !== null) form.append(key, value ? 'true' : 'false');
  });

  const response = await fetch(`${API_BASE_URL}/api/enterprise/agent-apps/ocr:run`, {
    method: 'POST',
    headers: { ...authHeader() },
    body: form,
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<OcrResponse>;
}
