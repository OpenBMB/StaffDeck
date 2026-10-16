import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, TENANT_ID } from '@/api/client';
import { cn } from '@/lib/utils';
import { reveal, settle, prefersReducedMotion } from '@/lib/motion';
import { findAgentCatalogEntry } from '@/lib/agentCatalog';
import { emitAgentScopeChange, persistSharedAgentScope } from '@/lib/agent-scope-storage';
import { NewsPanel, QuickAccessTiles } from '@/components/openPlatform/PlazaWidgets';

/**
 * 开放广场「全部」tab —— 全广场热门资源总览（视觉对照 demo：design-mockups/plaza/tab-all-hot.html）：
 *
 * 氛围层（极光光斑 / 26px 点阵 / 噪点颗粒）+ 编辑式大标题 +
 * 左列：№1 主推大卡（鼠标追光 + 3D 倾斜）+ 热门排行榜 Top 10（真实计数，
 * 后端 /api/enterprise/plaza-hot 与广场可见性同口径）。
 * 点击行为见 `openHotItem`：榜单里的「数字员工」直进该员工的工作台（不落在广场 tab），
 * 目录型 Agent（如 OA 单据助手）进对应工作台路由，其余资源类型仍跳对应广场 tab。
 * 右列：快捷访问画布（可编辑摆放）+ 首页同款新闻（AI 圈 · 旅文圈）。
 */

type HotItem = {
  kind: string; // agents | general-skills | skills | knowledge
  ref_id: string;
  title: string;
  description: string;
  metric_label: string;
  metric_value: number;
  score: number;
  updated_at: string | null;
};

type HotPayload = { generated_at: string; items: HotItem[] };

const KIND_META: Record<string, { label: string; en: string }> = {
  agents: { label: '数字员工', en: 'AI STAFF' },
  'general-skills': { label: '技能', en: 'SKILL' },
  skills: { label: 'SOP', en: 'SOP' },
  knowledge: { label: '知识库', en: 'KNOWLEDGE' },
};

/** 各资源类型的线性图标（demo 同款描边风格）。 */
function KindIcon({ kind, className }: { kind: string; className?: string }) {
  const paths: Record<string, React.ReactNode> = {
    agents: (
      <>
        <circle cx="9" cy="8" r="3.5" />
        <path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" />
        <circle cx="17.5" cy="9.5" r="2.5" />
        <path d="M16 14.7c2.9.2 4.9 1.9 5.5 5.3" />
      </>
    ),
    'general-skills': (
      <>
        <path d="m14.5 5.5 4 4L8 20H4v-4z" />
        <path d="m12.5 7.5 4 4" />
      </>
    ),
    skills: (
      <>
        <rect x="5.5" y="3.5" width="13" height="17" rx="2" />
        <path d="M9 8h6M9 12h6M9 16h3.5" />
      </>
    ),
    knowledge: (
      <path d="M3.5 7A2.5 2.5 0 0 1 6 4.5h4l2 2.5h6A2.5 2.5 0 0 1 20.5 9.5v7A2.5 2.5 0 0 1 18 19H6a2.5 2.5 0 0 1-2.5-2.5z" />
    ),
  };
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {paths[kind] || paths['general-skills']}
    </svg>
  );
}

const fmt = (value: number) => value.toLocaleString('zh-CN');

