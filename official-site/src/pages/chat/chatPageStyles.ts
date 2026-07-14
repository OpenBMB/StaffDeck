import { cn } from '@/lib/utils';

/**
 * Tailwind class tokens for the migrated chat conversation page.
 *
 * Palette + metrics are derived from the SD1 Figma (nodes 38:3773 empty state
 * and 38:4962 active conversation): background #fcfcfc, 220px sidebar with a
 * #f4f4f4 right border, ink #18181a, muted #858b9c / #757f9c, hairline borders
 * #e3e7f1 (0.5px), user bubble #f6f6f6, 56px header, and a white composer card
 * with a #18181a 0.5px border, 14px radius and a dark send button.
 */

// ---------------------------------------------------------------------------
// Shared chat chrome (the sidebar itself now lives in the reusable AppSidebar
// component; see AppSidebar.tsx `variant="chat"`).
// ---------------------------------------------------------------------------
export const CHAT_ICON_BUTTON_CLASS =
  'inline-grid size-[30px] shrink-0 place-items-center rounded-[8px] border-0 bg-transparent p-0 text-[#757f9c] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]';

// ---------------------------------------------------------------------------
// Main column + header
// ---------------------------------------------------------------------------
export const CHAT_MAIN_CLASS = 'flex min-h-0 min-w-0 flex-col bg-[#fcfcfc]';
export const CHAT_HEADER_CLASS =
  'flex h-[56px] shrink-0 items-center justify-between gap-[12px] border-b border-[#f4f4f4] pl-[18px] pr-[24px]';
export const CHAT_HEADER_TITLE_STACK_CLASS = 'flex min-w-0 items-end gap-[8px]';
export const CHAT_HEADER_TITLE_NAME_CLASS = 'truncate text-[14px] capitalize text-[#18181a]';
export const CHAT_HEADER_TITLE_META_CLASS = 'shrink-0 truncate text-[10px] text-[#757f9c]';
export const CHAT_HEADER_ACTIONS_CLASS = 'flex shrink-0 items-center gap-[6px]';

// ---------------------------------------------------------------------------
// Message scroller
// ---------------------------------------------------------------------------
export const CHAT_MESSAGES_CLASS = 'min-h-0 flex-1 overflow-y-auto px-[24px] pt-[22px] pb-[62px]';
export const CHAT_MESSAGE_STACK_CLASS = 'mx-auto flex w-full max-w-[820px] flex-col gap-[20px]';

export const CHAT_MESSAGE_ITEM_CLASS = 'flex min-w-0 flex-col';
export const CHAT_MESSAGE_ROW_BASE_CLASS = 'flex min-w-0';
export const CHAT_MESSAGE_ROW_USER_CLASS = 'justify-end';
export const CHAT_MESSAGE_ROW_ASSISTANT_CLASS = 'justify-start';

export const CHAT_BUBBLE_BASE_CLASS =
  'relative box-border min-w-0 max-w-[min(680px,92%)] text-[14px] leading-[1.7] wrap-anywhere text-[#18181a]';
export const CHAT_BUBBLE_ASSISTANT_CLASS =
  'w-full max-w-full rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[18px] py-[14px] shadow-[0_1px_2px_rgba(24,24,26,0.03)]';
export const CHAT_BUBBLE_USER_CLASS =
  'rounded-[14px] bg-[#f6f6f6] px-[16px] py-[11px] text-[#18181a]';
export const CHAT_BUBBLE_ERROR_CLASS = 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]';
export const CHAT_BUBBLE_STREAMING_PLACEHOLDER_CLASS =
  'inline-grid h-[42px] w-[48px] max-w-[48px] place-items-center px-0 py-0';

export function chatRowClass(role: 'user' | 'assistant' | 'system' | 'tool'): string {
  return cn(
    CHAT_MESSAGE_ROW_BASE_CLASS,
    role === 'user' ? CHAT_MESSAGE_ROW_USER_CLASS : CHAT_MESSAGE_ROW_ASSISTANT_CLASS,
  );
}

