// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import {
  chatSnapshot, SlotTestRuntime, TestRemote, stubSettingsScope, usePinnedBrowserLanguages,
} from '@deepseek-ai/dsh-client-test-runtime'
import { LocaleRuntime } from '@deepseek-ai/dsh-client-locale/client'
import { resolveSlotLabel } from '@deepseek-ai/dsh-client-ui-slots'
import type { ObservableSnapshot } from '@deepseek-ai/dsh-client-store'
import type { SessionBinding } from '@deepseek-ai/dsh-api-session-controller/client'
import type { SessionId } from '@deepseek-ai/dsh-session/types'
import {
  apply as applyConversation, inject as injectConversation,
} from '@deepseek-ai/dsh-client-ui-conversation/client'
import {
  apply as applyChat, EMPTY_CHAT_SNAPSHOT, inject as injectChat,
} from '@deepseek-ai/dsh-client-ui-chat/client'
import type {
  ChatNodeTurnDataInjected, ChatSnapshot, TranscriptViewRowInjected, UseChat,
} from '@deepseek-ai/dsh-client-ui-chat/client'
import { CHAT_SETTINGS_NAMESPACE, type ChatSettings } from '../src/chat-settings.ts'

declare module '@deepseek-ai/dsh-client-ui-conversation/client' {
  interface ConversationTurnDataMap {
    metric: number
  }
}

usePinnedBrowserLanguages('zh-CN')

const SID = 'session-1' as SessionId

async function bench() {
  const runtime = await SlotTestRuntime.create()
  const chatSettings = stubSettingsScope<ChatSettings>()
  runtime.ctx.provide('settingsScope', {
    bind: ({ namespace }: { namespace: string }) => namespace === CHAT_SETTINGS_NAMESPACE
      ? chatSettings.scope
      : stubSettingsScope().scope,
  } as never)
  runtime.ctx.provide('layout', { openDetails: vi.fn(), closeDetails: vi.fn() } as never)
  runtime.ctx.provide('uiWorkspace', {
    connectWorkspace: vi.fn(async () => SID),
  } as never)
  new TestRemote(runtime.ctx, {
    session: { openWorkspacePath: vi.fn(async () => ({ ok: true, value: { opened: true } })) },
  })
  const locale = new LocaleRuntime(runtime.ctx)
  runtime.ctx.provide('locale', locale)
  runtime.slots.installLocale(locale)
  await runtime.root.declare({
    'conversation': { kind: 'single', scope: 'session-maybe' },
    'details': { kind: 'single', scope: 'session' },
    'conversation.approval.detail': { kind: 'single', scope: 'session' },
    'settings.general.item': { kind: 'list', scope: 'root' },
  }, (_props: { renderSlot?: unknown }) => null)
  const conversation = await runtime.mount({
    inject: [...injectConversation],
    apply: applyConversation,
  })
  const provide = vi.spyOn(runtime.ctx.uiSession, 'provide')
  const chat = await runtime.mount({ inject: [...injectChat], apply: applyChat })
  const sourceDescriptor = provide.mock.calls[0]?.[0]
  if (sourceDescriptor === undefined) throw new Error('ui-chat did not provide its standard source')
  return { runtime, conversation, chat, chatSettings, sourceDescriptor }
}

function storeOf(runtime: SlotTestRuntime, key: 'conversation.session' | 'conversation.session.header' | 'conversation.view' | 'details') {
  return (runtime.slots.entries(key)[0] as { store?: unknown } | undefined)?.store
}