/** ISO 时间戳 → MM-DD HH:mm（解析失败原样返回）。 */
function fmtDate(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 鼠标追光 + 3D 倾斜的通用 handler（reduced-motion 时只更新追光位置）。 */
function useSpotlightTilt(reduce: boolean) {
  return (event: React.MouseEvent<HTMLElement>) => {
    const el = event.currentTarget;
    const rect = el.getBoundingClientRect();
    const px = (event.clientX - rect.left) / rect.width;
    const py = (event.clientY - rect.top) / rect.height;
    el.style.setProperty('--mx', `${px * 100}%`);
    el.style.setProperty('--my', `${py * 100}%`);
    if (!reduce) {
      el.style.transform = `perspective(600px) rotateX(${(0.5 - py) * 4}deg) rotateY(${(px - 0.5) * 4}deg)`;
    }
  };
}

function resetTilt(event: React.MouseEvent<HTMLElement>) {
  event.currentTarget.style.transform = '';
}

/** №1 主推大卡：本期热度冠军（demo feat-card 同款）。 */
function ChampionCard({ item, onOpen }: { item: HotItem; onOpen: () => void }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const reduce = prefersReducedMotion();
  const onSpotlightMove = useSpotlightTilt(reduce);
  const meta = KIND_META[item.kind] || { label: item.kind, en: item.kind.toUpperCase() };

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || reduce) return;
    const animation = reveal(el, { y: 16, duration: 560 });
    return () => {
      animation?.pause();
      settle(el, { y: 16 });
    };
  }, [item.ref_id, reduce]);

  const updated = item.updated_at ? fmtDate(item.updated_at) : '';

  return (
    <div ref={wrapRef}>
      <section
        ref={cardRef}
        aria-label="本期热度冠军资源"
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onOpen();
          }
        }}
        onMouseMove={onSpotlightMove}
        onMouseLeave={resetTilt}
        className="group relative w-full cursor-pointer overflow-hidden rounded-[20px] border border-black/[0.06] bg-white/85 text-left shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur transition-[box-shadow,border-color] duration-300 hover:border-[rgba(15,114,110,0.28)] hover:shadow-[0_24px_56px_rgba(17,17,17,0.10)]"
      >
        {/* 追光：radial 渐变跟随鼠标（--mx/--my 由 handler 写入） */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-0 transition-opacity duration-300 group-hover:opacity-100"
          style={{ background: 'radial-gradient(340px circle at var(--mx,50%) var(--my,50%), rgba(15,114,110,0.09), transparent 62%)' }}
        />
        {/* 右上角 № 角标：hover 放大点亮 */}
        <span className="pointer-events-none absolute right-[24px] top-[20px] font-mono text-[11px] tracking-[0.08em] text-[#757f9c] transition-all duration-300 group-hover:scale-[1.2] group-hover:font-bold group-hover:text-[#0f7268]">
          № 01 · 热度冠军
        </span>

        <div className="relative p-[24px_26px_14px]">
          <span className="flex h-[46px] w-[46px] items-center justify-center rounded-[15px] border border-black/[0.07] bg-white shadow-[0_2px_8px_rgba(17,17,17,0.04)] text-[#0f7268] transition-transform duration-500 [transform:rotate(-3deg)] group-hover:[transform:rotate(2deg)_scale(1.04)]">
            <KindIcon kind={item.kind} className="size-[21px]" />
          </span>
          <span className="mt-[14px] block font-mono text-[10px] font-medium uppercase tracking-[0.14em] text-[#0f7268]">
            {meta.en} · Champion
          </span>
          <h3 className="mt-[5px] text-[24px] font-black leading-tight tracking-[-0.025em] text-[#18181a]">
            {item.title}
          </h3>
          {item.description && (
            <p className="mt-[7px] max-w-[460px] text-[13px] leading-[1.7] text-[#5b6274]">
              {item.description}
            </p>
          )}

          <div className="mt-[10px] flex items-end gap-[26px]">
            <div className="flex min-w-[150px] flex-col gap-[9px]">
              <span>
                <b className="block font-mono text-[22px] font-bold leading-none tracking-[-0.02em] tabular-nums text-[#18181a]">
                  {fmt(item.metric_value)}
                </b>
                <span className="mt-[4px] block text-[10.5px] text-[#757f9c]">{item.metric_label}</span>
              </span>
              <span>
                <b className="block font-mono text-[22px] font-bold leading-none tracking-[-0.02em] tabular-nums text-[#0f7268]">
                  {fmt(Math.round(item.score))}
                </b>
                <span className="mt-[4px] block text-[10.5px] text-[#757f9c]">综合热度</span>
              </span>
            </div>
            {/* 右侧大水印编号：保持 demo 右侧体量 */}
            <span
              aria-hidden
              className="ml-auto select-none self-end font-mono text-[64px] font-bold leading-none tracking-[-0.04em] text-[#0f7268]/[0.10] transition-colors duration-500 group-hover:text-[#0f7268]/[0.16]"
            >
              01
            </span>
          </div>
        </div>

        <div className="relative flex items-center gap-[8px] border-t border-dashed border-black/[0.12] px-[26px] py-[10px] font-mono text-[11px] text-[#757f9c]">
          <span>{meta.label}</span>
          <i className="h-[3px] w-[3px] rounded-full bg-black/25" />
          <span>{updated ? `更新于 ${updated}` : '按真实使用计数'}</span>
          <span className="ml-auto inline-flex -translate-x-[6px] items-center gap-[4px] font-medium text-[#0f7268] opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:opacity-100">
            打开 →
          </span>
        </div>
      </section>
    </div>
  );
}

