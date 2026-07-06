import CodeBlock from '@/components/CodeBlock';
import StaffdeckIcon, { type StaffdeckIconName } from '@/components/StaffdeckIcon';
import { cn } from '@/lib/utils';

import {
  CHAT_TRACE_CHEVRON_CLASS,
  CHAT_TRACE_CHEVRON_EXPANDED_CLASS,
  CHAT_TRACE_CODE_SUMMARY_CLASS,
  CHAT_TRACE_DETAILS_CLASS,
  CHAT_TRACE_ICON_CLASS,
  CHAT_TRACE_LINE_CLASS,
  CHAT_TRACE_LINE_CONTENT_CLASS,
  CHAT_TRACE_LINE_DETAIL_CLASS,
  CHAT_TRACE_LINE_TEXT_CLASS,
  CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_CLASS,
  CHAT_TRACE_SUMMARY_FAILED_CLASS,
  CHAT_TRACE_SUMMARY_RUNNING_CLASS,
  CHAT_TRACE_WRAP_CLASS,
} from '../chatPageStyles';
import { traceLineIconName, traceSummaryIconName } from '../chatHelpers';
import type { CotTraceIconName, TraceLine } from '../chatTypes';

const COT_ICON_MAP: Record<CotTraceIconName, StaffdeckIconName> = {
  advance: 'branch',
  execute: 'spark',
  generated: 'code',
  judge: 'filter',
  loading: 'refresh',
  select: 'check',
  tool: 'tool',
};

function CotTraceIcon({ name }: { name: CotTraceIconName }) {
  return (
    <span className={CHAT_TRACE_ICON_CLASS} aria-hidden="true">
      <StaffdeckIcon name={COT_ICON_MAP[name]} size={14} />
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
        <span>{summary.text}</span>
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
            <div key={line.id} className={CHAT_TRACE_LINE_CLASS}>
              <CotTraceIcon name={traceLineIconName(line)} />
              <span className={CHAT_TRACE_LINE_CONTENT_CLASS}>
                <span
                  className={cn(
                    CHAT_TRACE_LINE_TEXT_CLASS,
                    line.state === 'failed' && CHAT_TRACE_LINE_TEXT_FAILED_CLASS,
                  )}
                >
                  {line.text}
                </span>
                {line.detail && <span className={CHAT_TRACE_LINE_DETAIL_CLASS}>{line.detail}</span>}
                {line.code && (
                  <details open>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>查看代码</summary>
                    <CodeBlock className="mt-[6px]" code={line.code} language={line.language || 'python'} />
                  </details>
                )}
                {line.output && (
                  <details open>
                    <summary className={CHAT_TRACE_CODE_SUMMARY_CLASS}>{line.outputTitle || '查看输出'}</summary>
                    <CodeBlock className="mt-[6px]" code={line.output} language={line.outputLanguage || 'text'} />
                  </details>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