describe('Chat apply wiring', () => {
  it('contributes Chat View, node renderers, stats, and details', async () => {
    const b = await bench()
    const views = b.runtime.slots.entries('conversation.view')
    expect(views.map(row => row.options.id)).toEqual(['chat'])
    expect(resolveSlotLabel(views[0]?.options.label)).toBe('对话')
    expect(b.runtime.slots.spec('conversation.chat.node'))
      .toMatchObject({ kind: 'keyed', scope: 'session' })
    expect(b.runtime.slots.entries('conversation.composer.dock').map(row => row.options.id))
      .toEqual(['stats'])
    expect(b.runtime.slots.entries('settings.general.item').map(row => row.options.id))
      .toEqual(['transcript-view', 'composer-enter'])
    expect(b.runtime.slots.entries('details')).toHaveLength(1)
    await b.runtime.dispose()
  })

  it('mirrors the Host transcript preference into its Settings row', async () => {
    const b = await bench()
    const row = b.runtime.slots.entries('settings.general.item')
      .find(entry => entry.options.id === 'transcript-view')!
    const face = (row.inject as unknown as () => TranscriptViewRowInjected)()

    expect(face.hooks.transcriptView.getSnapshot()).toBe('compact')
    face.setTranscriptView('normal')
    expect(face.hooks.transcriptView.getSnapshot()).toBe('normal')
    expect(b.chatSettings.set).toHaveBeenCalledWith('transcriptView', 'normal')

    b.chatSettings.publish({
      status: 'ready', value: { transcriptView: 'compact' }, revision: 1, writable: true,
    })
    expect(face.hooks.transcriptView.getSnapshot()).toBe('compact')
    await b.runtime.dispose()
  })

  it('shares one Chat store while keeping it distinct from Conversation state', async () => {
    const b = await bench()
    const conversationStore = storeOf(b.runtime, 'conversation.session')
    const chatStore = storeOf(b.runtime, 'conversation.view')
    expect(storeOf(b.runtime, 'conversation.session.header')).toBe(conversationStore)
    expect(storeOf(b.runtime, 'details')).toBe(chatStore)
    expect(chatStore).toBeDefined()
    expect(chatStore).not.toBe(conversationStore)
    await b.runtime.dispose()
  })

  it('removes only Chat contributions when Chat unloads', async () => {
    const b = await bench()
    await b.chat.dispose()
    expect(b.runtime.slots.entries('conversation.view')).toHaveLength(0)
    expect(b.runtime.slots.spec('conversation.chat.node')).toBeUndefined()
    expect(b.runtime.slots.entries('conversation')).toHaveLength(1)
    expect(b.runtime.ctx.get('uiConversation')).toBeDefined()
    await b.runtime.dispose()
  })

  it('keeps the Chat standard source total while its target enters and leaves', async () => {
    const b = await bench()
    await b.runtime.sessions.add({ id: SID }, { current: false })
    const binding = b.runtime.sessions.binding(SID)
    if (binding === undefined) throw new Error('Chat source test Session binding is unavailable')
    const resolveSource = (owner: SessionBinding): ObservableSnapshot<ChatSnapshot> => {
      const contribution = b.sourceDescriptor.resolve(owner) as {
        hooks: { chat: ObservableSnapshot<ChatSnapshot> }
      }
      return contribution.hooks.chat
    }
    const source = b.runtime.ctx.uiSession.adapter.resolve(SID)!.hooks.chat as
      ObservableSnapshot<ChatSnapshot>
    expect(resolveSource(binding)).toBe(source)
    expect(resolveSource(binding)).toBe(source)
    const listener = vi.fn()
    const off = source.subscribe(listener)

    expect(source.getSnapshot()).toBeDefined()
    await b.chat.dispose()
    expect(source.getSnapshot()).toBe(EMPTY_CHAT_SNAPSHOT)

    off()
    await b.runtime.dispose()
  })

  it('binds Turn data through the Chat selector hook for Turn and Step locations', async () => {
    const b = await bench()
    const spec = b.runtime.slots.spec('conversation.chat.node') as unknown as {
      inject: ChatNodeTurnDataInjected
    }
    let snapshot: ChatSnapshot = chatSnapshot()
    const useChat = ((selector: (value: ChatSnapshot) => unknown) => selector(snapshot)) as UseChat
    const useTurnData = spec.inject.hooks.turnData(
      { useChat } as Parameters<typeof spec.inject.hooks.turnData>[0],
      'node-1',
    )
    const data = { get: (key: string) => key === 'metric' ? 42 : undefined }
    const turn = { data }

    snapshot = chatSnapshot({
      nodes: { get: () => ({ location: { kind: 'turn', turn } }), values: () => [] } as never,
    })
    expect(useTurnData('metric')).toBe(42)
    snapshot = chatSnapshot({
      nodes: { get: () => ({ location: { kind: 'step', turn } }), values: () => [] } as never,
    })
    expect(useTurnData('metric')).toBe(42)
    snapshot = chatSnapshot({
      nodes: { get: () => ({ location: { kind: 'session' } }), values: () => [] } as never,
    })
    expect(useTurnData('metric')).toBeUndefined()
    snapshot = chatSnapshot({
      nodes: { get: () => undefined, values: () => [] },
    })
    expect(useTurnData('metric')).toBeUndefined()

    await b.runtime.dispose()
  })
})
