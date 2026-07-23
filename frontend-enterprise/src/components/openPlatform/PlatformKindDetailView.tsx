import type { ComponentType, ReactNode, SVGProps } from 'react';
import { useMemo, useState } from 'react';

import AppHeader from '@/components/AppHeader';
import { StatCard } from '@/components/StatCard';
import { Button as UIButton } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const RETURN_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-5 text-[12px] font-normal text-[#757f9c] hover:border-[#cbd3e6] hover:bg-white hover:text-[#18181a]';

import IconArrowRight from '../../assets/icons/arrow-right.svg?react';
import IconRefresh from '../../assets/icons/refresh.svg?react';
import IconSearch from '../../assets/icons/search.svg?react';
import plazaKnowledgeIcon from '../../assets/icons/plaza-knowledge.svg';
import plazaSkillIcon from '../../assets/icons/plaza-skill.svg';
import plazaSopIcon from '../../assets/icons/plaza-sop.svg';
import plazaToolIcon from '../../assets/icons/plaza-tool.svg';
import EmployeeAvatar from '../EmployeeAvatar';
import type { AgentProfileRead } from '../../types';

import PlatformEmployeeCard, { type PlatformStat } from './PlatformEmployeeCard';
import PlatformResourceCard, { type PlatformResourceAccent } from './PlatformResourceCard';

export type PlatformDetailKind = 'agents' | 'knowledge' | 'general-skills' | 'skills' | 'tools';

export type PlatformDetailItem = {
  id: string;
  title: string;
  description: string;
  meta: string;
  tags: string[];
  agent?: AgentProfileRead;
};

const PLATFORM_RESOURCE_ICON: Partial<Record<PlatformDetailKind, string>> = {
  knowledge: plazaKnowledgeIcon,
  'general-skills': plazaSkillIcon,
  skills: plazaSopIcon,
  tools: plazaToolIcon,
};

const PLATFORM_ACCENT: Partial<Record<PlatformDetailKind, PlatformResourceAccent>> = {
  knowledge: 'green',
  'general-skills': 'indigo',
  skills: 'blue',
  tools: 'orange',
};

export type PlatformKindDetailViewProps = {
  kind: PlatformDetailKind;
  title: string;
  subtitle: string;
  countLabel: string;
  signals: string[];
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  items: PlatformDetailItem[];
  loading: boolean;
  employeeStats: (agent: AgentProfileRead) => PlatformStat[];
  onBack: () => void;
  onRefresh: () => void;
  onOpenItem: (item: PlatformDetailItem) => void;
  onLogout?: () => void;
  userName?: string;
};

function DetailSkeleton({ kind }: { kind: PlatformDetailKind }) {
  const cardHeight = kind === 'agents' ? 'h-[140px]' : 'h-[112px]';
  return (
    <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className={cn(
            'w-full animate-pulse rounded-[20px] border-[0.5px] border-[#f0f1f5] bg-[#f6f6f6]',
            cardHeight,
          )}
        />
      ))}
    </div>
  );
}

/**
 * Full-list view for a single 开放广场 module (/enterprise/platform/:kind).
 * Mirrors the main platform page card system inside the standard enterprise page shell.
 */
