import type { FormEvent, MouseEvent } from 'react';
import { useEffect, useRef, useState } from 'react';

import EmployeeAvatar from '@/components/EmployeeAvatar';
import StaffdeckIcon from '@/components/StaffdeckIcon';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card';
import { employeeDisplayName } from '@/employee';
import { cn } from '@/lib/utils';
import { useI18n } from '@/i18n';

import {
  CHAT_COMPOSER_ACTIONS_ROW_CLASS,
  CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
  CHAT_COMPOSER_ATTACHMENT_COPY_CLASS,
  CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
  CHAT_COMPOSER_ATTACHMENT_IMG_CLASS,
  CHAT_COMPOSER_ATTACHMENT_NAME_CLASS,
  CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS,
  CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS,
  CHAT_COMPOSER_ATTACHMENTS_CLASS,
  CHAT_COMPOSER_AVATAR_CLASS,
  CHAT_COMPOSER_CONTEXT_ROW_CLASS,
  CHAT_COMPOSER_DROP_HINT_CLASS,
  CHAT_COMPOSER_FORM_CLASS,
  CHAT_COMPOSER_FORM_DRAG_CLASS,
  CHAT_COMPOSER_HINT_CLASS,
  CHAT_COMPOSER_INTENT_CHIP_CLASS,
  CHAT_COMPOSER_MODEL_BTN_CLASS,
  CHAT_COMPOSER_PLUS_BTN_CLASS,
  CHAT_COMPOSER_SEND_BTN_CLASS,
  CHAT_COMPOSER_STAGE_CLASS,
  CHAT_COMPOSER_STOP_BTN_CLASS,
  CHAT_COMPOSER_TEXTAREA_CLASS,
  CHAT_INPUT_SHELL_CLASS,
  CHAT_MENU_CONTENT_CLASS,
  CHAT_MENU_ITEM_CLASS,
  CHAT_MODEL_MENU_COPY_CLASS,
  CHAT_MODEL_MENU_DETAIL_CLASS,
  CHAT_MODEL_MENU_ITEM_CLASS,
  CHAT_MODEL_MENU_NAME_CLASS,
} from '../chatPageStyles';
import { attachmentTypeLabel, modelDetailText, modelDisplayName } from '../chatHelpers';
import type { UseChatSession } from '../useChatSession';