export function chatBubbleClass(role: 'user' | 'assistant' | 'system' | 'tool', isError?: boolean): string {
  return cn(
    CHAT_BUBBLE_BASE_CLASS,
    role === 'user' ? CHAT_BUBBLE_USER_CLASS : CHAT_BUBBLE_ASSISTANT_CLASS,
    isError && CHAT_BUBBLE_ERROR_CLASS,
  );
}

// User plain answer + scheduled chip
export const CHAT_PLAIN_ANSWER_CLASS = 'flex flex-col items-end gap-[6px] whitespace-pre-wrap';
export const CHAT_MESSAGE_MODE_CHIP_CLASS =
  'inline-flex items-center gap-[4px] rounded-full bg-[#eef0f4] px-[9px] py-[2px] text-[11px] font-medium text-[#464c5e]';
export const CHAT_QUEUED_STATUS_ROW_CLASS = 'mt-[5px] flex justify-end pr-[2px]';
export const CHAT_QUEUED_STATUS_CLASS =
  'inline-flex items-center gap-[4px] text-[11px] font-medium leading-[1.4] text-[#858b9c]';
export const CHAT_TYPING_CARET_CLASS =
  'inline-block h-[15px] w-[2px] translate-y-[2px] animate-pulse bg-[#18181a]';

// ---------------------------------------------------------------------------
// Markdown answer (styled via child selectors, code delegates to CodeBlock)
// ---------------------------------------------------------------------------
export const CHAT_MARKDOWN_CLASS = cn(
  'min-w-0 max-w-full text-[14px] leading-[1.75] text-[#18181a]',
  '[&>*:first-child]:mt-0 [&>*:last-child]:mb-0',
  '[&_p]:my-[8px] [&_p]:wrap-anywhere',
  '[&_h1]:mt-[16px] [&_h1]:mb-[8px] [&_h1]:text-[18px] [&_h1]:font-semibold',
  '[&_h2]:mt-[16px] [&_h2]:mb-[8px] [&_h2]:text-[16px] [&_h2]:font-semibold',
  '[&_h3]:mt-[14px] [&_h3]:mb-[6px] [&_h3]:text-[15px] [&_h3]:font-semibold',
  '[&_h4]:mt-[12px] [&_h4]:mb-[6px] [&_h4]:text-[14px] [&_h4]:font-semibold',
  '[&_ul]:my-[8px] [&_ul]:pl-[22px] [&_ul]:list-disc',
  '[&_ol]:my-[8px] [&_ol]:pl-[22px] [&_ol]:list-decimal',
  '[&_li]:my-[3px]',
  '[&_a]:text-[#0b6cf5] [&_a]:underline [&_a]:underline-offset-2',
  '[&_.md-link-label]:text-[#0b6cf5] [&_.md-link-label]:underline [&_.md-link-label]:underline-offset-2',
  '[&_blockquote]:my-[10px] [&_blockquote]:border-l-[3px] [&_blockquote]:border-[#dfe5f2] [&_blockquote]:pl-[12px] [&_blockquote]:text-[#5b6273]',
  '[&_hr]:my-[12px] [&_hr]:border-0 [&_hr]:border-t [&_hr]:border-[#e3e7f1]',
  '[&_strong]:font-semibold',
  '[&_code]:rounded-[5px] [&_code]:bg-[#f1f2f5] [&_code]:px-[5px] [&_code]:py-[1px] [&_code]:font-mono [&_code]:text-[12.5px]',
  '[&_.code-block-vscode]:my-[10px]',
);
export const CHAT_MD_TABLE_SCROLL_CLASS = 'my-[10px] max-w-full overflow-x-auto';
export const CHAT_MD_TABLE_CLASS =
  'w-full border-collapse text-[13px] [&_th]:border [&_th]:border-[#e3e7f1] [&_th]:bg-[#f7f8fa] [&_th]:px-[10px] [&_th]:py-[6px] [&_th]:font-semibold [&_td]:border [&_td]:border-[#e3e7f1] [&_td]:px-[10px] [&_td]:py-[6px]';

