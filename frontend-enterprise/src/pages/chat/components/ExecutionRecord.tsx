import { useEffect, useState, type ComponentType, type SVGProps } from 'react';

import CodeBlock from '@/components/CodeBlock';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import IconCotAdvance from '@/assets/staffdeck/cot-icons/advance.svg?react';
import IconCotExecute from '@/assets/staffdeck/cot-icons/execute.svg?react';
import IconCotGenerated from '@/assets/staffdeck/cot-icons/generated.svg?react';
import IconCotJudge from '@/assets/staffdeck/cot-icons/judge.svg?react';
import IconCotLoading from '@/assets/staffdeck/cot-icons/loading.svg?react';
import IconCotSelect from '@/assets/staffdeck/cot-icons/select.svg?react';
import IconCotTool from '@/assets/staffdeck/cot-icons/tool.svg?react';
import { cn } from '@/lib/utils';
import { useI18n } from '@/i18n';

import {
  CHAT_TRACE_CHEVRON_CLASS,
  CHAT_TRACE_CHEVRON_EXPANDED_CLASS,
  CHAT_TRACE_CODE_BLOCK_CLASS,
  CHAT_TRACE_CODE_DETAILS_CLASS,
  CHAT_TRACE_CODE_SUMMARY_CLASS,
  CHAT_TRACE_DETAILS_CLASS,
  CHAT_TRACE_FLOW_TEXT_CLASS,
  CHAT_TRACE_ICON_CLASS,
  CHAT_TRACE_LINE_CLASS,
  CHAT_TRACE_LINE_CONTENT_CLASS,
  CHAT_TRACE_LINE_DETAIL_CLASS,
  CHAT_TRACE_LINE_DURATION_CLASS,
  CHAT_TRACE_LINE_TEXT_CLASS,
  CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_CLASS,
  CHAT_TRACE_SUMMARY_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_RUNNING_CLASS,
  CHAT_TRACE_WRAP_CLASS,
} from '../chatPageStyles';
import { formatTraceDuration, traceLineIconName, traceSummaryIconName } from '../chatHelpers';
import type { CotTraceIconName, TraceLine } from '../chatTypes';
import MCPAppView from './MCPAppView';

const COT_ICON_MAP: Record<CotTraceIconName, ComponentType<SVGProps<SVGSVGElement>>> = {
  advance: IconCotAdvance,
  execute: IconCotExecute,
  generated: IconCotGenerated,
  judge: IconCotJudge,
  loading: IconCotLoading,
  select: IconCotSelect,
  tool: IconCotTool,
};

function CotTraceIcon({ name }: { name: CotTraceIconName }) {
  const Icon = COT_ICON_MAP[name];
  return (
    <span className={CHAT_TRACE_ICON_CLASS} aria-hidden="true">
      <Icon />
    </span>
  );
}

/** 步骤仍在执行时按固定间隔刷新“当前时刻”，用于实时耗时显示。 */
function useTraceNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function lineDurationMs(line: TraceLine, now: number): number | undefined {
  if (line.state === 'running') {
    return line.startedAt !== undefined ? Math.max(0, now - line.startedAt) : undefined;
  }
  if (line.durationMs !== undefined) return line.durationMs;
  if (line.startedAt !== undefined && line.completedAt !== undefined) {
    return Math.max(0, line.completedAt - line.startedAt);
  }
  return undefined;
}

function TraceDurationBadge({ line }: { line: TraceLine }) {
  const running = line.state === 'running' && line.startedAt !== undefined;
  const now = useTraceNow(running);
  const durationMs = lineDurationMs(line, now);
  const label = durationMs !== undefined ? formatTraceDuration(durationMs) : '';
  if (!label) return null;
  return (
    <span
      className={cn(CHAT_TRACE_LINE_DURATION_CLASS, running && CHAT_TRACE_FLOW_TEXT_CLASS)}
      data-i18n-ignore
    >
      {label}
    </span>
  );
}

type ExecutionRecordProps = {
  traceTurnId: string;
  summary: { text: string; state: TraceLine['state'] };
  details: TraceLine[];
  expanded: boolean;
  onToggle: (turnId: string, isExpanded: boolean) => void;
};

export default function ExecutionRecord({
  traceTurnId,
  summary,
  details,
  expanded,
  onToggle,
}: ExecutionRecordProps) {
  const { t } = useI18n();

  return (
    <div className={CHAT_TRACE_WRAP_CLASS}>
      <button
        type="button"
        className={cn(
          CHAT_TRACE_SUMMARY_CLASS,
          summary.state === 'running' && CHAT_TRACE_SUMMARY_RUNNING_CLASS,
          summary.state === 'failed' && CHAT_TRACE_SUMMARY_FAILED_CLASS,
        )}
        onClick={() => onToggle(traceTurnId, expanded)}
      >
        <CotTraceIcon name={traceSummaryIconName(summary)} />
        <span className={cn(summary.state === 'running' && CHAT_TRACE_FLOW_TEXT_CLASS)}>{t(summary.text)}</span>
        {details.length > 0 && (
          <StaffdeckIcon
            name="arrow"
            size={14}
            className={cn(CHAT_TRACE_CHEVRON_CLASS, expanded && CHAT_TRACE_CHEVRON_EXPANDED_CLASS)}
          />
        )}
      </button>
      {expanded && details.length > 0 && (
        <div className={CHAT_TRACE_DETAILS_CLASS}>
          {details.map((line) => (
            <div
              key={line.id}
              className={CHAT_TRACE_LINE_CLASS}
              style={line.depth ? { marginLeft: `${Math.min(line.depth, 3) * 20}px` } : undefined}
            >
              <CotTraceIcon name={traceLineIconName(line)} />
              <span className={CHAT_TRACE_LINE_CONTENT_CLASS}>
                <span
                  className={cn(
                    CHAT_TRACE_LINE_TEXT_CLASS,
                    line.state === 'running' && CHAT_TRACE_FLOW_TEXT_CLASS,
                    line.state === 'failed' && CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
                  )}
                >
                  {t(line.text)}
                  <TraceDurationBadge line={line} />
                </span>
                {line.detail && <span className={CHAT_TRACE_LINE_DETAIL_CLASS}>{t(line.detail)}</span>}
                {line.code && (
                  <details className={CHAT_TRACE_CODE_DETAILS_CLASS}>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>查看代码</summary>
                    <CodeBlock className={CHAT_TRACE_CODE_BLOCK_CLASS} code={line.code} language={line.language || 'python'} />
                  </details>
                )}
                {line.output && (
                  <details className={CHAT_TRACE_CODE_DETAILS_CLASS}>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>{t(line.outputTitle || '查看输出')}</summary>
                    <CodeBlock className={CHAT_TRACE_CODE_BLOCK_CLASS} code={line.output} language={line.outputLanguage || 'text'} />
                  </details>
                )}
                {line.mcpApp && <MCPAppView descriptor={line.mcpApp} />}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