export default function Composer({ chat }: { chat: UseChatSession }) {
  const { t } = useI18n();
  const {
    input,
    setInput,
    composerAttachments,
    composerDragActive,
    composerPlusOpen,
    setComposerPlusOpen,
    composerIntent,
    setComposerIntent,
    readyComposerAttachments,
    uploadingComposerAttachment,
    currentSessionRunning,
    composerActive,
    showComposerAvatar,
    displayedProfile,
    displayedAgent,
    emptyRoleSummary,
    emptyProfileTags,
    emptyStats,
    enabledModelConfigs,
    selectedModelConfig,
    changeModelConfig,
    isComposing,
    setIsComposing,
    fileInputRef,
    send,
    abortStream,
    handleComposerPaste,
    handleComposerFileChange,
    handleComposerDragEnter,
    handleComposerDragOver,
    handleComposerDragLeave,
    handleComposerDrop,
    removeComposerAttachment,
    handleComposerPlusAction,
  } = chat;

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const stopClickArmedRef = useRef(false);
  const [scheduleIntentHovered, setScheduleIntentHovered] = useState(false);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`;
  }, [input]);

  useEffect(() => {
    if (composerIntent !== 'scheduled_task') {
      setScheduleIntentHovered(false);
    }
  }, [composerIntent]);

  const sendDisabled = !currentSessionRunning
    && ((!input.trim() && readyComposerAttachments.length === 0) || uploadingComposerAttachment);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (stopClickArmedRef.current) {
      stopClickArmedRef.current = false;
      return;
    }
    void send();
  };

  const handleSendButtonMouseDown = (event: MouseEvent<HTMLButtonElement>) => {
    if (!currentSessionRunning) return;
    event.preventDefault();
    event.stopPropagation();
    stopClickArmedRef.current = true;
    abortStream();
  };

  const handleSendButtonClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (currentSessionRunning || stopClickArmedRef.current) {
      event.preventDefault();
      event.stopPropagation();
      const shouldAbort = currentSessionRunning;
      stopClickArmedRef.current = false;
      if (shouldAbort) abortStream();
      return;
    }
    void send();
  };

  return (
    <div className={CHAT_INPUT_SHELL_CLASS}>
      <div className={CHAT_COMPOSER_STAGE_CLASS}>
        {showComposerAvatar && displayedProfile && (
          <HoverCard openDelay={80} closeDelay={80}>
            <HoverCardTrigger asChild>
              <button
                type="button"
                aria-label="员工信息"
                className={cn(CHAT_COMPOSER_AVATAR_CLASS, 'block cursor-pointer outline-none')}
              >
                <EmployeeAvatar profile={displayedProfile} size={44} className="size-full" />
              </button>
            </HoverCardTrigger>
            <HoverCardContent
              side="left"
              align="end"
              sideOffset={10}
              className="flex w-[220px] flex-col items-start gap-[8px] rounded-[20px] border-0 bg-white p-0 py-[4px] shadow-[0px_16px_15px_rgba(0,0,0,0.1)] ring-0"
            >
              <div className="flex w-full flex-col px-[6px]">
                <div className="flex h-[46px] w-full flex-col items-center justify-end rounded-[14px] bg-[#f6f6f6] pb-[4px] pl-[8px] pr-[16px] pt-[8px]">
                  <div className="flex w-full items-end">
                    <div className="flex items-end gap-[4px]">
                      <EmployeeAvatar
                        profile={displayedProfile}
                        agent={displayedAgent ?? undefined}
                        width={60}
                        height={60}
                        radius={30}
                        objectPosition="bottom"
                      />
                      <div className="flex h-[36px] flex-col justify-center gap-[2px] whitespace-nowrap pb-[2px] text-[10px] capitalize leading-normal">
                        <p className="font-medium text-[#464c5e]">
                          {displayedAgent ? employeeDisplayName(displayedAgent) : displayedProfile.roleName}
                        </p>
                        <p className="text-[#757f9c]">{displayedProfile.roleName}</p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <div className="flex w-full flex-col gap-[8px] px-[8px]">
                <p className="w-full text-[10px] capitalize leading-[14px] text-[#757f9c]">
                  {emptyRoleSummary}
                </p>
                {emptyProfileTags.length > 0 && (
                  <div className="flex w-full flex-wrap content-center items-center gap-[4px]">
                    {emptyProfileTags.map((tag, index) => (
                      <div
                        key={`${tag}-${index}`}
                        className="flex h-[16px] items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] px-[8px] py-[2px]"
                      >
                        <span className="whitespace-nowrap text-[8px] capitalize leading-normal text-[#757f9c]">
                          {tag}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="flex w-full flex-col px-[8px] pb-[8px]">
                <div className="flex w-full items-start whitespace-nowrap capitalize leading-normal">
                  {emptyStats.map((item, index) => (
                    <div
                      key={item.label}
                      className={cn(
                        'flex flex-1 flex-col justify-center gap-[4px] border-[0.5px] border-[#e3e7f1] px-[12px] py-[6px]',
                        index === 0 && 'rounded-l-[14px]',
                        index === emptyStats.length - 1 && 'rounded-r-[14px]',
                      )}
                    >
                      <p className="text-[16px] font-medium text-[#18181a]">{item.value}</p>
                      <p className="text-[10px] text-[#464c5e]">{item.label}</p>
                    </div>
                  ))}
                </div>
              </div>
            </HoverCardContent>
          </HoverCard>
        )}
        <form
          className={cn(CHAT_COMPOSER_FORM_CLASS, composerDragActive && CHAT_COMPOSER_FORM_DRAG_CLASS)}
          onDragEnter={handleComposerDragEnter}
          onDragOver={handleComposerDragOver}
          onDragLeave={handleComposerDragLeave}
          onDrop={handleComposerDrop}
          onSubmit={handleSubmit}
        >
          <input
            ref={fileInputRef}
            className="hidden"
            type="file"
            multiple
            onChange={handleComposerFileChange}
          />
          {composerDragActive && <div className={CHAT_COMPOSER_DROP_HINT_CLASS}>松开上传文件</div>}

          {composerAttachments.length > 0 && (
            <div className={CHAT_COMPOSER_ATTACHMENTS_CLASS}>
              {composerAttachments.map((attachment) => (
                <div
                  className={cn(
                    CHAT_COMPOSER_ATTACHMENT_CHIP_CLASS,
                    attachment.uploadStatus === 'error' && CHAT_COMPOSER_ATTACHMENT_ERROR_CLASS,
                  )}
                  key={attachment.uploadKey}
                >
                  {attachment.kind === 'image' && attachment.data_url ? (
                    <img className={CHAT_COMPOSER_ATTACHMENT_IMG_CLASS} src={attachment.data_url} alt={attachment.filename} />
                  ) : (
                    <StaffdeckIcon name={attachment.kind === 'pdf' ? 'file' : 'folder'} size={16} />
                  )}
                  <span className={CHAT_COMPOSER_ATTACHMENT_COPY_CLASS}>
                    <span className={CHAT_COMPOSER_ATTACHMENT_NAME_CLASS}>{attachment.filename}</span>
                    <span className={CHAT_COMPOSER_ATTACHMENT_STATUS_CLASS}>
                      {attachment.uploadStatus === 'uploading' && '解析中'}
                      {attachment.uploadStatus === 'ready' && attachmentTypeLabel(attachment)}
                      {attachment.uploadStatus === 'error' && (attachment.error || '上传失败')}
                    </span>
                  </span>
                  <button
                    type="button"
                    className={CHAT_COMPOSER_ATTACHMENT_REMOVE_CLASS}
                    onClick={() => removeComposerAttachment(attachment.uploadKey)}
                    aria-label="移除附件"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}

          <textarea
            ref={textareaRef}
            className={CHAT_COMPOSER_TEXTAREA_CLASS}
            value={input}
            rows={2}
            placeholder={t('输入消息，按 Enter 发送...')}
            onChange={(event) => setInput(event.target.value)}
            onPaste={handleComposerPaste}
            onCompositionStart={() => setIsComposing(true)}
            onCompositionEnd={() => window.setTimeout(() => setIsComposing(false), 0)}
            onKeyDown={(event) => {
              const nativeEvent = event.nativeEvent as KeyboardEvent & { isComposing?: boolean };
              if (
                event.key === 'Enter'
                && !event.shiftKey
                && !isComposing
                && !nativeEvent.isComposing
                && nativeEvent.keyCode !== 229
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />

          <div className={cn('flex items-center justify-between gap-[10px]', !composerActive && 'opacity-95')}>
            <div className={CHAT_COMPOSER_CONTEXT_ROW_CLASS}>
              <DropdownMenu open={composerPlusOpen} onOpenChange={setComposerPlusOpen}>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className={CHAT_COMPOSER_PLUS_BTN_CLASS}
                    aria-label="添加"
                    title="添加"
                  >
                    <StaffdeckIcon name="plus" size={16} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" side="top" className={cn(CHAT_MENU_CONTENT_CLASS, 'min-w-[160px]')}>
                  <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => handleComposerPlusAction('upload')}>
                    <StaffdeckIcon name="upload" size={16} />
                    <span>上传文件</span>
                  </DropdownMenuItem>
                  <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} onSelect={() => handleComposerPlusAction('scheduled_task')}>
                    <StaffdeckIcon name="clock" size={16} />
                    <span>定时任务</span>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              {composerIntent === 'scheduled_task' && (
                <button
                  type="button"
                  className={CHAT_COMPOSER_INTENT_CHIP_CLASS}
                  onMouseEnter={() => setScheduleIntentHovered(true)}
                  onMouseLeave={() => setScheduleIntentHovered(false)}
                  onFocus={() => setScheduleIntentHovered(true)}
                  onBlur={() => setScheduleIntentHovered(false)}
                  onClick={() => setComposerIntent(null)}
                  aria-label="取消定时任务"
                  title="取消定时任务"
                >
                  <span className={cn(
                    'relative inline-grid size-[16px] shrink-0 place-items-center rounded-full transition-colors',
                    scheduleIntentHovered ? 'text-[#18181a]' : 'text-[#858b9c]',
                  )}
                  >
                    <StaffdeckIcon
                      name="clock"
                      size={14}
                      className={cn('transition-opacity', scheduleIntentHovered ? 'opacity-0' : 'opacity-100')}
                    />
                    <StaffdeckIcon
                      name="close"
                      size={9}
                      className={cn('absolute transition-opacity', scheduleIntentHovered ? 'opacity-100' : 'opacity-0')}
                      style={{ width: 9, height: 9 }}
                    />
                  </span>
                  <span>定时任务</span>
                </button>
              )}
              <div className={CHAT_COMPOSER_HINT_CLASS}>Enter 发送 / Shift+Enter 换行</div>
            </div>
            <div className={CHAT_COMPOSER_ACTIONS_ROW_CLASS}>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className={CHAT_COMPOSER_MODEL_BTN_CLASS}
                    disabled={!enabledModelConfigs.length}
                  >
                    <span>{selectedModelConfig ? modelDisplayName(selectedModelConfig) : '默认模型'}</span>
                    <StaffdeckIcon name="arrow" size={14} style={{ transform: 'rotate(90deg)' }} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" side="top" className={cn(CHAT_MENU_CONTENT_CLASS, 'max-h-[360px] min-w-[240px] overflow-y-auto')}>
                  {enabledModelConfigs.length === 0 ? (
                    <DropdownMenuItem className={CHAT_MENU_ITEM_CLASS} disabled>暂无可用模型</DropdownMenuItem>
                  ) : (
                    enabledModelConfigs.map((model) => (
                      <DropdownMenuItem
                        key={model.id}
                        className={CHAT_MODEL_MENU_ITEM_CLASS}
                        onSelect={() => changeModelConfig(model.id)}
                      >
                        <span className={CHAT_MODEL_MENU_COPY_CLASS}>
                          <span className={CHAT_MODEL_MENU_NAME_CLASS}>{modelDisplayName(model)}</span>
                          <span className={CHAT_MODEL_MENU_DETAIL_CLASS}>{modelDetailText(model)}</span>
                        </span>
                        {selectedModelConfig?.id === model.id && <StaffdeckIcon name="check" size={15} />}
                      </DropdownMenuItem>
                    ))
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
              <button
                type="button"
                className={cn(CHAT_COMPOSER_SEND_BTN_CLASS, currentSessionRunning && CHAT_COMPOSER_STOP_BTN_CLASS)}
                onMouseDown={handleSendButtonMouseDown}
                onClick={handleSendButtonClick}
                disabled={sendDisabled}
                aria-label={currentSessionRunning ? '停止生成' : '发送'}
              >
                <StaffdeckIcon name={currentSessionRunning ? 'stop' : 'send'} size={18} />
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
