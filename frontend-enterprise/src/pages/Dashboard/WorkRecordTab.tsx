import { useMemo } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

import IconGrowthArrow from '../../assets/icons/growth-arrow.svg?react';
import IconCardArrow from '../../assets/icons/card-arrow.svg?react';
import IconCapFolder from '../../assets/icons/cap-folder.svg?react';
import IconCapMagicWand from '../../assets/icons/cap-magicwand.svg?react';
import IconCapClipboard from '../../assets/icons/cap-clipboard.svg?react';
import IconCapBriefcase from '../../assets/icons/cap-briefcase.svg?react';
import IconProfileAlarm from '../../assets/icons/profile-alarm.svg?react';
import IconProfileCalendar from '../../assets/icons/profile-calendar.svg?react';
import capabilityLogs from '../../assets/staffdeck/capabilityLogs.png';
import capabilityTasks from '../../assets/staffdeck/capabilityTasks.png';
import capabilityTools from '../../assets/staffdeck/capabilityTools.png';
import StaffdeckIcon from '../../components/StaffdeckIcon';
import { staffdeckDisplayText } from '../../employee';
import type {
  AgentProfileRead,
  EnterpriseChatSessionRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';

export type ReplyStats = {
  total: number;
  today: number;
  byDay: Record<string, number>;
};

const HEATMAP_ROWS = 7;
const HEATMAP_COLUMNS = 33;
const HEATMAP_BUCKETS = HEATMAP_ROWS * HEATMAP_COLUMNS;
// Rolling window: from the current month one year ago (left) to the current month (right).
const HEATMAP_MONTH_SLOTS = 13;
// Rows are Sun→Sat (第一行周日); labels only on 周一 / 周三 / 周五.
const HEATMAP_WEEKDAY_LABELS = ['', '周一', '', '周三', '', '周五', ''];
const HEATMAP_CELL_LEVELS = [
  'bg-[#f6f6f6] in-data-[theme=dark]:bg-[#363944]',
  'bg-[#cfd5e2] in-data-[theme=dark]:bg-[#5a6274]',
  'bg-[#9aa3ba] in-data-[theme=dark]:bg-[#7b8498]',
  'bg-[#6a7488] in-data-[theme=dark]:bg-[#a4adbf]',
  'bg-[#464c5e] in-data-[theme=dark]:bg-[#f0f2f6]',
];

type GrowthEvent = {
  id: string;
  kind: string;
  title: string;
  description: string;
  timestamp: string;
  icon: ReactNode;
  tone: string;
};

type GrowthTimestampSource = {
  created_at?: string;
  updated_at?: string;
  metadata?: Record<string, unknown>;
};

export type WorkRecordTabProps = {
  selectedAgent: AgentProfileRead;
  activeKnowledge: KnowledgeBaseRead[];
  activeGeneralSkills: GeneralSkillRead[];
  activeSkills: SkillRead[];
  activeTools: ToolRead[];
  activeScheduledTasks: ScheduledTaskRead[];
  employeeSessions: EnterpriseChatSessionRead[];
  replyStats: ReplyStats;
  positiveRate: number;
  negativeRate: number;
};

const capabilityCardClass = 'group relative flex h-[230px] w-full min-w-0 appearance-none flex-col items-stretch gap-[6px] overflow-hidden rounded-[20px] border px-[24px] py-[20px] text-left transition-[transform,box-shadow] duration-[180ms] ease-[ease] hover:-translate-y-[2px]';
const capabilityLightCardClass = 'border-[#f6f6f6] bg-white shadow-[0_4px_10px_rgba(0,0,0,0.05)] hover:shadow-[0_12px_26px_rgba(0,0,0,0.08)]';
const capabilityDarkCardClass = 'border-[#29282d] bg-[#29282d] text-white shadow-none hover:shadow-[0_12px_26px_rgba(0,0,0,0.28)]';
const capabilityArrowClass = 'pointer-events-none absolute top-[13px] right-[8px] size-[20px] text-[#858b9c] group-data-[tone=dark]:text-[#c7ccd6]';
const capabilityGlyphClass = 'size-[14px] shrink-0 text-[#858b9c] group-data-[tone=dark]:text-white';
const capabilityNameClass = 'min-w-0 truncate text-[14px] font-normal text-[#858b9c] group-data-[tone=dark]:text-white';
const capabilityBarClass = 'block h-[4px] w-full overflow-hidden rounded-[90px] bg-[#e9e9e9] group-data-[tone=dark]:bg-[#6a6a6a]';
const capabilityBarFillClass = 'block h-full w-[20px] rounded-[90px] bg-[#282931] group-data-[tone=dark]:bg-[#e9e9e9]';
const capabilityDescClass = 'line-clamp-5 min-w-0 overflow-hidden text-[10px] leading-[16px] font-normal text-[#757f9c] [overflow-wrap:anywhere] group-data-[tone=dark]:line-clamp-2 group-data-[tone=dark]:text-[#f6f6f6]';

export default function WorkRecordTab({
  selectedAgent,
  activeKnowledge,
  activeGeneralSkills,
  activeSkills,
  activeTools,
  activeScheduledTasks,
  employeeSessions,
  replyStats,
  positiveRate,
  negativeRate,
}: WorkRecordTabProps) {
  const navigate = useNavigate();
  const goToLogs = () => navigate(`/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`);

  const capabilityCards = [
    {
      route: '/enterprise/knowledge',
      title: '知识库',
      tone: 'knowledge',
      count: activeKnowledge.length,
      body: activeKnowledge.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无知识库',
      icon: <IconCapFolder className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/general-skills',
      title: '技能',
      tone: 'skill',
      count: activeGeneralSkills.length,
      body: activeGeneralSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用技能',
      icon: <IconCapMagicWand className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/skills',
      title: 'SOP',
      tone: 'sop',
      count: activeSkills.length,
      body: activeSkills.slice(0, 3).map((item) => staffdeckDisplayText(item.name)).join(' / ') || '暂无启用 SOP',
      icon: <IconCapClipboard className={capabilityGlyphClass} />,
      dark: false,
    },
    {
      route: '/enterprise/tools',
      title: '工具',
      tone: 'tools',
      count: activeTools.length,
      body: activeTools.slice(0, 3).map((item) => staffdeckDisplayText(item.display_name || item.name)).join(' / ') || '暂无启用工具',
      icon: <IconCapBriefcase className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityTools,
    },
    {
      route: '/enterprise/scheduled-tasks',
      title: '定时任务',
      tone: 'tasks',
      count: activeScheduledTasks.length,
      body: activeScheduledTasks.slice(0, 2).map((item) => staffdeckDisplayText(item.title)).join(' / ') || '暂无启用定时任务',
      icon: <IconProfileAlarm className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityTasks,
    },
    {
      route: `/enterprise/feedback?agent_id=${encodeURIComponent(selectedAgent.id)}`,
      title: '对话日志',
      tone: 'logs',
      count: replyStats.total,
      body: staffdeckDisplayText(employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务'),
      icon: <IconProfileCalendar className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityLogs,
    },
  ];

  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeTools);

  return (
    <section className="relative flex w-full min-w-0 max-w-full mt-[-2px] flex-col gap-[24px] overflow-hidden rounded-[18px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] bg-white p-[14px] *:min-w-0 min-[521px]:p-[18px] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:bg-[#202126] in-data-[theme=dark]:text-[#f0f2f6]">
      <div className="flex w-full items-stretch">
        <ClickableMetric label="今日对话" value={replyStats.today} onClick={goToLogs} />
        <ClickableMetric label="累计对话" value={replyStats.total} onClick={goToLogs} />
        <ClickableMetric label="好评率" value={positiveRate} suffix="%" onClick={goToLogs} />
        <ClickableMetric label="差评率" value={negativeRate} suffix="%" onClick={goToLogs} />
      </div>
      <ConversationHeatmap byDay={replyStats.byDay} />
      <div className="flex w-full min-w-0 max-w-full flex-col gap-[10px] mt-[20px]">
        <div className="inline-flex items-center gap-[6px] self-start text-[14px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          <IconGrowthArrow className="size-[14px] shrink-0" />
          成长记录
        </div>
        {growthItems.length ? (
          <div className="relative w-full min-w-0 max-w-full overflow-x-auto">
            <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[20px] pb-[20px]">
              {growthItems.map((item) => (
                <div className="relative flex flex-col items-center gap-[8px]" key={item.id}>
                  <span className="pointer-events-none absolute left-[-10px] right-[-10px] top-[28px] z-0 h-px bg-[#e3e7f1] in-data-[theme=dark]:bg-[#363a45]" />
                  <p className="m-0 text-center text-[12px] font-medium leading-[16px] text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
                    {formatMonthDay(item.timestamp)}
                  </p>
                  <span className="relative z-10 size-[8px] shrink-0 rounded-full bg-[#18181a] in-data-[theme=dark]:bg-[#f0f2f6]" />
                  <div className="relative flex w-[136px] flex-col gap-[4px] rounded-[14px] bg-[#f6f6f6] px-[16px] py-[10px] in-data-[theme=dark]:bg-[#2b2d33]">
                    <span className="absolute top-[-8px] left-1/2 size-0 -translate-x-1/2 border-x-6 border-b-8 border-x-transparent border-b-[#f6f6f6] in-data-[theme=dark]:border-b-[#2b2d33]" />
                    <span className="truncate text-[10px] leading-none text-[#757f9c]">{item.kind}</span>
                    <span className="truncate text-[12px] leading-none text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                      {staffdeckDisplayText(item.title)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <div className="employee-memory-empty">暂无成长轨迹</div>
        )}
      </div>
      <div className="w-full min-w-0 max-w-full overflow-x-auto">
        <div className="grid grid-flow-col auto-cols-[minmax(160px,1fr)] gap-[clamp(18px,2.22vw,32px)]">
        {capabilityCards.map((item) => (
          <button
            type="button"
            key={item.title}
            className={`${capabilityCardClass} ${item.dark ? capabilityDarkCardClass : capabilityLightCardClass}`}
            data-tone={item.dark ? 'dark' : 'light'}
            onClick={() => navigate(item.route)}
          >
            <IconCardArrow className={capabilityArrowClass} />
            <span className="flex flex-col gap-[12px]">
              <span className="flex min-w-0 items-center gap-[6px] pr-[24px]">
                {item.icon}
                <span className={capabilityNameClass}>{item.title}</span>
              </span>
              <span className="flex flex-col gap-[6px]">
                <strong className="text-[24px] leading-none font-semibold text-[#18181a] group-data-[tone=dark]:text-white">{item.count}</strong>
                <span className={capabilityBarClass}><span className={capabilityBarFillClass} /></span>
              </span>
            </span>
            <span className={capabilityDescClass}>{item.body}</span>
            {item.illustration && (
              <img
                className="pointer-events-none absolute bottom-0 left-1/2 h-[84px] w-[120px] -translate-x-1/2 object-contain object-bottom"
                src={item.illustration}
                alt=""
              />
            )}
          </button>
        ))}
        </div>
      </div>
    </section>
  );
}

function ClickableMetric({ label, value, suffix = '', onClick }: { label: string; value: number; suffix?: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-w-px flex-[1_0_0] cursor-pointer flex-col justify-center gap-1 border-[0.5px] border-[#e3e7f1] bg-transparent px-5 py-2.5 text-left transition-colors first:rounded-l-[14px] last:rounded-r-[14px] hover:bg-[#f7f8fa] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:hover:bg-white/5"
    >
      <strong className="text-[18px] font-medium leading-none text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">{value}{suffix}</strong>
      <span className="text-[12px] leading-none text-[#464c5e] in-data-[theme=dark]:text-[#aeb6c6]">{label}</span>
    </button>
  );
}

function ConversationHeatmap({ byDay }: { byDay: Record<string, number> }) {
  const days = useMemo(() => heatmapDays(byDay), [byDay]);
  const rows = useMemo(
    () =>
      Array.from({ length: HEATMAP_ROWS }, (_, row) =>
        Array.from({ length: HEATMAP_COLUMNS }, (_, column) => days[column * HEATMAP_ROWS + row]),
      ),
    [days],
  );
  return (
    <div className="w-full overflow-x-auto overflow-y-hidden">
      <div className="mx-auto flex w-max flex-col gap-[6px]">
        <div className="ml-[52px] grid w-[714px] grid-cols-[repeat(33,10px)] gap-x-[12px] text-[10px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          {monthLabels().map((item) => (
            <span
              key={`${item.label}-${item.offset}`}
              className="whitespace-nowrap"
              style={{ gridColumn: `${item.offset + 1} / span ${item.span}` }}
            >
              {item.label}
            </span>
          ))}
        </div>
        {rows.map((cells, row) => (
          <div className="flex items-center gap-[32px]" key={`row-${row}`}>
            <span className="w-[20px] shrink-0 text-[10px] capitalize leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
              {HEATMAP_WEEKDAY_LABELS[row]}
            </span>
            <div className="flex gap-[12px]">
              {cells.map((day) => (
                <span
                  key={day.key}
                  className={`group relative size-[10px] shrink-0 rounded-[2.5px] border-[0.625px] border-solid border-[#e3e7f1] in-data-[theme=dark]:border-[#363a45] ${HEATMAP_CELL_LEVELS[Math.min(4, day.count)]}`}
                >
                  {day.count > 0 && (
                    <span className={`pointer-events-none absolute left-1/2 z-20 hidden -translate-x-1/2 whitespace-nowrap rounded-[6px] bg-[#303645] px-[8px] py-[5px] text-[11px] font-medium leading-none text-white shadow-[0_6px_16px_rgba(21,26,38,0.18)] group-hover:block in-data-[theme=dark]:bg-[#f0f2f6] in-data-[theme=dark]:text-[#202126] ${row < 2 ? 'top-full mt-[7px]' : 'bottom-full mb-[7px]'}`}>
                      {day.label} · {day.count} 轮对话
                      <span className={`absolute left-1/2 size-0 -translate-x-1/2 border-x-4 border-x-transparent ${row < 2 ? 'bottom-full border-b-4 border-b-[#303645] in-data-[theme=dark]:border-b-[#f0f2f6]' : 'top-full border-t-4 border-t-[#303645] in-data-[theme=dark]:border-t-[#f0f2f6]'}`} />
                    </span>
                  )}
                </span>
              ))}
            </div>
          </div>
        ))}
        <div className="mt-[4px] flex items-center justify-center gap-[6px] text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
          <span>少</span>
          {[1, 2, 3, 4].map((level) => (
            <span
              key={`legend-${level}`}
              className={`size-[12px] shrink-0 rounded-[3px] border-[0.625px] border-solid border-[#e3e7f1] in-data-[theme=dark]:border-[#363a45] ${HEATMAP_CELL_LEVELS[level]}`}
            />
          ))}
          <span>多</span>
        </div>
      </div>
    </div>
  );
}

// Ascending rolling months ending at the current month, e.g. [去年7月 … 今年7月].
function heatmapMonthSequence() {
  const now = new Date();
  return Array.from({ length: HEATMAP_MONTH_SLOTS }, (_, index) => {
    const offsetFromNow = HEATMAP_MONTH_SLOTS - 1 - index;
    const date = new Date(now.getFullYear(), now.getMonth() - offsetFromNow, 1);
    return { year: date.getFullYear(), month: date.getMonth() };
  });
}

// Canonical partition of the columns into month slots, shared by the data grid
// and the month labels so they always line up.
function heatmapMonthColumnStart(slot: number) {
  return Math.floor((slot * HEATMAP_COLUMNS) / HEATMAP_MONTH_SLOTS);
}

function heatmapSlotForColumn(column: number) {
  for (let slot = HEATMAP_MONTH_SLOTS - 1; slot >= 0; slot -= 1) {
    if (column >= heatmapMonthColumnStart(slot)) return slot;
  }
  return 0;
}

function heatmapDays(byDay: Record<string, number>) {
  const months = heatmapMonthSequence();
  return Array.from({ length: HEATMAP_BUCKETS }, (_, index) => {
    const column = Math.floor(index / HEATMAP_ROWS);
    const row = index % HEATMAP_ROWS;
    const monthSlot = heatmapSlotForColumn(column);
    const { year, month } = months[monthSlot];
    const monthStartColumn = heatmapMonthColumnStart(monthSlot);
    const monthEndColumn = heatmapMonthColumnStart(monthSlot + 1);
    const columnsInMonth = Math.max(1, monthEndColumn - monthStartColumn);
    const cellsInMonth = columnsInMonth * HEATMAP_ROWS;
    const cellInMonth = (column - monthStartColumn) * HEATMAP_ROWS + row;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const startDay = Math.min(daysInMonth, Math.floor((cellInMonth * daysInMonth) / cellsInMonth) + 1);
    const endDay = Math.max(startDay, Math.min(daysInMonth, Math.floor(((cellInMonth + 1) * daysInMonth) / cellsInMonth)));
    const bucketStart = new Date(year, month, startDay);
    const bucketEnd = new Date(year, month, endDay);
    let count = 0;
    for (let dayOfMonth = startDay; dayOfMonth <= endDay; dayOfMonth += 1) {
      count += byDay[dateKey(new Date(year, month, dayOfMonth))] || 0;
    }
    const startKey = dateKey(bucketStart);
    const endKey = dateKey(bucketEnd);
    return {
      key: `${index}-${startKey}`,
      label: startKey === endKey ? startKey : `${startKey} 至 ${endKey}`,
      date: bucketStart,
      count,
    };
  });
}

function monthLabels() {
  const months = heatmapMonthSequence();
  return months.map((item, index) => {
    const offset = heatmapMonthColumnStart(index);
    const nextOffset = heatmapMonthColumnStart(index + 1);
    return {
      label: `${item.month + 1}月`,
      offset,
      span: Math.max(1, nextOffset - offset),
    };
  });
}

export function dateKey(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function growthTimeline(
  sops: SkillRead[],
  generalSkills: GeneralSkillRead[],
  tools: ToolRead[],
): GrowthEvent[] {
  const events: GrowthEvent[] = [];

  sops.forEach((item) => {
    const evolved = Boolean(item.branch_head_version && item.branch_head_version !== item.branch_base_version);
    events.push({
      id: `sop-${item.id}`,
      kind: evolved ? 'SOP 进化' : '新增 SOP',
      title: item.name,
      description: evolved
        ? `本地版本从 ${item.branch_base_version || item.version} 进化到 ${item.branch_head_version || item.version}`
        : `新增 ${item.version} 版业务流程`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="filter" />,
      tone: 'mint',
    });
  });

  generalSkills.forEach((item) => {
    const upgraded = isMeaningfullyUpdated(item.created_at, item.updated_at);
    events.push({
      id: `general-${item.id}`,
      kind: upgraded ? '技能升级' : '新增技能',
      title: item.name,
      description: upgraded ? '技能说明、权限或运行配置有更新' : `新增 ${item.slug} 通用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="spark" />,
      tone: 'teal',
    });
  });

  tools.forEach((item) => {
    events.push({
      id: `tool-${item.id}`,
      kind: '新增工具',
      title: item.display_name || item.name,
      description: `${item.bucket || '工具'} · ${item.tool_type.toUpperCase()} 调用能力`,
      timestamp: stableGrowthTimestamp(item),
      icon: <StaffdeckIcon name="tool" />,
      tone: 'green',
    });
  });

  return events
    .filter((item) => Boolean(item.timestamp))
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
}

function stableGrowthTimestamp(item: GrowthTimestampSource): string {
  const metadata = item.metadata || {};
  const candidates = [
    metadata.learned_at,
    metadata.assigned_at,
    metadata.installed_at,
    metadata.imported_at,
    metadata.created_at,
    item.created_at,
  ];
  return candidates.find((value): value is string => typeof value === 'string' && Boolean(value.trim())) || '';
}

function isMeaningfullyUpdated(createdAt?: string, updatedAt?: string): boolean {
  if (!createdAt || !updatedAt) return false;
  return Math.abs(new Date(updatedAt).getTime() - new Date(createdAt).getTime()) > 60 * 1000;
}

function formatMonthDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return `${date.getMonth() + 1}.${date.getDate()}`;
}
