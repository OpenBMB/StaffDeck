import rawCatalog from '../data/agent-catalog.json';

/**
 * Agent 广场的定制 Agent 清单。
 *
 * 清单由 `src/data/agent-catalog.json` 维护（见该文件的 `_note`）：想让平台多一个开箱即用的
 * Agent，只在 JSON 里加一条即可，不需要建表、不需要后端接口。条目对所有成员一致可见 ——
 * 这类 Agent 是平台统一定制的，成员只能「使用」，不能编辑或删除。
 *
 * 因此这里的加载器只做两件事：把 JSON 收敛成稳定的结构（字段缺失/类型不对不炸页面）、
 * 按 `entry` 提供查找（详情抽屉与工作台路由共用同一个 key）。
 */
export type AgentCatalogEntry = {
  /** 稳定标识，用于列表 key。 */
  id: string;
  name: string;
  /** 一句话简介，卡片描述用。 */
  summary: string;
  /** 详情抽屉里的长描述。 */
  description: string;
  author: string;
  updatedAt: string;
  category: string;
  tags: string[];
  /** 工作台路由 key：`/enterprise/agent-apps/:entryId`。 */
  entry: string;
  /** 服务端能力标识，前端据此选择工作台实现（`slides` / `doc-review` / `ocr` / `decision` / `code-review`，未知能力给提示页）。 */
  capability: string;
  /**
   * 仅管理员可见（如 AI CodeReviewer）。
   *
   * 广场列表按 `listAgentCatalog(isAdmin)` 过滤；工作台路由在 AgentAppPage 里再挡一层，
   * 防止深链直进。普通成员对这类条目完全无感。
   */
  adminOnly: boolean;
  /**
   * 该 Agent 预留的固定口径 prompt（如「旅文汇报样式」）。
   *
   * 只在清单里维护一份；当前使用页面不展示样式勾选项、不随生成请求下发。
   * 后端 `slides:generate` 仍接受 `style_prompt`（默认空串），如需恢复勾选项只需接回清单字段。
   */
  prompt: string;
  status: 'active' | 'offline';
};

function asText(value: unknown, fallback = ''): string {
  const text = typeof value === 'string'
    ? value.trim()
    : typeof value === 'number' && Number.isFinite(value)
      ? String(value)
      : '';
  // 空串等同于「没写」：作者/分类这类字段留空时应该落到默认值，而不是显示空白
  return text || fallback;
}

function asTags(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => asText(item)).filter(Boolean);
}

/**
 * 把任意 JSON 收敛成 Agent 清单：非法条目直接丢弃而不是抛错 ——
 * 一个手写错的字段不应该让整个广场 tab 白屏。
 */
export function normalizeAgentCatalog(input: unknown): AgentCatalogEntry[] {
  const rows = Array.isArray(input)
    ? input
    : Array.isArray((input as { agents?: unknown })?.agents)
      ? ((input as { agents: unknown[] }).agents)
      : [];
  const seen = new Set<string>();
  const entries: AgentCatalogEntry[] = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue;
    const record = row as Record<string, unknown>;
    const id = asText(record.id);
    const name = asText(record.name);
    const entry = asText(record.entry) || id;
    if (!id || !name || !entry || seen.has(entry)) continue;
    seen.add(entry);
    const status = asText(record.status).toLowerCase() === 'offline' ? 'offline' : 'active';
    entries.push({
      id,
      name,
      summary: asText(record.summary),
      description: asText(record.description),
      author: asText(record.author, '平台内置'),
      updatedAt: asText(record.updated_at),
      category: asText(record.category, '通用'),
      tags: asTags(record.tags),
      entry,
      capability: asText(record.capability),
      adminOnly: record.admin_only === true,
      prompt: asText(record.prompt),
      status,
    });
  }
  return entries;
}

export const AGENT_CATALOG: AgentCatalogEntry[] = normalizeAgentCatalog(rawCatalog);

/**
 * 广场列表用：只展示在线的 Agent。
 *
 * `adminOnly` 条目只在管理员视角出现；调用方（开放广场页 / 使用页守卫）必须把当前用户的
 * 管理员身份传进来，缺省按非管理员处理 —— 宁可少展示也不能多展示。
 */
export function listAgentCatalog(isAdmin = false): AgentCatalogEntry[] {
  return AGENT_CATALOG.filter((entry) => entry.status === 'active' && (!entry.adminOnly || isAdmin));
}

export function findAgentCatalogEntry(entryId: string | undefined): AgentCatalogEntry | null {
  if (!entryId) return null;
  return AGENT_CATALOG.find((row) => row.entry === entryId || row.id === entryId) || null;
}

/** 卡片的 meta 行：作者 + 更新时间。 */
export function agentCatalogMeta(entry: AgentCatalogEntry): string {
  const parts = [entry.author];
  if (entry.updatedAt) parts.push(`更新于 ${entry.updatedAt}`);
  return parts.join(' · ');
}
