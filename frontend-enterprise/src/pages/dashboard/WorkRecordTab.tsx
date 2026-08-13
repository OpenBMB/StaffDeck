import { Fragment, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

import { HoverCard, HoverCardContent, HoverCardTrigger } from '../../components/ui/hover-card';
import { Popover, PopoverContent, PopoverTrigger } from '../../components/ui/popover';

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
  AgentWorkRecordEventRead,
  EnterpriseChatSessionRead,
  GeneralSkillRead,
  KnowledgeBaseRead,
  ScheduledTaskRead,
  SkillRead,
  ToolRead,
} from '../../types';

const TIMELINE_MODES = [
  { key: 'day', label: 'Day' },
  { key: 'week', label: 'Week' },
  { key: 'month', label: 'Month' },
] as const;
type TimelineMode = (typeof TIMELINE_MODES)[number]['key'];
const TIMELINE_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

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
  conversationCount: number;
  activityEvents: AgentWorkRecordEventRead[];
  feedbackCount: number;
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
  conversationCount,
  activityEvents,
  feedbackCount,
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
      count: conversationCount,
      body: staffdeckDisplayText(employeeSessions[0]?.summary || employeeSessions[0]?.last_agent_question || '暂无对话任务'),
      icon: <IconProfileCalendar className={capabilityGlyphClass} />,
      dark: true,
      illustration: capabilityLogs,
    },
  ];

  const growthItems = growthTimeline(activeSkills, activeGeneralSkills, activeTools);

  return (
    <section className="relative flex w-full min-w-0 max-w-full mt-[-2px] flex-col gap-[24px] overflow-hidden rounded-[18px] shadow-[0_20px_42px_rgba(21,26,38,0.045)] bg-white p-[14px] *:min-w-0 min-[521px]:p-[18px] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:bg-[#202126] in-data-[theme=dark]:text-[#f0f2f6]">
      <div className="flex w-full items-stretch gap-[16px]">
        <ClickableMetric label="对话次数" value={conversationCount} onClick={goToLogs} />
        <ClickableMetric label="反馈次数" value={feedbackCount} onClick={goToLogs} />
        <ClickableMetric label="好评率" value={positiveRate} suffix="%" tone="positive" onClick={goToLogs} />
        <ClickableMetric label="差评率" value={negativeRate} suffix="%" tone="negative" onClick={goToLogs} />
      </div>
      <ActivityTimeline events={activityEvents} />
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

type MetricTone = 'default' | 'positive' | 'negative';

const metricToneClass: Record<MetricTone, string> = {
  default:
    'border-[0.5px] border-[#e3e7f1] bg-transparent hover:bg-[#f7f8fa] in-data-[theme=dark]:border-[#343741] in-data-[theme=dark]:hover:bg-white/5',
  positive: 'bg-[#e9f7ef] hover:bg-[#dcf1e5] in-data-[theme=dark]:bg-[#173a29] in-data-[theme=dark]:hover:bg-[#1c452f]',
  negative: 'bg-[#fce7e7] hover:bg-[#f9dada] in-data-[theme=dark]:bg-[#3d1f1f] in-data-[theme=dark]:hover:bg-[#4a2626]',
};

const metricValueToneClass: Record<MetricTone, string> = {
  default: 'text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]',
  positive: 'text-[#2cb360] in-data-[theme=dark]:text-[#4fd189]',
  negative: 'text-[#d20b0b] in-data-[theme=dark]:text-[#f26565]',
};

function ClickableMetric({
  label,
  value,
  suffix = '',
  tone = 'default',
  onClick,
}: {
  label: string;
  value: number;
  suffix?: string;
  tone?: MetricTone;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex min-w-px flex-[1_0_0] cursor-pointer flex-col justify-center gap-[4px] rounded-[20px] px-[32px] py-[16px] text-left transition-colors ${metricToneClass[tone]}`}
    >
      <strong className={`text-[18px] font-medium leading-none ${metricValueToneClass[tone]}`}>{value}{suffix}</strong>
      <span className="text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">{label}</span>
    </button>
  );
}

// Per-activity accent colors, shared by the Day/Week timeline and the Month calendar.
const ACTIVITY_DOT: Record<string, string> = {
  chat: 'bg-[#4f92ff]',
  task: 'bg-[#ff9138]',
  sop: 'bg-[#2cb360]',
  tool: 'bg-[#9b6dff]',
  knowledge: 'bg-[#12b5c9]',
  skill: 'bg-[#f2589f]',
};

type TrackEvent = { time: number; name: string };

type TimelineTrackConfig = {
  key: string;
  label: string;
  unit: string;
  dot: string;
  bar: string;
};

const TIMELINE_TRACKS: TimelineTrackConfig[] = [
  {
    key: 'chat',
    label: '对话',
    unit: '次对话',
    dot: ACTIVITY_DOT.chat,
    bar: 'bg-[#e8f0ff] in-data-[theme=dark]:bg-[#1d2c47]',
  },
  {
    key: 'task',
    label: '定时任务',
    unit: '个任务',
    dot: ACTIVITY_DOT.task,
    bar: 'bg-[#fff1e3] in-data-[theme=dark]:bg-[#3a2c1a]',
  },
  {
    key: 'sop',
    label: '新增SOP',
    unit: '个 SOP',
    dot: ACTIVITY_DOT.sop,
    bar: 'bg-[#e9f7ef] in-data-[theme=dark]:bg-[#173a29]',
  },
  {
    key: 'tool',
    label: '新增工具',
    unit: '个工具',
    dot: ACTIVITY_DOT.tool,
    bar: 'bg-[#f1ecff] in-data-[theme=dark]:bg-[#2c2544]',
  },
  {
    key: 'knowledge',
    label: '新增知识',
    unit: '个知识',
    dot: ACTIVITY_DOT.knowledge,
    bar: 'bg-[#e2f6f9] in-data-[theme=dark]:bg-[#123037]',
  },
  {
    key: 'skill',
    label: '新增技能',
    unit: '个技能',
    dot: ACTIVITY_DOT.skill,
    bar: 'bg-[#fde8f1] in-data-[theme=dark]:bg-[#3d1e2e]',
  },
];

type DayActivity = { label: string; dot: string; time?: string };

type ActivityTimelineProps = {
  events: AgentWorkRecordEventRead[];
};

function ActivityTimeline({ events }: ActivityTimelineProps) {
  const [mode, setMode] = useState<TimelineMode>('day');
  const [anchor, setAnchor] = useState<number>(() => startOfDay(new Date()).getTime());

  const eventsByTrack = useMemo(() => {
    const collect = (entries: Array<{ value?: string; name?: string }>) =>
      entries
        .map((entry) => ({ time: entry.value ? new Date(entry.value).getTime() : Number.NaN, name: entry.name || '' }))
        .filter((entry) => Number.isFinite(entry.time));
    return TIMELINE_TRACKS.reduce<Record<string, TrackEvent[]>>((grouped, track) => {
      grouped[track.key] = collect(
        events
          .filter((item) => item.kind === track.key)
          .map((item) => ({ value: item.timestamp, name: staffdeckDisplayText(item.label) })),
      );
      return grouped;
    }, {});
  }, [events]);

  const itemsByDay = useMemo(
    () => (mode === 'month' || mode === 'week' ? buildDayActivities(events) : {}),
    [events, mode],
  );

  const range = useMemo(() => timelineRange(mode, anchor), [mode, anchor]);
  const ticks = useMemo(() => timelineTicks(mode, range), [mode, range]);
  const activeTracks = useMemo(
    () =>
      TIMELINE_TRACKS.map((track) => ({
        track,
        segments: daySegments(eventsByTrack[track.key] || [], range),
      })).filter((item) => item.segments.length > 0),
    [eventsByTrack, range],
  );

  const shift = (direction: number) => setAnchor((prev) => shiftAnchor(mode, prev, direction));
  const changeMode = (next: TimelineMode) => {
    setMode(next);
    setAnchor(normalizeAnchor(next, anchor));
  };

  return (
    <div className="flex w-full min-w-0 flex-col gap-[16px]">
      <div className="flex h-[36px] flex-wrap items-center justify-between gap-[12px]">
        <div className="flex items-center gap-[6px] text-[14px] text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
          <IconProfileCalendar className="size-[14px] shrink-0" />
          {formatAnchorLabel(mode, range)}
        </div>
        <div className="flex items-center gap-[24px] rounded-[8px] border border-[#e3e7f1] px-[12px] py-[8px] in-data-[theme=dark]:border-[#343741]">
          <button
            type="button"
            onClick={() => shift(-1)}
            className="flex size-[14px] items-center justify-center text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
            aria-label="上一个周期"
          >
            <TimelineChevron direction="left" />
          </button>
          <TimelineDatePicker
            mode={mode}
            anchor={anchor}
            label={formatTimelineRange(mode, range)}
            onPick={setAnchor}
          />
          <button
            type="button"
            onClick={() => shift(1)}
            className="flex size-[14px] items-center justify-center text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
            aria-label="下一个周期"
          >
            <TimelineChevron direction="right" />
          </button>
        </div>
        <div className="flex items-center gap-[12px]">
          {TIMELINE_MODES.map((item) => (
            <button
              type="button"
              key={item.key}
              onClick={() => changeMode(item.key)}
              className={`flex w-[50px] items-center justify-center px-[8px] text-[12px] transition-colors ${
                mode === item.key
                  ? 'font-medium text-[#464c5e] in-data-[theme=dark]:text-[#f0f2f6]'
                  : 'text-[#757f9c] hover:text-[#464c5e] in-data-[theme=dark]:text-[#8b93a6]'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      {mode === 'month' ? (
        <MonthCalendar anchor={anchor} itemsByDay={itemsByDay} />
      ) : mode === 'week' ? (
        <WeekCalendar anchor={anchor} itemsByDay={itemsByDay} />
      ) : activeTracks.length === 0 ? (
        <TimelineEmptyState text="当日暂无活动记录" />
      ) : (
        <div className="flex min-h-[178px] w-full flex-col gap-[16px]">
          <div className="relative flex flex-1 w-full flex-col justify-center overflow-hidden rounded-[20px] px-[12px] py-[16px]">
            <div className="pointer-events-none absolute inset-x-[12px] inset-y-[8px] flex justify-between">
              {ticks.map((_, index) => (
                <span key={`grid-${index}`} className="w-px bg-[#eef1f7] in-data-[theme=dark]:bg-[#2c2f38]" />
              ))}
            </div>
            <div className="relative z-10 flex min-h-[94px] flex-col gap-[8px]">
              {activeTracks.map(({ track, segments }) => (
                <div key={track.key} className="relative h-[26px] w-full">
                  {segments.map((segment, segmentIndex) => {
                    const label = trackBarLabel(track, segment);
                    return (
                      <HoverCard key={segmentIndex} openDelay={120} closeDelay={80}>
                        <HoverCardTrigger asChild>
                          <div
                            className={`absolute top-0 flex h-[26px] min-w-0 cursor-default items-center gap-[6px] overflow-hidden rounded-[8px] px-[8px] py-[4px] ${track.bar}`}
                            style={{ left: `${segment.left}%`, width: `${segment.width}%` }}
                          >
                            <span className={`size-[6px] shrink-0 rounded-full ${track.dot}`} />
                            <span className="truncate text-[10px] leading-none capitalize text-[#464c5e] in-data-[theme=dark]:text-[#f0f2f6]">
                              {label}
                            </span>
                          </div>
                        </HoverCardTrigger>
                        <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                          <div className="mb-[8px] flex items-center gap-[6px]">
                            <span className={`size-[6px] shrink-0 rounded-full ${track.dot}`} />
                            <span className="text-[12px] font-medium text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
                              {track.label}
                            </span>
                            <span className="text-[11px] text-[#858b9c]">
                              共{segment.count}
                              {track.unit}
                            </span>
                          </div>
                          <div className="flex flex-col gap-[4px]">
                            {segment.events.slice(0, 12).map((event, eventIndex) => (
                              <div
                                key={`${track.key}-${event.time}-${eventIndex}`}
                                className="flex items-start gap-[8px] text-[11px] leading-[15px]"
                              >
                                <span className="shrink-0 tabular-nums text-[#858b9c]">
                                  {formatHm(new Date(event.time))}
                                </span>
                                <span className="flex-1 break-words text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                                  {event.name || track.label}
                                </span>
                              </div>
                            ))}
                            {segment.events.length > 12 && (
                              <div className="text-[11px] text-[#858b9c]">…等{segment.events.length}项</div>
                            )}
                          </div>
                        </HoverCardContent>
                      </HoverCard>
                    );
                  })}
                </div>
              ))}
            </div>
          </div>

          <div className="flex h-[16px] w-full items-center justify-between px-[12px] text-[12px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
            {ticks.map((tick, index) => (
              <span key={`tick-${index}`} className="relative w-0">
                <span className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 whitespace-nowrap">
                  {tick}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TimelineEmptyState({ text }: { text: string }) {
  return (
    <div className="flex min-h-[178px] w-full flex-col items-center justify-center gap-[10px] rounded-[20px] border border-dashed border-[#e3e7f1] in-data-[theme=dark]:border-[#343741]">
      <IconProfileCalendar className="size-[24px] text-[#c0c5d2] in-data-[theme=dark]:text-[#5b606d]" />
      <span className="text-[13px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
        {text}
      </span>
    </div>
  );
}

function WeekCalendar({
  anchor,
  itemsByDay,
}: {
  anchor: number;
  itemsByDay: Record<string, DayActivity[]>;
}) {
  const weekStart = startOfWeek(new Date(anchor));
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(weekStart);
    date.setDate(date.getDate() + index);
    return date;
  });
  const todayKey = dateKey(new Date());
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const hasActivity = days.some((date) => (itemsByDay[dateKey(date)] || []).length > 0);
  if (!hasActivity) {
    return <TimelineEmptyState text="本周暂无活动记录" />;
  }

  return (
    <div className="flex min-h-[178px] w-full min-w-0 items-stretch rounded-[20px]">
      {days.map((date, index) => {
        const key = dateKey(date);
        const isToday = key === todayKey;
        const items = itemsByDay[key] || [];
        const isExpanded = Boolean(expanded[key]);
        const visible = isExpanded || items.length <= 4 ? items : items.slice(0, 3);
        const overflow = items.length - visible.length;

        return (
          <Fragment key={key}>
            {index > 0 && (
              <span className="my-[12px] w-px shrink-0 self-stretch bg-[#eef1f7] in-data-[theme=dark]:bg-[#2c2f38]" />
            )}
            <div
              className={`flex min-w-px flex-1 flex-col gap-[12px] rounded-[18px] px-[12px] py-[10px] ${
                isToday ? 'bg-[#e8f0ff] in-data-[theme=dark]:bg-[#1d2c47]' : ''
              }`}
            >
              <span className="text-[14px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
                {date.getDate()}
              </span>
              <div className="flex w-full flex-col gap-[2px]">
                {visible.map((item, itemIndex) => (
                  <HoverCard key={`${key}-${itemIndex}`} openDelay={120} closeDelay={80}>
                    <HoverCardTrigger asChild>
                      <div className="flex cursor-default items-center gap-[6px] rounded-[8px] p-[4px] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]">
                        <span className={`size-[6px] shrink-0 rounded-full ${item.dot}`} />
                        <span className="truncate text-[10px] leading-none capitalize text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                          {item.label}
                        </span>
                      </div>
                    </HoverCardTrigger>
                    <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                      <div className="flex items-start gap-[8px]">
                        <span className={`mt-[4px] size-[6px] shrink-0 rounded-full ${item.dot}`} />
                        <span className="flex-1 break-words text-[12px] leading-[17px] text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                          {item.time ? (
                            <span className="mr-[6px] tabular-nums text-[#858b9c]">{item.time}</span>
                          ) : null}
                          {item.label}
                        </span>
                      </div>
                    </HoverCardContent>
                  </HoverCard>
                ))}
                {overflow > 0 && (
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                  >
                    <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                      还有{overflow}项
                    </span>
                  </button>
                )}
                {isExpanded && items.length > 4 && (
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                  >
                    <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                      收起
                    </span>
                  </button>
                )}
              </div>
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}

function MonthCalendar({
  anchor,
  itemsByDay,
}: {
  anchor: number;
  itemsByDay: Record<string, DayActivity[]>;
}) {
  const weeks = monthCalendarWeeks(anchor);
  const month = new Date(anchor).getMonth();
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  return (
    <div className="w-full overflow-hidden rounded-[20px] border border-[#eef1f7] in-data-[theme=dark]:border-[#2c2f38]">
      <div className="grid grid-cols-7">
        {['周日', '周一', '周二', '周三', '周四', '周五', '周六'].map((day) => (
          <div
            key={day}
            className="px-[12px] py-[8px] text-[12px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]"
          >
            {day}
          </div>
        ))}
      </div>
      {weeks.map((week) => (
        <div
          key={dateKey(week[0])}
          className="grid grid-cols-7 border-t border-[#eef1f7] in-data-[theme=dark]:border-[#2c2f38]"
        >
          {week.map((date) => {
            const key = dateKey(date);
            const items = itemsByDay[key] || [];
            const isExpanded = Boolean(expanded[key]);
            const visible = isExpanded || items.length <= 4 ? items : items.slice(0, 3);
            const overflow = items.length - visible.length;
            const inMonth = date.getMonth() === month;
            const dayLabel = date.getDate() === 1 ? `${date.getMonth() + 1}月1日` : `${date.getDate()}`;
            return (
              <div
                key={key}
                className={`flex min-h-[136px] flex-col gap-[8px] px-[12px] py-[10px] ${inMonth ? '' : 'opacity-45'}`}
              >
                <span className="text-[14px] leading-none text-[#858b9c] in-data-[theme=dark]:text-[#8b93a6]">
                  {dayLabel}
                </span>
                <div className="flex flex-col gap-[2px]">
                  {visible.map((item, index) => (
                    <HoverCard key={`${key}-${index}`} openDelay={120} closeDelay={80}>
                      <HoverCardTrigger asChild>
                        <div className="flex cursor-default items-center gap-[6px] rounded-[8px] p-[4px] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]">
                          <span className={`size-[6px] shrink-0 rounded-full ${item.dot}`} />
                          <span className="truncate text-[10px] leading-none capitalize text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                            {item.label}
                          </span>
                        </div>
                      </HoverCardTrigger>
                      <HoverCardContent align="start" sideOffset={6} className="w-auto max-w-[300px] p-[10px]">
                        <div className="flex items-start gap-[8px]">
                          <span className={`mt-[4px] size-[6px] shrink-0 rounded-full ${item.dot}`} />
                          <span className="flex-1 break-words text-[12px] leading-[17px] text-[#464c5e] in-data-[theme=dark]:text-[#c9cede]">
                            {item.time ? (
                              <span className="mr-[6px] tabular-nums text-[#858b9c]">{item.time}</span>
                            ) : null}
                            {item.label}
                          </span>
                        </div>
                      </HoverCardContent>
                    </HoverCard>
                  ))}
                  {overflow > 0 && (
                    <button
                      type="button"
                      onClick={() => toggle(key)}
                      className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                    >
                      <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                        还有{overflow}项
                      </span>
                    </button>
                  )}
                  {isExpanded && items.length > 4 && (
                    <button
                      type="button"
                      onClick={() => toggle(key)}
                      className="flex items-center gap-[6px] rounded-[8px] p-[4px] text-left transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:hover:bg-[#2b2d33]"
                    >
                      <span className="truncate text-[10px] leading-none text-[#757f9c] in-data-[theme=dark]:text-[#8b93a6]">
                        收起
                      </span>
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

function TimelineChevron({ direction }: { direction: 'left' | 'right' }) {
  return (
    <svg viewBox="0 0 14 14" fill="none" className="size-[14px]" aria-hidden>
      <path
        d={direction === 'left' ? 'M9 3L5 7l4 4' : 'M5 3l4 4-4 4'}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function startOfWeek(date: Date): Date {
  const start = startOfDay(date);
  start.setDate(start.getDate() - start.getDay());
  return start;
}

function timelineRange(mode: TimelineMode, anchor: number): { start: number; end: number } {
  const base = new Date(anchor);
  if (mode === 'day') {
    const start = startOfDay(base);
    return { start: start.getTime(), end: start.getTime() + 24 * 60 * 60 * 1000 };
  }
  if (mode === 'week') {
    const start = startOfWeek(base);
    return { start: start.getTime(), end: start.getTime() + 7 * 24 * 60 * 60 * 1000 };
  }
  const start = new Date(base.getFullYear(), base.getMonth(), 1);
  const end = new Date(base.getFullYear(), base.getMonth() + 1, 1);
  return { start: start.getTime(), end: end.getTime() };
}

function normalizeAnchor(mode: TimelineMode, anchor: number): number {
  return timelineRange(mode, anchor).start;
}

function shiftAnchor(mode: TimelineMode, anchor: number, direction: number): number {
  const base = new Date(anchor);
  if (mode === 'day') base.setDate(base.getDate() + direction);
  else if (mode === 'week') base.setDate(base.getDate() + direction * 7);
  else base.setMonth(base.getMonth() + direction);
  return normalizeAnchor(mode, base.getTime());
}

function trackBarLabel(
  track: TimelineTrackConfig,
  bar: { count: number; names: string[] },
): string {
  if (track.key === 'chat') return `对话${bar.count}条`;
  if (!bar.names.length) return track.label;
  const suffix = bar.names.length > 1 ? ` 等${bar.names.length}项` : '';
  return `${track.label} ${bar.names[0]}${suffix}`;
}

type DaySegment = { left: number; width: number; count: number; names: string[]; events: TrackEvent[] };

// Day view: each event fills the 2-hour cell that contains it — start snaps down
// to the 2-hour tick line at or before it, end snaps up to the next 2-hour tick
// line (so 6:26 → 6-8, 9:40 → 8-10). Consecutive/touching cells of the same
// track merge into one bar (e.g. a chat at 9:xx + an event at 11:xx → 8-12).
const DAY_HOURS = 24;
const HOUR_MS = 60 * 60 * 1000;

function daySegments(events: TrackEvent[], range: { start: number; end: number }): DaySegment[] {
  const inRange = events
    .filter((event) => event.time >= range.start && event.time < range.end)
    .sort((a, b) => a.time - b.time);
  if (!inRange.length) return [];

  const merged: Array<{ start: number; end: number; events: TrackEvent[] }> = [];
  for (const event of inRange) {
    const hour = (event.time - range.start) / HOUR_MS;
    const start = Math.max(0, Math.floor(hour / 2) * 2);
    const end = Math.min(DAY_HOURS, start + 2);
    const last = merged[merged.length - 1];
    if (last && start <= last.end) {
      last.end = Math.max(last.end, end);
      last.events.push(event);
    } else {
      merged.push({ start, end, events: [event] });
    }
  }

  return merged.map((block) => ({
    left: (block.start / DAY_HOURS) * 100,
    width: ((block.end - block.start) / DAY_HOURS) * 100,
    count: block.events.length,
    names: Array.from(new Set(block.events.map((event) => event.name).filter(Boolean))),
    events: block.events,
  }));
}

function timelineTicks(mode: TimelineMode, range: { start: number; end: number }): string[] {
  if (mode === 'day') {
    return Array.from({ length: 13 }, (_, index) => formatHour(index * 2));
  }
  if (mode === 'week') {
    return Array.from({ length: 7 }, (_, index) => {
      const date = new Date(range.start + index * 24 * 60 * 60 * 1000);
      return `${TIMELINE_WEEKDAYS[date.getDay()]} ${date.getDate()}`;
    });
  }
  const start = new Date(range.start);
  const daysInMonth = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
  const step = Math.max(2, Math.round(daysInMonth / 8));
  const ticks: string[] = [];
  for (let day = 1; day <= daysInMonth; day += step) {
    ticks.push(`${start.getMonth() + 1}/${day}`);
  }
  return ticks;
}

function formatHour(hour: number): string {
  if (hour === 0 || hour === 24) return '12AM';
  if (hour === 12) return '12PM';
  return hour < 12 ? `${hour}AM` : `${hour - 12}PM`;
}

function formatTimelineDate(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${date.getFullYear()}/${month}/${day}`;
}

function formatTimelineRange(mode: TimelineMode, range: { start: number; end: number }): string {
  const start = new Date(range.start);
  if (mode === 'day') return formatTimelineDate(start);
  const last = new Date(range.end - 24 * 60 * 60 * 1000);
  const short = (date: Date) => `${date.getMonth() + 1}/${date.getDate()}`;
  if (mode === 'week') return `${short(start)} - ${short(last)}`;
  return `${start.getFullYear()}/${`${start.getMonth() + 1}`.padStart(2, '0')}`;
}

function TimelineDatePicker({
  mode,
  anchor,
  label,
  onPick,
}: {
  mode: TimelineMode;
  anchor: number;
  label: string;
  onPick: (ms: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const [viewDate, setViewDate] = useState(() => new Date(anchor));

  const handleOpenChange = (next: boolean) => {
    if (next) setViewDate(new Date(anchor));
    setOpen(next);
  };
  const commit = (date: Date) => {
    onPick(normalizeAnchor(mode, date.getTime()));
    setOpen(false);
  };

  const selected = new Date(anchor);
  const selectedWeekStart = startOfWeek(selected).getTime();
  const shiftView = (deltaMonth: number, deltaYear: number) =>
    setViewDate((prev) => new Date(prev.getFullYear() + deltaYear, prev.getMonth() + deltaMonth, 1));

  const now = new Date();
  const shortcuts: { label: string; date: Date }[] =
    mode === 'day'
      ? [
          { label: '今天', date: now },
          { label: '昨天', date: new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1) },
        ]
      : mode === 'week'
        ? [
            { label: '本周', date: now },
            { label: '上周', date: new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7) },
          ]
        : [
            { label: '本月', date: now },
            { label: '上月', date: new Date(now.getFullYear(), now.getMonth() - 1, 1) },
          ];

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="text-[12px] whitespace-nowrap text-[#464c5e] transition-colors hover:text-[#18181a] in-data-[theme=dark]:text-[#c9cede]"
        >
          {label}
        </button>
      </PopoverTrigger>
      <PopoverContent align="center" className="w-auto p-[12px]">
        <div className="mb-[8px] flex items-center justify-between">
          <button
            type="button"
            onClick={() => shiftView(mode === 'month' ? 0 : -1, mode === 'month' ? -1 : 0)}
            className="flex size-[24px] items-center justify-center rounded-[6px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            aria-label="上一页"
          >
            <TimelineChevron direction="left" />
          </button>
          <span className="text-[13px] font-medium text-[#18181a] in-data-[theme=dark]:text-[#f0f2f6]">
            {mode === 'month'
              ? `${viewDate.getFullYear()}年`
              : `${viewDate.getFullYear()}年${viewDate.getMonth() + 1}月`}
          </span>
          <button
            type="button"
            onClick={() => shiftView(mode === 'month' ? 0 : 1, mode === 'month' ? 1 : 0)}
            className="flex size-[24px] items-center justify-center rounded-[6px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            aria-label="下一页"
          >
            <TimelineChevron direction="right" />
          </button>
        </div>

        {mode === 'month' ? (
          <div className="grid grid-cols-3 gap-[6px]">
            {Array.from({ length: 12 }, (_, index) => {
              const isSelected = selected.getFullYear() === viewDate.getFullYear() && selected.getMonth() === index;
              return (
                <button
                  key={index}
                  type="button"
                  onClick={() => commit(new Date(viewDate.getFullYear(), index, 1))}
                  className={`flex h-[36px] w-[64px] items-center justify-center rounded-[8px] text-[12px] transition-colors ${
                    isSelected
                      ? 'bg-[#4f92ff] text-white'
                      : 'text-[#464c5e] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]'
                  }`}
                >
                  {index + 1}月
                </button>
              );
            })}
          </div>
        ) : (
          <>
            <div className="grid grid-cols-7">
              {['日', '一', '二', '三', '四', '五', '六'].map((day) => (
                <span
                  key={day}
                  className="flex size-[32px] items-center justify-center text-[11px] text-[#a7adbd] in-data-[theme=dark]:text-[#6b7080]"
                >
                  {day}
                </span>
              ))}
            </div>
            {monthCalendarWeeks(viewDate.getTime()).map((week) => (
              <div key={dateKey(week[0])} className="grid grid-cols-7">
                {week.map((date, dayIndex) => {
                  const inMonth = date.getMonth() === viewDate.getMonth();
                  const isSelected = mode === 'day' && isSameDay(date, selected);
                  const inWeek = mode === 'week' && startOfWeek(date).getTime() === selectedWeekStart;
                  const bandRounding =
                    dayIndex === 0 ? 'rounded-l-[8px]' : dayIndex === 6 ? 'rounded-r-[8px]' : '';
                  const tone = isSelected
                    ? 'rounded-[8px] bg-[#4f92ff] font-medium text-white'
                    : inWeek
                      ? `${bandRounding} bg-[#e8f0ff] text-[#18181a] in-data-[theme=dark]:bg-[#1d2c47] in-data-[theme=dark]:text-[#f0f2f6]`
                      : inMonth
                        ? 'rounded-[8px] text-[#464c5e] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]'
                        : 'rounded-[8px] text-[#c0c5d2] hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#5b606d]';
                  return (
                    <button
                      key={dateKey(date)}
                      type="button"
                      onClick={() => commit(date)}
                      className={`flex size-[32px] items-center justify-center text-[12px] transition-colors ${tone}`}
                    >
                      {date.getDate()}
                    </button>
                  );
                })}
              </div>
            ))}
          </>
        )}

        <div className="mt-[8px] flex items-center gap-[6px] border-t border-[#eef1f7] pt-[8px] in-data-[theme=dark]:border-[#2c2f38]">
          {shortcuts.map((shortcut) => (
            <button
              key={shortcut.label}
              type="button"
              onClick={() => commit(shortcut.date)}
              className="rounded-[6px] px-[10px] py-[4px] text-[12px] text-[#464c5e] transition-colors hover:bg-[#f6f6f6] in-data-[theme=dark]:text-[#c9cede] in-data-[theme=dark]:hover:bg-[#2b2d33]"
            >
              {shortcut.label}
            </button>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}

function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function formatAnchorLabel(mode: TimelineMode, range: { start: number; end: number }): string {
  const start = new Date(range.start);
  if (mode === 'month') return `${start.getFullYear()}/${`${start.getMonth() + 1}`.padStart(2, '0')}`;
  return formatTimelineDate(start);
}

function formatHm(date: Date): string {
  return `${`${date.getHours()}`.padStart(2, '0')}:${`${date.getMinutes()}`.padStart(2, '0')}`;
}

// Sun→Sat weeks covering the full month of the anchor (leading/trailing days included).
function monthCalendarWeeks(anchor: number): Date[][] {
  const base = new Date(anchor);
  const monthStart = new Date(base.getFullYear(), base.getMonth(), 1);
  const monthEnd = new Date(base.getFullYear(), base.getMonth() + 1, 0);
  const gridStart = startOfWeek(monthStart);
  const weeks: Date[][] = [];
  const cursor = new Date(gridStart);
  while (cursor <= monthEnd || cursor.getDay() !== 0) {
    const week: Date[] = [];
    for (let day = 0; day < 7; day += 1) {
      week.push(new Date(cursor));
      cursor.setDate(cursor.getDate() + 1);
    }
    weeks.push(week);
    if (weeks.length > 6) break;
  }
  return weeks;
}

function buildDayActivities(events: AgentWorkRecordEventRead[]): Record<string, DayActivity[]> {
  const map: Record<string, DayActivity[]> = {};
  const push = (event: AgentWorkRecordEventRead, label: string) => {
    const date = new Date(event.timestamp);
    if (Number.isNaN(date.getTime())) return;
    const key = dateKey(date);
    (map[key] ||= []).push({ label, dot: ACTIVITY_DOT[event.kind], time: formatHm(date) });
  };

  const chatByDay: Record<string, number> = {};
  events.filter((item) => item.kind === 'chat').forEach((item) => {
    const date = new Date(item.timestamp);
    if (Number.isNaN(date.getTime())) return;
    const key = dateKey(date);
    chatByDay[key] = (chatByDay[key] || 0) + 1;
  });
  Object.entries(chatByDay).forEach(([dayKey, count]) => {
    (map[dayKey] ||= []).unshift({ label: `对话${count}条`, dot: ACTIVITY_DOT.chat });
  });

  events.filter((item) => item.kind !== 'chat').forEach((item) => {
    const prefix = item.kind === 'sop'
      ? '新增SOP '
      : item.kind === 'tool'
        ? '新增工具 '
        : item.kind === 'knowledge'
          ? '新增知识 '
          : item.kind === 'skill'
            ? '新增技能 '
            : '';
    push(item, `${prefix}${staffdeckDisplayText(item.label)}`);
  });

  return map;
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