export default function PlatformKindDetailView({
  kind,
  title,
  subtitle,
  countLabel,
  signals,
  icon: PlatformIcon,
  items,
  loading,
  employeeStats,
  onBack,
  onRefresh,
  onOpenItem,
  onLogout,
  userName,
}: PlatformKindDetailViewProps) {
  const [searchText, setSearchText] = useState('');

  const filteredItems = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter((item) => [
      item.title,
      item.description,
      item.meta,
      item.tags.join(' '),
    ].some((value) => value.toLowerCase().includes(keyword)));
  }, [items, searchText]);

  return (
    <div className="min-h-full box-border px-[48px] pt-[32px] pb-[43px] max-[900px]:px-[16px]" aria-busy={loading}>
      <AppHeader
        onLogout={onLogout}
        userName={userName}
        title={title}
        description={subtitle}
      />

      <div className="mt-[20px] mb-[16px] flex flex-wrap justify-end gap-[16px]">
        <UIButton variant="outline" onClick={onBack} className={RETURN_BUTTON_CLASS}>
          <IconArrowRight className="size-3.5 rotate-180" />
          返回开放广场
        </UIButton>
        <UIButton
          variant="outline"
          onClick={onRefresh}
          disabled={loading}
          className={RETURN_BUTTON_CLASS}
        >
          <IconRefresh className={cn('size-[14px]', loading && 'animate-spin')} />
          刷新
        </UIButton>
      </div>

      <div className="flex flex-col gap-[24px] rounded-[20px] bg-white p-[18px_18px_24px_18px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
        <div className="flex flex-wrap items-stretch gap-[20px]" aria-label={`${title}统计`}>
          <StatCard value={items.length} label={countLabel} className="max-w-[220px]" />
        </div>

        <div className="flex flex-col gap-[18px]">
          <div className="flex items-center gap-[6px] px-[12px] text-[#757f9c]">
            <PlatformIcon className="size-[14px] shrink-0" />
            <span className="text-[14px] font-normal leading-none">{title}</span>
          </div>

          {signals.length > 0 && (
            <div className="flex flex-wrap items-center gap-[6px] px-[12px]">
              {signals.map((signal) => (
                <span
                  key={signal}
                  className="rounded-[20px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px] text-[10px] leading-[normal] text-[#757f9c]"
                >
                  {signal}
                </span>
              ))}
            </div>
          )}

          <label className="flex h-[34px] w-full max-w-[360px] items-center gap-[8px] overflow-hidden rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] transition-colors focus-within:border-[#18181a]">
            <IconSearch className="size-[14px] shrink-0 text-[#858b9c]" />
            <input
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-bwignore="true"
              value={searchText}
              placeholder={`搜索${countLabel}`}
              onChange={(event) => setSearchText(event.target.value)}
              className="min-w-0 flex-1 border-0 bg-transparent text-[12px] text-[#18181a] outline-none placeholder:text-[#858b9c]"
            />
          </label>

          {loading ? (
            <DetailSkeleton kind={kind} />
          ) : filteredItems.length === 0 ? (
            <div className="grid min-h-[180px] w-full place-items-center content-center gap-[10px] rounded-[18px] border border-dashed border-[#dfe4ec] bg-[#fbfcfd] px-[20px] py-[40px] text-center font-bold text-[#8b94aa]">
              <IconSearch className="size-[20px] shrink-0" />
              <span>{items.length === 0 ? '暂无开放内容' : '没有匹配的广场内容'}</span>
            </div>
          ) : kind === 'agents' ? (
            <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
              {filteredItems.map((item) => item.agent && (
                <PlatformEmployeeCard
                  key={item.id}
                  avatar={(
                    <EmployeeAvatar
                      agent={item.agent}
                      width={50}
                      height={59}
                      fit="contain"
                      objectPosition="center bottom"
                      className="overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
                    />
                  )}
                  name={item.title}
                  role={item.meta}
                  online={item.agent.status === 'active'}
                  description={item.description}
                  stats={employeeStats(item.agent)}
                  onOpen={() => onOpenItem(item)}
                />
              ))}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-[16px] sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
              {filteredItems.map((item) => (
                <PlatformResourceCard
                  key={item.id}
                  icon={PLATFORM_RESOURCE_ICON[kind]
                    ? <img src={PLATFORM_RESOURCE_ICON[kind]} alt="" className="size-[32px] shrink-0 object-contain" />
                    : undefined}
                  accent={PLATFORM_ACCENT[kind]}
                  title={item.title}
                  meta={item.meta}
                  description={item.description}
                  tags={item.tags.slice(0, 2)}
                  onClick={() => onOpenItem(item)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