// ---------------------------------------------------------------------------
// Execution record (执行记录 trace panel)
// ---------------------------------------------------------------------------
export const CHAT_TRACE_WRAP_CLASS = 'mb-[10px] min-w-0';
export const CHAT_TRACE_SUMMARY_CLASS =
  'inline-flex cursor-pointer items-center gap-[7px] border-0 bg-transparent p-0 text-[13px] font-semibold leading-[1.5] text-[#464c5e] transition-colors hover:text-[#18181a]';
export const CHAT_TRACE_SUMMARY_RUNNING_CLASS = 'text-[#18181a]';
export const CHAT_TRACE_SUMMARY_FAILED_CLASS = 'text-[#d20b0b]';
export const CHAT_TRACE_ICON_CLASS =
  'inline-flex size-[18px] shrink-0 items-center justify-center text-[#858b9c] [&>svg]:block [&>svg]:size-[14px]';
export const CHAT_TRACE_CHEVRON_CLASS = 'transition-transform duration-150';
export const CHAT_TRACE_CHEVRON_EXPANDED_CLASS = 'rotate-90';
export const CHAT_TRACE_DETAILS_CLASS =
  'mt-[8px] grid gap-[8px] border-l-[1.5px] border-[#eef0f4] pl-[14px]';
export const CHAT_TRACE_LINE_CLASS = 'grid grid-cols-[18px_minmax(0,1fr)] items-start gap-[8px]';
export const CHAT_TRACE_LINE_CONTENT_CLASS = 'grid grid-cols-[minmax(0,1fr)] min-w-0 gap-[4px]';
export const CHAT_TRACE_LINE_TEXT_CLASS = 'text-[13px] leading-[1.5] text-[#464c5e] wrap-anywhere';
export const CHAT_TRACE_FLOW_TEXT_CLASS = 'sd1-trace-flow-text';
export const CHAT_TRACE_LINE_TEXT_FAILED_CLASS = 'text-[#d20b0b]';
export const CHAT_TRACE_LINE_DETAIL_CLASS = 'text-[12px] leading-[1.5] text-[#858b9c] wrap-anywhere';
export const CHAT_TRACE_CODE_SUMMARY_CLASS =
  'cursor-pointer text-[12px] font-medium text-[#757f9c] hover:text-[#18181a]';
export const CHAT_TRACE_CODE_DETAILS_CLASS = 'block min-w-0 max-w-full overflow-hidden';
export const CHAT_TRACE_CODE_BLOCK_CLASS =
  'mt-[6px] max-h-[420px] w-full max-w-full overflow-auto overscroll-contain';

// ---------------------------------------------------------------------------
// Citations
// ---------------------------------------------------------------------------
export const CHAT_CITATIONS_CLASS = 'mt-[12px] grid gap-[8px] border-t border-[#f0f1f4] pt-[10px]';
export const CHAT_CITATION_HEADING_CLASS =
  'inline-flex items-center gap-[6px] text-[12px] font-semibold text-[#757f9c]';
export const CHAT_CITATION_LIST_CLASS = 'flex flex-wrap gap-[6px]';
export const CHAT_CITATION_CHIP_CLASS =
  'inline-flex max-w-full items-center gap-[6px] rounded-[8px] border border-[#e3e7f1] bg-[#fafbfc] px-[9px] py-[5px] text-left text-[12px] text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:bg-white';
export const CHAT_CITATION_INDEX_CLASS = 'shrink-0 font-semibold text-[#18181a]';
export const CHAT_CITATION_TITLE_CLASS = 'min-w-0 truncate';

