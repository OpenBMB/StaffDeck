import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import IconRefresh from '@/assets/icons/refresh.svg?react';
import { api, TENANT_ID } from '@/api/client';
import { notify } from '@/components/ui';
import { prefersReducedMotion } from '@/lib/motion';
import { cn } from '@/lib/utils';
import { AGENT_CATALOG, findAgentCatalogEntry } from '@/lib/agentCatalog';
import type { AgentProfileRead } from '@/types';

/**
 * 「全部」tab 右列组件（原站点「首页」页拆解而来，2026-10-13 首页下线后并入开放广场）：
 *
 * - `QuickAccessTiles`——快捷访问磁贴网格（demo qtile 同款：两列行卡 + 追光 3D 倾斜 +
 *   虚线「添加快捷方式」格）。「管理 →」进入编辑：悬停删除、添加候选、重置；「保存」写 localStorage。
 *   浏览态点虚线格也能直接添加（无「保存」按钮）→ 那条路径**添加即落盘**，否则刷新即丢。
 * - `NewsPanel`——「AI 圈 / 旅文圈」新闻 TOP，后端按天缓存 RSS 抓取结果，一日一更新；
 *   前端只渲染两组各 3 条（共 6 条，数量按整页排版定）。
 */

// ---------- 类型 ----------

type PlazaChip =
  | { kind: 'employee'; agentId: string; name: string; color: string }
  | { kind: 'agent-app'; entryId: string; name: string; color: string }
  | { kind: 'link'; url: string; name: string; color: string };

type NewsItem = {
  category: 'ai' | 'travel';
  title: string;
  summary: string;
  source: string;
  url: string;
  published_at: string;
};

type NewsPayload = { date: string; items: { ai?: NewsItem[]; travel?: NewsItem[] }; provenance: string };

// ---------- 常量 ----------

const LAYOUT_STORAGE_KEY = 'ultrarag_plaza_home_layout_v1';

const PALETTE = ['#0f7268', '#c2703e', '#6f7b42', '#5b6fa8', '#8a5aa8'];

// 默认磁贴：Agent 应用清单全部上墙（本地清单即时可得，不依赖接口）。
function defaultChips(): PlazaChip[] {
  return AGENT_CATALOG.slice(0, 6).map((entry, index) => ({
    kind: 'agent-app',
    entryId: entry.entry,
    name: entry.name,
    color: PALETTE[index % PALETTE.length],
  }));
}

// 兼容两代存储：v1 = { chips: [{chip, x, y}] }（自由画布），v2 = { chips: [chip] }（磁贴）。
function loadChips(): PlazaChip[] | null {
  try {
    const raw = JSON.parse(window.localStorage.getItem(LAYOUT_STORAGE_KEY) || 'null');
    if (!Array.isArray(raw?.chips)) return null;
    const chips = raw.chips
      .map((row: unknown): PlazaChip | null => {
        const chip = row && typeof row === 'object' && 'chip' in (row as Record<string, unknown>)
          ? (row as { chip: PlazaChip }).chip
          : (row as PlazaChip);
        return chip && typeof chip.kind === 'string' ? chip : null;
      })
      .filter((chip: PlazaChip | null): chip is PlazaChip => chip !== null);
    return chips.length > 0 ? chips : null;
  } catch {
    return null;
  }
}

function saveChips(chips: PlazaChip[]) {
  try {
    window.localStorage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify({ v: 2, chips }));
  } catch {
    // 隐私模式等场景写不进就算了，布局丢不了数据
  }
}

function chipKey(chip: PlazaChip): string {
  if (chip.kind === 'employee') return `employee:${chip.agentId}`;
  if (chip.kind === 'agent-app') return `agent-app:${chip.entryId}`;
  return `link:${chip.url}`;
}

const KIND_LABEL: Record<PlazaChip['kind'], string> = {
  employee: 'WORKER',
  'agent-app': 'AGENT APP',
  link: 'LINK',
};

