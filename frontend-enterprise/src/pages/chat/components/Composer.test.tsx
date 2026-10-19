// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import type { UseChatSession } from '../useChatSession';
import Composer from './Composer';

function buildChat(): UseChatSession {
  return {
    input: '',
    setInput: vi.fn(),
    slashCommands: [],
    composerAttachments: [],
    composerDragActive: false,
    // 直接把加号菜单置为打开：否则 Radix 要等 pointerdown 才渲染菜单内容
    composerPlusOpen: true,
    setComposerPlusOpen: vi.fn(),
    composerIntent: null,
    setComposerIntent: vi.fn(),
    readyComposerAttachments: [],
    uploadingComposerAttachment: false,
    currentSessionRunning: false,
    composerActive: true,
    showComposerAvatar: false,
    displayedProfile: null,
    displayedAgent: null,
    emptyRoleSummary: '',
    emptyProfileTags: [],
    emptyStats: [],
    enabledModelConfigs: [],
    selectedModelConfig: null,
    changeModelConfig: vi.fn(),
    showModelSetupNotice: false,
    modelSetupNoticeText: '',
    freeModelNoticeText: '',
    canConfigureModels: false,
    setModelSetupOpen: vi.fn(),
    isComposing: false,
    setIsComposing: vi.fn(),
    fileInputRef: { current: null },
    send: vi.fn(),
    abortStream: vi.fn(),
    handleComposerPaste: vi.fn(),
    handleComposerFileChange: vi.fn(),
    handleComposerDragEnter: vi.fn(),
    handleComposerDragOver: vi.fn(),
    handleComposerDragLeave: vi.fn(),
    handleComposerDrop: vi.fn(),
    removeComposerAttachment: vi.fn(),
    handleComposerPlusAction: vi.fn(),
    t: (key: string) => key,
  } as unknown as UseChatSession;
}

function renderComposer(props: { hideModelSelector?: boolean; hideScheduledTask?: boolean } = {}) {
  return render(
    <I18nProvider>
      <Composer chat={buildChat()} {...props} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe('Composer plus menu', () => {
  it('站内对话展示「定时任务」入口', () => {
    renderComposer();

    expect(screen.getByText('上传文件')).toBeTruthy();
    expect(screen.getByText('定时任务')).toBeTruthy();
  });

  it('分享访客的加号菜单里没有「定时任务」', () => {
    renderComposer({ hideModelSelector: true, hideScheduledTask: true });

    expect(screen.getByText('上传文件')).toBeTruthy();
    expect(screen.queryByText('定时任务')).toBeNull();
  });
});