// ---------------------------------------------------------------------------
// Message attachments (in-bubble)
// ---------------------------------------------------------------------------
export const CHAT_ATTACHMENT_LIST_CLASS = 'mt-[10px] grid gap-[8px]';
export const CHAT_ATTACHMENT_CARD_CLASS =
  'grid min-h-[46px] w-[min(280px,100%)] grid-cols-[36px_minmax(0,1fr)] items-center gap-[10px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] p-[7px]';
export const CHAT_ATTACHMENT_IMG_CLASS = 'size-[36px] rounded-[8px] object-cover';
export const CHAT_ATTACHMENT_FILE_ICON_CLASS =
  'inline-grid size-[36px] place-items-center rounded-[8px] bg-[#eef0f4] text-[#464c5e]';
export const CHAT_ATTACHMENT_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_ATTACHMENT_NAME_CLASS = 'truncate text-[12px] font-medium text-[#18181a]';
export const CHAT_ATTACHMENT_META_CLASS = 'truncate text-[11px] text-[#858b9c]';

// ---------------------------------------------------------------------------
// Feedback actions
// ---------------------------------------------------------------------------
export const CHAT_FEEDBACK_CLASS = 'mt-[10px] flex items-center gap-[4px]';
export const CHAT_FEEDBACK_BTN_CLASS =
  'inline-grid size-[28px] place-items-center rounded-[8px] border-0 bg-transparent p-0 text-[#a2a8b8] transition-colors hover:bg-[#f1f2f5] hover:text-[#18181a]';
export const CHAT_FEEDBACK_BTN_ACTIVE_CLASS = 'bg-[#eef0f4] text-[#18181a]';
export const CHAT_FEEDBACK_BTN_DISLIKE_ACTIVE_CLASS = 'bg-[#fce7e7] text-[#d20b0b] hover:bg-[#fce7e7] hover:text-[#d20b0b]';

// ---------------------------------------------------------------------------
// Empty state (Hello {name})
// ---------------------------------------------------------------------------
export const CHAT_EMPTY_CLASS =
  'mx-auto flex w-full max-w-[640px] flex-col gap-[16px] pt-[8vh]';
export const CHAT_EMPTY_GREETING_CARD_CLASS =
  'flex w-full items-end justify-between rounded-[20px] bg-[#f6f6f6] pl-[8px] pr-[16px]';
export const CHAT_EMPTY_TITLE_CLASS = 'text-[36px] font-semibold leading-none text-[#18181a]';
export const CHAT_EMPTY_SUBTITLE_CLASS = 'text-[18px] font-medium text-[#464c5e]';
export const CHAT_EMPTY_CARD_CLASS =
  'flex w-full items-stretch gap-[8px] rounded-[20px] bg-[#f6f6f6] p-[16px]';
export const CHAT_EMPTY_ROLE_CLASS = 'line-clamp-2 text-[12px] capitalize leading-[1.5] text-[#757f9c]';
export const CHAT_EMPTY_TAGS_CLASS =
  'flex flex-wrap items-center gap-[10px] [&>span]:rounded-[10px] [&>span]:border-[0.5px] [&>span]:border-[#e3e7f1] [&>span]:px-[10px] [&>span]:py-[4px] [&>span]:text-[10px] [&>span]:capitalize [&>span]:text-[#757f9c]';
export const CHAT_EMPTY_STAT_CELL_CLASS =
  'flex flex-1 flex-col justify-center gap-[4px] border-[0.5px] border-[#e3e7f1] px-[20px] py-[8px] capitalize text-[#464c5e] first:rounded-l-[14px] last:rounded-r-[14px] [&:not(:first-child)]:ml-[-0.5px]';

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------
export const CHAT_INPUT_SHELL_CLASS = 'shrink-0 px-[24px] pb-[20px] pt-[6px]';
export const CHAT_COMPOSER_STAGE_CLASS = 'relative mx-auto w-full max-w-[820px]';
export const CHAT_COMPOSER_AVATAR_CLASS =
  'absolute bottom-full left-[16px] z-10 mb-[2px] size-[44px] shrink-0 overflow-hidden';