/** 磁贴线性图标（demo 描边风格，颜色跟随磁贴色）。 */
function ChipIcon({ chip, className }: { chip: PlazaChip; className?: string }) {
  const common = {
    className,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.8,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  };
  if (chip.kind === 'employee') {
    return (
      <svg {...common}>
        <circle cx="9" cy="8" r="3.5" />
        <path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" />
        <circle cx="17.5" cy="9.5" r="2.5" />
        <path d="M16 14.7c2.9.2 4.9 1.9 5.5 5.3" />
      </svg>
    );
  }
  if (chip.kind === 'link') {
    return (
      <svg {...common}>
        <path d="M10 14 20.5 3.5M15 4h5.5v5.5" />
        <path d="M19 13.5V19a1.5 1.5 0 0 1-1.5 1.5h-12A1.5 1.5 0 0 1 4 19V7a1.5 1.5 0 0 1 1.5-1.5H11" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="m14.5 5.5 4 4L8 20H4v-4z" />
      <path d="m12.5 7.5 4 4" />
    </svg>
  );
}

// ---------- 子组件 ----------

function QuickTile({
  chip,
  editing,
  onRemove,
  onOpen,
}: {
  chip: PlazaChip;
  editing: boolean;
  onRemove: () => void;
  onOpen: () => void;
}) {
  const reduce = prefersReducedMotion();

  return (
    <div
      className="group relative"
      onMouseMove={(event) => {
        if (editing) return;
        const el = event.currentTarget.querySelector<HTMLElement>('[data-tile]');
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const px = (event.clientX - rect.left) / rect.width;
        const py = (event.clientY - rect.top) / rect.height;
        el.style.setProperty('--mx', `${px * 100}%`);
        el.style.setProperty('--my', `${py * 100}%`);
        if (!reduce) {
          el.style.transform = `perspective(600px) rotateX(${(0.5 - py) * 5}deg) rotateY(${(px - 0.5) * 5}deg)`;
        }
      }}
      onMouseLeave={(event) => {
        const el = event.currentTarget.querySelector<HTMLElement>('[data-tile]');
        if (el) el.style.transform = '';
      }}
    >
      <button
        type="button"
        data-tile
        onClick={() => {
          if (!editing) onOpen();
        }}
        className={cn(
          'relative flex w-full items-center gap-[12px] overflow-hidden rounded-[16px] border border-black/[0.07] bg-white p-[14px] text-left shadow-[0_2px_8px_rgba(17,17,17,0.04)] transition-[box-shadow,border-color] duration-300 hover:border-[rgba(15,114,110,0.3)] hover:shadow-[0_24px_56px_rgba(17,17,17,0.10)]',
          editing && 'cursor-default',
        )}
        title={chip.name}
      >
        {/* 追光：radial 渐变跟随鼠标（--mx/--my 写在磁贴元素上） */}
        <span
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-200 group-hover:opacity-100"
          style={{ background: `radial-gradient(180px circle at var(--mx,50%) var(--my,50%), color-mix(in srgb, ${chip.color} 10%, transparent), transparent 60%)` }}
        />
        <span
          className="flex h-[38px] w-[38px] flex-none items-center justify-center rounded-[12px] border border-black/[0.07] bg-[#fbfbfa]"
          style={{ color: chip.color }}
        >
          <ChipIcon chip={chip} className="size-[17px]" />
        </span>
        <span className="min-w-0 flex-1">
          <b className="block overflow-hidden text-ellipsis whitespace-nowrap text-[13px] font-bold text-[#18181a]">
            {chip.name}
          </b>
          <small className="mt-[1px] block font-mono text-[10px] tracking-[0.05em] text-[#757f9c]">
            {KIND_LABEL[chip.kind]}
          </small>
        </span>
        <span
          aria-hidden
          className="absolute right-[12px] top-1/2 -translate-x-[5px] -translate-y-1/2 font-mono text-[12px] font-medium text-[#0f7268] opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:opacity-100"
        >
          →
        </span>
      </button>
      {editing && (
        <button
          type="button"
          aria-label={`移除 ${chip.name}`}
          onClick={(event) => {
            event.stopPropagation();
            onRemove();
          }}
          className="absolute -right-[7px] -top-[7px] z-[2] hidden h-[20px] w-[20px] items-center justify-center rounded-full border border-black/[0.08] bg-white text-[11px] text-[#757f9c] hover:border-[#c0392b] hover:text-[#c0392b] group-hover:flex"
        >
          ✕
        </button>
      )}
    </div>
  );
}

export function NewsPanel() {
  const [payload, setPayload] = useState<NewsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const load = useCallback((force = false) => {
    setLoading(true);
    setError('');
    api
      .get<NewsPayload>(`/api/enterprise/plaza-news?tenant_id=${TENANT_ID}${force ? '&refresh=1' : ''}`)
      .then(setPayload)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '新闻加载失败'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const groups = useMemo(() => {
    const ai = (payload?.items?.ai || []).slice(0, 3);
    const travel = (payload?.items?.travel || []).slice(0, 3);
    return [
      { key: 'ai', label: 'AI 圈', items: ai },
      { key: 'travel', label: '旅文圈', items: travel },
    ];
  }, [payload]);

  let rank = 0;

  return (
    <section className="flex min-h-0 w-full flex-1 flex-col overflow-hidden rounded-[20px] border border-black/[0.06] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur max-[1200px]:w-[320px]">
      <div className="flex items-center justify-between border-b border-black/[0.05] px-[18px] py-[14px]">
        <div className="flex items-center gap-[9px] text-[14.5px] font-semibold text-[#18181a]">
          🔥 AI 圈 · 旅文圈
        </div>
        <button
          type="button"
          onClick={() => load(true)}
          className="flex items-center gap-[6px] rounded-[9px] px-[8px] py-[5px] text-[12px] text-[#5b6274] transition-colors hover:bg-[#f4f2ec]"
          title="换一批（后端按天缓存，一日一更新）"
        >
          <IconRefresh className={cn('size-[13px]', loading && 'animate-spin')} />
          刷新
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto py-[6px]">
        {loading && !payload && (
          <div className="flex flex-col gap-[12px] px-[18px] py-[14px]">
            {Array.from({ length: 6 }).map((_, index) => (
              <div key={index} className="animate-pulse">
                <div className="mb-[8px] h-[13px] w-[85%] rounded bg-[#efede7]" />
                <div className="mb-[6px] h-[10px] w-full rounded bg-[#f3f1ec]" />
                <div className="h-[10px] w-[55%] rounded bg-[#f3f1ec]" />
              </div>
            ))}
          </div>
        )}
        {error && (
          <div className="px-[18px] py-[30px] text-center text-[12.5px] text-[#757f9c]">
            {error}
            <button type="button" onClick={() => load()} className="ml-[8px] text-[#0f7268] underline">
              重试
            </button>
          </div>
        )}
        {groups.map((group) => (
          <div key={group.key}>
            <div className="flex items-center gap-[8px] px-[18px] pb-[4px] pt-[12px]">
              <span
                className={cn(
                  'rounded-full px-[9px] py-[2px] text-[11px] font-semibold',
                  group.key === 'ai' ? 'bg-[#e3efed] text-[#0f7268]' : 'bg-[#f7ece4] text-[#c2703e]',
                )}
              >
                {group.label}
              </span>
              {group.key === 'travel' && payload?.date && (
                <span className="text-[11px] text-[#757f9c]">{payload.date} 更新</span>
              )}
            </div>
            {group.items.map((item) => {
              rank += 1;
              const color = PALETTE[(rank - 1) % PALETTE.length];
              return (
                <a
                  key={item.url + item.title}
                  href={item.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="group flex gap-[13px] border-b border-[#f2f0ea] px-[18px] py-[13px] last:border-b-0 hover:bg-[#fbfaf7]"
                >
                  <span
                    className="mt-[2px] flex h-[30px] w-[30px] flex-none items-center justify-center rounded-[9px] text-[12.5px] font-bold tabular-nums"
                    style={{ backgroundColor: `color-mix(in srgb, ${color} 11%, #fff)`, color }}
                  >
                    {String(rank).padStart(2, '0')}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[13px] font-semibold leading-[1.45] text-[#18181a] group-hover:text-[#0f7268]">
                      {item.title}
                      <span className="ml-[4px] text-[11px] font-normal text-[#757f9c]">↗</span>
                    </span>
                    {item.summary && (
                      <span className="mt-[3px] block text-[11.5px] leading-[1.6] text-[#5b6274]">{item.summary}</span>
                    )}
                    <span className="mt-[6px] flex items-center gap-[8px] text-[11px] text-[#757f9c]">
                      {item.source && (
                        <span
                          className="rounded-full px-[8px] py-[2px] font-medium"
                          style={{ backgroundColor: `color-mix(in srgb, ${color} 9%, #fff)`, color: `color-mix(in srgb, ${color} 80%, #000)` }}
                        >
                          {item.source}
                        </span>
                      )}
                      {item.published_at && <span>{item.published_at}</span>}
                    </span>
                  </span>
                </a>
              );
            })}
            {group.items.length === 0 && !loading && (
              <div className="px-[18px] pb-[10px] text-[12px] text-[#757f9c]">暂无内容</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------- 添加候选弹层 ----------

function AddChipDialog({
  open,
  employees,
  existingKeys,
  onClose,
  onPickEmployee,
  onPickAgentApp,
  onPickLink,
}: {
  open: boolean;
  employees: AgentProfileRead[];
  existingKeys: Set<string>;
  onClose: () => void;
  onPickEmployee: (agent: AgentProfileRead) => void;
  onPickAgentApp: (entryId: string) => void;
  onPickLink: (name: string, url: string) => void;
}) {
  const [tab, setTab] = useState<'employees' | 'agents' | 'links'>('employees');
  const [linkName, setLinkName] = useState('');
  const [linkUrl, setLinkUrl] = useState('');
  const colorOf = (index: number) => PALETTE[index % PALETTE.length];
  const reduce = prefersReducedMotion();

  // Esc 关闭 + 打开期间锁定背景滚动
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [open, onClose]);

  if (!open) return null;

  // portal 挂 body：外层卡片有 backdrop-blur / transform 时，fixed 会被当作
  // 相对卡片定位导致弹层被裁切——必须逃逸到文档根节点才能全屏居中。
  return createPortal(
    <div
      className={cn(
        'fixed inset-0 z-[100] flex items-center justify-center bg-[rgba(28,28,30,0.32)] backdrop-blur-[3px]',
        !reduce && 'animate-[sd-fade-in_.2s_ease-out]',
      )}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="添加快捷方式"
        className={cn(
          'flex max-h-[78vh] w-[460px] flex-col overflow-hidden rounded-[18px] border border-black/[0.05] bg-white shadow-[0_24px_64px_-20px_rgba(28,28,30,0.4)]',
          !reduce && 'animate-[sd-pop-in_.26s_cubic-bezier(0.16,1,0.3,1)]',
        )}
      >
        <div className="flex items-center justify-between px-[20px] pb-[12px] pt-[16px]">
          <h3 className="text-[15px] font-semibold text-[#18181a]">添加快捷方式</h3>
          <button
            type="button"
            onClick={onClose}
            className="h-[28px] w-[28px] rounded-[8px] text-[16px] text-[#757f9c] hover:bg-[#f4f2ec] hover:text-[#18181a]"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>
        <div className="flex gap-[6px] border-b border-black/[0.05] px-[20px] pb-[12px]">
          {(
            [
              ['employees', '数字员工'],
              ['agents', 'Agent 应用'],
              ['links', '外部链接'],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setTab(value)}
              className={cn(
                'rounded-full border px-[13px] py-[6px] text-[12.5px] transition-colors',
                tab === value
                  ? 'border-[rgba(15,114,104,0.18)] bg-[#e3efed] font-semibold text-[#0f7268]'
                  : 'border-transparent text-[#5b6274] hover:bg-[#f4f2ec]',
              )}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === 'employees' && (
          <div className="flex flex-col gap-[6px] overflow-y-auto p-[14px]">
            {employees.length === 0 && (
              <div className="py-[20px] text-center text-[12.5px] text-[#757f9c]">暂无已发布到广场的数字员工</div>
            )}
            {employees.map((agent, index) => {
              const key = `employee:${agent.id}`;
              const added = existingKeys.has(key);
              return (
                <button
                  key={agent.id}
                  type="button"
                  disabled={added}
                  onClick={() => onPickEmployee(agent)}
                  className={cn(
                    'flex items-center gap-[12px] rounded-[12px] border border-black/[0.05] bg-white px-[12px] py-[10px] text-left transition-colors',
                    added ? 'cursor-not-allowed opacity-50' : 'hover:border-[#0f7268] hover:bg-[#f7fbfa]',
                  )}
                >
                  <span
                    className="flex h-[36px] w-[36px] flex-none items-center justify-center rounded-[10px] text-[15px] font-semibold text-white"
                    style={{ backgroundColor: colorOf(index) }}
                  >
                    {(agent.name || '?').slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-[#18181a]">{agent.name}</span>
                    <span className="mt-[2px] block text-[11.5px] text-[#757f9c]">
                      {agent.description || '数字员工'}
                    </span>
                  </span>
                  <span className="text-[11.5px] font-semibold text-[#0f7268]">{added ? '已添加' : '＋ 添加'}</span>
                </button>
              );
            })}
          </div>
        )}

        {tab === 'agents' && (
          <div className="flex flex-col gap-[6px] overflow-y-auto p-[14px]">
            {AGENT_CATALOG.map((entry, index) => {
              const key = `agent-app:${entry.entry}`;
              const added = existingKeys.has(key);
              return (
                <button
                  key={entry.id}
                  type="button"
                  disabled={added}
                  onClick={() => onPickAgentApp(entry.entry)}
                  className={cn(
                    'flex items-center gap-[12px] rounded-[12px] border border-black/[0.05] bg-white px-[12px] py-[10px] text-left transition-colors',
                    added ? 'cursor-not-allowed opacity-50' : 'hover:border-[#0f7268] hover:bg-[#f7fbfa]',
                  )}
                >
                  <span
                    className="flex h-[36px] w-[36px] flex-none items-center justify-center rounded-[10px] text-[15px] font-semibold text-white"
                    style={{ backgroundColor: colorOf(index) }}
                  >
                    {entry.name.slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[13px] font-semibold text-[#18181a]">{entry.name}</span>
                    <span className="mt-[2px] block text-[11.5px] text-[#757f9c]">{entry.summary}</span>
                  </span>
                  <span className="text-[11.5px] font-semibold text-[#0f7268]">{added ? '已添加' : '＋ 添加'}</span>
                </button>
              );
            })}
          </div>
        )}

        {tab === 'links' && (
          <div className="flex flex-col gap-[10px] p-[20px]">
            <label className="text-[12px] font-medium text-[#5b6274]">名称</label>
            <input
              value={linkName}
              onChange={(event) => setLinkName(event.target.value)}
              placeholder="例如：Hacker News"
              className="rounded-[10px] border border-black/[0.05] px-[12px] py-[9px] text-[13px] outline-none focus:border-[#0f7268] focus:shadow-[0_0_0_3px_rgba(15,114,104,0.1)]"
            />
            <label className="text-[12px] font-medium text-[#5b6274]">链接地址</label>
            <input
              value={linkUrl}
              onChange={(event) => setLinkUrl(event.target.value)}
              placeholder="https://…"
              className="rounded-[10px] border border-black/[0.05] px-[12px] py-[9px] text-[13px] outline-none focus:border-[#0f7268] focus:shadow-[0_0_0_3px_rgba(15,114,104,0.1)]"
            />
            <button
              type="button"
              className="mt-[4px] self-start rounded-[9px] bg-[#0f7268] px-[16px] py-[7px] text-[12.5px] font-medium text-white hover:bg-[#0c6158]"
              onClick={() => {
                if (!linkName.trim() || !linkUrl.trim()) {
                  notify.warning('请填写名称和链接');
                  return;
                }
                onPickLink(linkName.trim(), linkUrl.trim());
                setLinkName('');
                setLinkUrl('');
              }}
            >
              添加到快捷访问
            </button>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

// ---------- 快捷访问磁贴网格（demo qtile 同款） ----------

export function QuickAccessTiles() {
  const navigate = useNavigate();
  const [mode, setMode] = useState<'view' | 'edit'>('view');
  const [chips, setChips] = useState<PlazaChip[]>(() => loadChips() || defaultChips());
  const [addOpen, setAddOpen] = useState(false);
  const [employees, setEmployees] = useState<AgentProfileRead[]>([]);

  // 编辑进入时的快照，「取消」恢复
  const snapshotRef = useRef<PlazaChip[]>([]);

  // 数字员工候选（进页面拉一次；失败静默——只影响候选列表）
  useEffect(() => {
    api
      .get<AgentProfileRead[]>(`/api/enterprise/agents?tenant_id=${TENANT_ID}`)
      .then((rows) =>
        // 整体智能体（is_overall 宿主）是系统资源池，不是可添加的岗位员工，与其他页面口径一致排除
        setEmployees((Array.isArray(rows) ? rows : []).filter((row) => !row.is_overall)),
      )
      .catch(() => setEmployees([]));
  }, []);

  // 复用广场「使用此员工」的完整链路：标记 used → 写入员工作用域 → 进员工档案。
  // 接口失败（已用过/网络抖动）不拦跳转，保证快捷方式始终可达。
  const handleUseEmployee = useCallback(
    (agentId: string) => {
      void (async () => {
        try {
          await api.post(`/api/chat/agents/${agentId}/use?tenant_id=${TENANT_ID}`, {});
        } catch {
          // 忽略：是否已用过不影响后续跳转
        }
        window.localStorage.setItem('ultrarag_enterprise_agent_scope', agentId);
        window.dispatchEvent(new Event('ultrarag-enterprise-agent-scope-refresh'));
        window.dispatchEvent(
          new CustomEvent('ultrarag-enterprise-agent-scope-change', { detail: { agentId } }),
        );
        navigate('/enterprise/dashboard');
      })();
    },
    [navigate],
  );

  const openChip = useCallback(
    (chip: PlazaChip) => {
      if (chip.kind === 'employee') handleUseEmployee(chip.agentId);
      else if (chip.kind === 'agent-app') navigate(`/enterprise/agent-apps/${encodeURIComponent(chip.entryId)}`);
      else window.open(chip.url, '_blank', 'noopener');
    },
    [handleUseEmployee, navigate],
  );

  const existingKeys = useMemo(() => new Set(chips.map((row) => chipKey(row))), [chips]);

  const startEdit = useCallback(() => {
    snapshotRef.current = chips;
    setMode('edit');
  }, [chips]);

  const cancelEdit = useCallback(() => {
    setChips(snapshotRef.current);
    setMode('view');
  }, []);

  const saveEdit = useCallback(() => {
    saveChips(chips);
    setMode('view');
    notify.success('快捷访问已保存');
  }, [chips]);

  const resetLayout = useCallback(() => {
    setChips(defaultChips());
    notify.success('已重置为默认布局，保存后生效');
  }, []);

  // 浏览态也能点虚线格添加（demo 同款），但那条路径没有「保存」按钮兜底 ——
  // 只改 state 不落盘，刷新或切 tab 重挂载就回到默认布局。故：浏览态添加即持久化，
  // 编辑态仍走「保存 / 取消」暂存语义。
  const addChip = useCallback(
    (chip: PlazaChip) => {
      const next = [...chips, chip];
      setChips(next);
      setAddOpen(false);
      if (mode === 'edit') return;
      saveChips(next);
      notify.success(`已添加「${chip.name}」到快捷访问`);
    },
    [chips, mode],
  );

  const removeChip = useCallback((key: string) => {
    setChips((current) => current.filter((row) => chipKey(row) !== key));
  }, []);

  return (
    <section
      className="w-full rounded-[20px] border border-black/[0.06] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur"
      aria-label="快捷访问"
    >
      <div className="flex items-center justify-between px-[24px] pb-[14px] pt-[22px]">
        <h2 className="text-[19px] font-black tracking-[-0.02em] text-[#18181a]">快捷访问</h2>
        <div className="flex items-center gap-[6px]">
          {mode === 'edit' ? (
            <>
              <button
                type="button"
                onClick={resetLayout}
                className="rounded-[9px] px-[9px] py-[5px] font-mono text-[11px] text-[#5b6274] transition-colors hover:bg-[#f4f2ec]"
              >
                ↺ 重置
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                className="rounded-[9px] border border-[#e3e7f1] bg-white px-[12px] py-[5px] font-mono text-[11px] text-[#5b6274] transition-colors hover:border-[#cbd3e6]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={saveEdit}
                className="rounded-[9px] bg-[#18181a] px-[12px] py-[5px] font-mono text-[11px] font-medium text-white transition-colors hover:bg-[#18181a]/90"
              >
                保存
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={startEdit}
              className="inline-flex items-center gap-[5px] font-mono text-[11px] font-medium text-[#0f7268] transition-opacity hover:opacity-70"
            >
              管理 →
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-[10px] px-[24px] pb-[20px] min-[460px]:grid-cols-2">
        {chips.map((chip) => {
          const key = chipKey(chip);
          return (
            <QuickTile
              key={key}
              chip={chip}
              editing={mode === 'edit'}
              onRemove={() => removeChip(key)}
              onOpen={() => openChip(chip)}
            />
          );
        })}
        {chips.length === 0 && (
          <div className="col-span-full py-[18px] text-center text-[12.5px] text-[#757f9c]">
            还没有快捷方式——点下方虚线格添加
          </div>
        )}
        {/* 虚线添加格（demo 同款，两种模式都在；浏览态点击直达添加弹层） */}
        <button
          type="button"
          onClick={() => setAddOpen(true)}
          className="grid min-h-[66px] place-items-center rounded-[16px] border border-dashed border-black/[0.12] font-mono text-[11px] text-[#757f9c] transition-colors hover:border-[rgba(15,114,110,0.45)] hover:text-[#0f7268]"
        >
          + 添加快捷方式
        </button>
      </div>

      <AddChipDialog
        open={addOpen}
        employees={employees}
        existingKeys={existingKeys}
        onClose={() => setAddOpen(false)}
        onPickEmployee={(agent) =>
          addChip({ kind: 'employee', agentId: agent.id, name: agent.name || agent.id, color: PALETTE[chips.length % PALETTE.length] })
        }
        onPickAgentApp={(entryId) => {
          const entry = findAgentCatalogEntry(entryId);
          addChip({ kind: 'agent-app', entryId, name: entry?.name || entryId, color: PALETTE[chips.length % PALETTE.length] });
        }}
        onPickLink={(name, url) =>
          addChip({ kind: 'link', url, name, color: PALETTE[chips.length % PALETTE.length] })
        }
      />
    </section>
  );
}
