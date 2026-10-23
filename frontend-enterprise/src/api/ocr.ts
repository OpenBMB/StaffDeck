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

/** 「API 接入」示例所需的接入信息。`public_base_url` 来自后端 PUBLIC_BASE_URL，可能为空。 */
export type OcrApiAccess = {
  required_scope: string;
  endpoint_path: string;
  docs_path: string;
  public_base_url: string;
};

/**
 * 识别参数。字段名与 `indra.md` 的 `/v1/ocr` 一一对应；
 * `null` / `undefined` 表示不传，由上游用默认值。
 *
 * 工作台只暴露这几项：识别模式固定「自动」、DPI 固定 200、页眉页脚固定过滤，
 * 结构化附加（行坐标 / 版面块）固定开启 —— 这些固定值见下面的 `FIXED_FORM_FIELDS`。
 */
export type OcrOptions = {
  format: OcrFormat;
  maxPages: number | null;
  password: string;
  math: boolean | null;
  imageOcr: boolean | null;
};

/**
 * 工作台不再给用户的项：一律取默认值。
 *
 * - `dpi` 200：与上游默认一致，显式传是为了不让上游改默认值后行为漂移；
 * - `furniture` false：页眉页脚一律过滤掉；
 * - `include_lines` / `include_blocks` true：「行」「版面块」两个视图的前提，
 *   与上游默认一致；
 * - `mode` 不传 = 自动（有文字层用文字层，没有才 OCR）；
 * - `include_timings` 不传 = 不带各阶段耗时。
 */
const FIXED_FORM_FIELDS: Record<string, string> = {
  dpi: '200',
  furniture: 'false',
  include_lines: 'true',
  include_blocks: 'true',
};

export async function fetchOcrStatus(): Promise<OcrStatus> {
  return api.get<OcrStatus>('/api/enterprise/agent-apps/ocr:status');
}

export function fetchOcrApiAccess(): Promise<OcrApiAccess> {
  return api.get<OcrApiAccess>('/api/enterprise/agent-apps/ocr:api-access');
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
  if (options.maxPages) form.append('max_pages', String(options.maxPages));
  if (options.password.trim()) form.append('password', options.password.trim());
  // 只有用户显式勾过才发：上游对 math / image_ocr 的默认值是关，不必替它决定
  if (options.math !== null) form.append('math', options.math ? 'true' : 'false');
  if (options.imageOcr !== null) form.append('image_ocr', options.imageOcr ? 'true' : 'false');
  Object.entries(FIXED_FORM_FIELDS).forEach(([key, value]) => form.append(key, value));

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