export const CHAT_COMPOSER_FORM_CLASS =
  'relative flex min-w-0 flex-1 flex-col gap-[10px] rounded-[14px] border-[0.5px] border-[#18181a] bg-white p-[12px] shadow-[0_6px_24px_rgba(24,24,26,0.08)] transition-colors';
export const CHAT_COMPOSER_FORM_DRAG_CLASS = 'border-dashed border-[#0b6cf5] bg-[#f5f9ff]';
export const CHAT_COMPOSER_DROP_HINT_CLASS =
  'pointer-events-none absolute inset-0 z-[2] grid place-items-center rounded-[14px] bg-white/85 text-[14px] font-medium text-[#18181a] backdrop-blur-sm';
export const CHAT_COMPOSER_TEXTAREA_CLASS =
  'min-h-[48px] max-h-[200px] w-full resize-none border-0 bg-transparent px-[4px] py-[2px] text-[14px] leading-[1.7] text-[#18181a] shadow-none outline-none placeholder:text-[#b3b8c4] focus-visible:ring-0';
export const CHAT_COMPOSER_ATTACHMENTS_CLASS = 'flex flex-wrap gap-[8px]';
export const CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS =
  'inline-flex max-w-[240px] items-center gap-[7px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] py-[5px] pl-[7px] pr-[6px] text-[12px] text-[#464c5e]';
export const CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS = 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]';
export const CHAT_COMPOSER_ATTACHMENT_IMG_CLASS = 'size-[24px] rounded-[6px] object-cover';
export const CHAT_COMPOSER_ATTACHMENT_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_COMPOSER_ATTACHMENT_NAME_CLASS = 'truncate text-[12px] font-medium text-[#18181a]';
export const CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS = 'truncate text-[11px] text-[#858b9c]';
export const CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS =
  'inline-grid size-[18px] shrink-0 place-items-center rounded-full border-0 bg-transparent p-0 text-[15px] leading-none text-[#a2a8b8] hover:text-[#18181a]';

export const CHAT_COMPOSER_TOOLBAR_CLASS = 'flex items-center justify-between gap-[10px]';
export const CHAT_COMPOSER_CONTEXT_ROW_CLASS = 'flex min-w-0 items-center gap-[8px]';
export const CHAT_COMPOSER_ACTIONS_ROW_CLASS = 'flex shrink-0 items-center gap-[8px]';
export const CHAT_COMPOSER_PLUS_BTN_CLASS =
  'inline-grid size-[32px] place-items-center rounded-[9px] border border-[#e3e7f1] bg-white p-0 text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-45';
export const CHAT_COMPOSER_INTENT_CHIP_CLASS =
  'inline-flex cursor-pointer items-center gap-[5px] rounded-full border border-[#e3e7f1] bg-[#f4f5f7] py-[4px] pl-[6px] pr-[10px] text-[12px] font-medium text-[#464c5e] transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#c9d2e4] focus-visible:ring-offset-1';
export const CHAT_COMPOSER_HINT_CLASS = 'truncate text-[11px] text-[#b3b8c4] max-[560px]:hidden';
export const CHAT_COMPOSER_MODEL_BTN_CLASS =
  'inline-flex h-[32px] max-w-[200px] items-center gap-[5px] rounded-[9px] border border-[#e3e7f1] bg-white px-[12px] text-[12px] font-normal text-[#757f9c] shadow-none transition-colors hover:border-[#c9d2e4] hover:text-[#18181a] disabled:cursor-not-allowed disabled:opacity-45 aria-expanded:border-[#c9d2e4] aria-expanded:text-[#18181a] [&>span:first-child]:min-w-0 [&>span:first-child]:truncate';