/** 热门排行榜单行（demo rank-row 同款 grid：名次 / 图标 / 名称 / 热度条）。 */
function RankRow({
  item,
  rank,
  index,
  percent,
  onOpen,
}: {
  item: HotItem;
  rank: number;
  index: number;
  percent: number;
  onOpen: () => void;
}) {
  const reduce = prefersReducedMotion();
  const [entered, setEntered] = useState(reduce);
  const [barOn, setBarOn] = useState(reduce);
  const top3 = rank <= 3;
  const meta = KIND_META[item.kind] || { label: item.kind, en: item.kind.toUpperCase() };

  useEffect(() => {
    if (reduce) return;
    const t1 = window.setTimeout(() => setEntered(true), 80 + index * 60);
    const t2 = window.setTimeout(() => setBarOn(true), 450 + index * 60);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
    };
  }, [index, reduce, item.ref_id]);

  return (
    <li
      className={cn(
        'transition-[opacity,transform] duration-500 ease-[cubic-bezier(0.16,1,0.3,1)]',
        index > 0 && 'border-t border-dashed border-black/[0.06]',
      )}
      style={entered ? undefined : { opacity: 0, transform: 'translateX(-14px)' }}
    >
      <button
        type="button"
        onClick={onOpen}
        className="grid w-full grid-cols-[46px_44px_minmax(0,1fr)_minmax(150px,210px)] items-center gap-[14px] rounded-[16px] px-[14px] py-[12px] text-left transition-colors duration-200 hover:bg-[rgba(15,114,110,0.05)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#0f7268]/40 max-[900px]:grid-cols-[40px_40px_minmax(0,1fr)] max-[900px]:[&>*:last-child]:hidden"
      >
        {/* 名次：前三名 teal 大号 */}
        <span
          className={cn(
            'font-mono font-bold tabular-nums leading-none',
            top3 ? 'text-[24px] text-[#0f7268]' : 'text-[15px] text-[#b9bfcc]',
          )}
        >
          {String(rank).padStart(2, '0')}
        </span>
        {/* 图标框 */}
        <span className="flex h-[42px] w-[42px] items-center justify-center rounded-[13px] border border-black/[0.07] bg-white shadow-[0_2px_8px_rgba(17,17,17,0.04)] text-[#0f7268]">
          <KindIcon kind={item.kind} className="size-[18px]" />
        </span>
        {/* 名称 + 类型 */}
        <span className="min-w-0">
          <b className="block overflow-hidden text-ellipsis whitespace-nowrap text-[14px] font-bold tracking-[-0.01em] text-[#18181a]">
            {item.title}
          </b>
          <small className="mt-[2px] block font-mono text-[10px] tracking-[0.05em] text-[#757f9c]">
            {meta.en}
          </small>
        </span>
        {/* 热度条 + 数值 */}
        <span className="flex items-center gap-[10px]">
          <span className="h-[6px] flex-1 overflow-hidden rounded-full bg-black/[0.06]">
            <span
              className="block h-full rounded-full transition-[width] duration-[1100ms] ease-[cubic-bezier(0.16,1,0.3,1)]"
              style={{
                width: barOn ? `${Math.max(percent, 4)}%` : '0%',
                background: top3
                  ? 'linear-gradient(90deg, #0f7268, #4bd3be)'
                  : 'linear-gradient(90deg, rgba(15,114,110,0.45), #0f7268)',
              }}
            />
          </span>
          <span className="min-w-[64px] text-right font-mono text-[11.5px] font-medium tabular-nums text-[#5b6274]">
            {fmt(item.metric_value)} {item.metric_label}
          </span>
        </span>
      </button>
    </li>
  );
}