export const CHAT_COMPOSER_SEND_BTN_CLASS =
  'inline-grid size-[36px] place-items-center rounded-[10px] bg-[#18181a] p-0 text-white transition-colors hover:bg-[#303030] disabled:cursor-not-allowed disabled:opacity-40';
export const CHAT_COMPOSER_STOP_BTN_CLASS = 'bg-[#d20b0b] hover:bg-[#b40a0a]';

export const CHAT_MENU_CONTENT_CLASS =
  'flex flex-col gap-[6px] rounded-[14px] border-0 bg-white p-[6px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0 [--accent:#F6F6F6] [--accent-foreground:#18181A]';
export const CHAT_MENU_ITEM_CLASS =
  'h-[36px] cursor-pointer gap-[8px] rounded-[10px] px-[12px] text-[14px] text-[#464C5E]';

export const CHAT_MODEL_MENU_ITEM_CLASS =
  'flex h-auto cursor-pointer items-center justify-between gap-[16px] rounded-[10px] px-[12px] py-[8px] text-[#464C5E]';
export const CHAT_MODEL_MENU_COPY_CLASS = 'grid min-w-0 gap-px';
export const CHAT_MODEL_MENU_NAME_CLASS = 'truncate text-[13px] font-medium text-[#18181a]';
export const CHAT_MODEL_MENU_DETAIL_CLASS = 'truncate text-[11px] text-[#858b9c]';

// ---------------------------------------------------------------------------
// Scheduled task draft card
// ---------------------------------------------------------------------------
export const CHAT_DRAFT_CARD_CLASS =
  'mt-[12px] grid gap-[12px] rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-[#fafbfc] p-[14px]';
export const CHAT_DRAFT_CARD_CREATED_CLASS = 'border-[#96d9b0] bg-[#f2fbf5]';
export const CHAT_DRAFT_HEADER_CLASS = 'flex items-start justify-between gap-[12px]';
export const CHAT_DRAFT_IDENTITY_CLASS = 'flex min-w-0 items-center gap-[10px]';
export const CHAT_DRAFT_ICON_CLASS =
  'inline-grid size-[34px] shrink-0 place-items-center rounded-[10px] bg-[#eef0f4] text-[#464c5e]';
export const CHAT_DRAFT_KICKER_CLASS = 'text-[11px] font-medium text-[#858b9c]';
export const CHAT_DRAFT_TITLE_CLASS = 'text-[14px] font-semibold text-[#18181a]';
export const CHAT_DRAFT_TOP_ACTIONS_CLASS = 'flex shrink-0 items-center gap-[6px]';
export const CHAT_DRAFT_CREATED_BADGE_CLASS =
  'inline-flex items-center gap-[4px] rounded-full bg-[#e9f7ef] px-[10px] py-[3px] text-[12px] font-medium text-[#018434]';
export const CHAT_DRAFT_META_GRID_CLASS = 'grid grid-cols-3 gap-[10px] max-[520px]:grid-cols-1';
export const CHAT_DRAFT_META_ITEM_CLASS =
  'grid gap-[3px] rounded-[10px] bg-white px-[10px] py-[8px] [&>span]:text-[11px] [&>span]:text-[#858b9c] [&>strong]:text-[13px] [&>strong]:font-medium [&>strong]:text-[#18181a]';
export const CHAT_DRAFT_PROMPT_CLASS =
  'grid gap-[4px] [&>span]:text-[11px] [&>span]:text-[#858b9c] [&>p]:text-[13px] [&>p]:leading-[1.6] [&>p]:text-[#464c5e]';
export const CHAT_DRAFT_EDITOR_CLASS =
  'grid grid-cols-2 gap-[10px] max-[520px]:grid-cols-1 [&_label]:grid [&_label]:gap-[5px] [&_label>span]:text-[12px] [&_label>span]:text-[#757f9c]';
export const CHAT_DRAFT_EDITOR_FULL_CLASS = 'col-span-full';
export const CHAT_DRAFT_FOOTER_CLASS = 'flex justify-end gap-[8px]';

// ---------------------------------------------------------------------------
// Dialogs (handoff inbox + citation detail + rename)
// ---------------------------------------------------------------------------
export const CHAT_HANDOFF_LIST_CLASS = 'grid max-h-[60vh] gap-[14px] overflow-y-auto';
export const CHAT_HANDOFF_CARD_CLASS = 'grid gap-[12px] rounded-[14px] border border-[#e3e7f1] bg-[#fafbfc] p-[16px]';
export const CHAT_HANDOFF_HEAD_CLASS =
  'flex items-center gap-[10px] [&_strong]:block [&_strong]:text-[14px] [&_strong]:font-semibold [&_strong]:text-[#18181a] [&_span]:text-[12px] [&_span]:text-[#858b9c]';
export const CHAT_HANDOFF_BLOCK_CLASS =
  'grid gap-[4px] [&>span]:text-[12px] [&>span]:font-medium [&>span]:text-[#757f9c] [&>p]:text-[13px] [&>p]:leading-[1.6] [&>p]:text-[#464c5e]';
export const CHAT_HANDOFF_ACTIONS_CLASS = 'flex justify-end gap-[8px]';
export const CHAT_HANDOFF_EMPTY_CLASS = 'py-[36px] text-center text-[13px] text-[#858b9c]';

export const CHAT_CITATION_DETAIL_CLASS = 'grid gap-[14px]';
export const CHAT_CITATION_DETAIL_EYEBROW_CLASS = 'text-[12px] font-medium text-[#858b9c]';
export const CHAT_CITATION_DETAIL_TITLE_CLASS = 'text-[18px] font-semibold text-[#18181a]';
export const CHAT_CITATION_DETAIL_SECTION_CLASS =
  'grid gap-[5px] [&>span]:text-[12px] [&>span]:font-medium [&>span]:text-[#757f9c] [&>p]:text-[13px] [&>p]:leading-[1.7] [&>p]:text-[#464c5e]';
export const CHAT_CITATION_DETAIL_QUOTE_CLASS =
  'm-0 max-h-[min(52vh,520px)] overflow-y-auto overscroll-contain rounded-[10px] border-l-[3px] border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[8px] text-[13px] leading-[1.7] whitespace-pre-wrap wrap-anywhere text-[#464c5e]';
export const CHAT_CITATION_DETAIL_MARKDOWN_CLASS =
  'max-h-[min(52vh,520px)] min-w-0 max-w-full overflow-y-auto overscroll-contain rounded-[10px] border-l-[3px] border-[#e3e7f1] bg-[#fafbfc] px-[12px] py-[8px] [&>div]:text-[13px] [&>div]:leading-[1.7] [&>div]:text-[#464c5e] [&_h1]:text-[16px] [&_h2]:text-[15px] [&_h3]:text-[14px] [&_h4]:text-[13px] [&_p]:wrap-anywhere [&_li]:wrap-anywhere [&_code]:wrap-anywhere [&_a]:wrap-anywhere';
export const CHAT_CITATION_DETAIL_GRID_CLASS =
  'grid grid-cols-2 gap-[12px] max-[520px]:grid-cols-1 [&>div]:grid [&>div]:gap-[3px] [&_span]:text-[11px] [&_span]:text-[#858b9c] [&_strong]:text-[13px] [&_strong]:font-medium [&_strong]:text-[#18181a]';
export const CHAT_CITATION_DETAIL_NOTE_CLASS = 'text-[12px] leading-[1.6] text-[#858b9c]';

export const CHAT_DEBUG_PANEL_CLASS =
  'mx-auto mt-[16px] max-w-[820px] overflow-auto rounded-[10px] bg-[#1e1e1e] p-[12px] text-[12px] text-[#d4d4d4]';