/** 氛围层：极光光斑 + 点阵 + 噪点颗粒（demo 同款，仅覆盖本 tab 内容区）。 */
function Atmosphere() {
  return (
    <>
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden rounded-[20px]">
        <i
          className="absolute left-[-6%] top-[2%] h-[360px] w-[480px] animate-[sd-drift-a_18s_ease-in-out_infinite_alternate] rounded-full blur-[70px]"
          style={{ background: 'radial-gradient(circle, rgba(15,114,110,0.16), transparent 68%)' }}
        />
        <i
          className="absolute right-[-6%] top-[30%] h-[380px] w-[440px] animate-[sd-drift-b_22s_ease-in-out_infinite_alternate] rounded-full blur-[80px]"
          style={{ background: 'radial-gradient(circle, rgba(168,93,50,0.12), transparent 68%)' }}
        />
        <i
          className="absolute left-[26%] top-[62%] h-[340px] w-[460px] animate-[sd-drift-a_26s_ease-in-out_infinite_alternate-reverse] rounded-full blur-[80px]"
          style={{ background: 'radial-gradient(circle, rgba(111,123,66,0.12), transparent 70%)' }}
        />
      </div>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 overflow-hidden rounded-[20px]"
        style={{
          backgroundImage: 'radial-gradient(rgba(24,24,26,0.05) 1px, transparent 1px)',
          backgroundSize: '26px 26px',
          WebkitMaskImage: 'radial-gradient(ellipse at 55% 18%, black, transparent 62%)',
          maskImage: 'radial-gradient(ellipse at 55% 18%, black, transparent 62%)',
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 z-[5] opacity-[0.035] mix-blend-multiply"
        style={{
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")",
        }}
      />
    </>
  );
}

export default function PlazaAllTab({ toolbar }: { toolbar?: ReactNode }) {
  const navigate = useNavigate();
  const [payload, setPayload] = useState<HotPayload | null>(null);
  const [error, setError] = useState('');
  const reduce = prefersReducedMotion();

  useEffect(() => {
    api
      .get<HotPayload>(`/api/enterprise/plaza-hot?tenant_id=${TENANT_ID}`)
      .then(setPayload)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : '热门数据加载失败'));
  }, []);

  const items = payload?.items || [];
  const champion = items[0];
  const rest = items.slice(1, 10);
  const maxScore = useMemo(() => items.reduce((acc, item) => Math.max(acc, item.score), 0) || 1, [items]);

  const openKind = (kind: string) => navigate(`/enterprise/platform/${kind}`);

  /**
   * 榜单成员的「打开」落点。
   *
   * 数字员工（kind === 'agents'）在榜单里只是一个名字 + 热度，跳进广场 tab 等于把用户
   * 丢回列表再让他自己找一遍，所以这里直进该员工的工作台：
   * 1. 目录型 Agent（`agent-catalog.json` 里的条目，如 OA 单据助手）→ 工作台路由
   *    `/enterprise/agent-apps/:entry`，它们在 `agent_profiles` 里没有记录，不能走员工作用域；
   * 2. 真·数字员工 → 先 `/use` 收进「我的数字员工」（失败也不挡跳转），再切换当前员工
   *    作用域并落到员工工作台，与「我的数字员工」列表点卡片的落点保持一致。
   *
   * 其余类型（技能 / SOP / 知识库）没有单资源页，保持跳对应广场 tab。
   */
  const openHotItem = (item: HotItem) => {
    if (item.kind !== 'agents') {
      openKind(item.kind);
      return;
    }
    const catalogEntry = findAgentCatalogEntry(item.ref_id);
    if (catalogEntry) {
      navigate(`/enterprise/agent-apps/${encodeURIComponent(catalogEntry.entry)}`);
      return;
    }
    void api
      .post(`/api/chat/agents/${encodeURIComponent(item.ref_id)}/use?tenant_id=${TENANT_ID}`, {})
      .catch(() => {});
    persistSharedAgentScope(item.ref_id);
    emitAgentScopeChange(item.ref_id);
    navigate('/enterprise/dashboard');
  };

  return (
    <div className="relative">
      <Atmosphere />

      <div className="relative z-[1]">
        {/* ===== 广场分类胶囊栏（页头文案已按需求移除） ===== */}
        {toolbar && <div>{toolbar}</div>}

        {/* ===== 双栏：排行 7 : 快捷 5 ===== */}
        <div className="mt-[24px] grid grid-cols-1 items-stretch gap-[16px] lg:grid-cols-[7fr_5fr]">          {/* 左列：№1 主推 + 排行榜 */}
          <div className="flex min-w-0 flex-col gap-[16px]">
            {error && (
              <div className="grid min-h-[120px] place-items-center rounded-[20px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] text-[12.5px] text-[#8b94aa]">
                {error}
              </div>
            )}
            {!error && !payload && (
              <div className="flex flex-col gap-[16px]">
                <div className="h-[190px] animate-pulse rounded-[20px] bg-[#f4f2ec]" />
                <div className="h-[420px] animate-pulse rounded-[20px] bg-[#f6f4ef]" />
              </div>
            )}
            {!error && payload && !champion && (
              <div className="grid min-h-[180px] place-items-center content-center gap-[10px] rounded-[20px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center font-bold text-[#8b94aa]">
                <span>广场还没有可展示的热门资源</span>
              </div>
            )}
            {!error && champion && (
              <>
                <ChampionCard item={champion} onOpen={() => openHotItem(champion)} />

                <section
                  aria-label="热门排行榜"
                  className="overflow-hidden rounded-[20px] border border-black/[0.06] bg-white/85 shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur"
                >
                  <div className="flex items-baseline justify-between gap-[16px] px-[26px] pb-[16px] pt-[24px]">
                    <h2 className="flex items-center gap-[10px] text-[19px] font-black tracking-[-0.02em] text-[#18181a]">
                      <svg
                        viewBox="0 0 24 24"
                        className="size-[19px] text-[#0f7268]"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={1.8}
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden
                      >
                        <path d="M12 2.5c1 3-.5 4.5-2 6C8.5 10 7 11.8 7 14.5a5 5 0 0 0 10 0c0-1.6-.6-3-1.5-4.2-.4 1-1 1.7-2 2.2.6-2.6.2-6.4-1.5-10z" />
                      </svg>
                      热门排行榜
                      <span className="font-mono text-[10.5px] font-normal text-[#757f9c]">
                        · <b className="font-medium text-[#0f7268]">全站</b> TOP 10
                      </span>
                    </h2>
                    <span className="font-mono text-[10.5px] text-[#757f9c]">
                      {payload?.generated_at ? `更新于 ${fmtDate(payload.generated_at)}` : '按真实使用计数'}
                    </span>
                  </div>
                  <ol className="px-[10px] pb-[16px]">
                    {rest.map((item, index) => (
                      <RankRow
                        key={item.kind + item.ref_id}
                        item={item}
                        rank={index + 2}
                        index={index}
                        percent={Math.round((item.score / maxScore) * 100)}
                        onOpen={() => openHotItem(item)}
                      />
                    ))}
                    {rest.length === 0 && (
                      <li className="px-[18px] py-[22px] text-center text-[12.5px] text-[#757f9c]">
                        榜单暂时只有 1 个资源，去各广场发布更多内容吧
                      </li>
                    )}
                  </ol>
                </section>
              </>
            )}
          </div>

          {/* 右列：快捷访问 + 新闻 */}
          <div className="flex min-w-0 flex-col gap-[16px]">
            <QuickAccessTiles />
            <NewsPanel />
          </div>
        </div>
      </div>
    </div>
  );
}
