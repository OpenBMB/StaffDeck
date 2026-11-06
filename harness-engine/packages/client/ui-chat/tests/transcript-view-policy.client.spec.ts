// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { stubSettingsScope } from '@deepseek-ai/dsh-client-test-runtime'
import type { ChatSettings } from '../src/chat-settings.ts'
import { TranscriptViewPolicy } from '../src/client/transcript-view.ts'

describe('TranscriptViewPolicy', () => {
  it('defaults to Compact and publishes explicit choices before persistence settles', () => {
    const host = stubSettingsScope<ChatSettings>()
    const observed: string[] = []
    let current = (): string => 'unconstructed'
    const scope: typeof host.scope = {
      ...host.scope,
      set: (field, value) => {
        observed.push(`${field}=${String(value)}:${current()}`)
        return host.scope.set(field, value)
      },
    }
    const policy = new TranscriptViewPolicy(scope)
    current = () => policy.mode.getSnapshot()

    expect(policy.mode.getSnapshot()).toBe('compact')
    policy.setMode('normal')
    expect(policy.mode.getSnapshot()).toBe('normal')
    expect(observed).toEqual(['transcriptView=normal:normal'])
    expect(host.set).toHaveBeenCalledWith('transcriptView', 'normal')
  })

  it('adopts Host state and ignores identical writes', () => {
    const host = stubSettingsScope<ChatSettings>()
    const policy = new TranscriptViewPolicy(host.scope)

    host.publish({ status: 'ready', value: { transcriptView: 'normal' }, revision: 1, writable: true })
    expect(policy.mode.getSnapshot()).toBe('normal')
    policy.setMode('normal')
    expect(host.set).not.toHaveBeenCalled()

    host.publish({ value: { transcriptView: 'compact' }, revision: 2 })
    expect(policy.mode.getSnapshot()).toBe('compact')
  })

  it('adopts an accepted section standing at construction', () => {
    const host = stubSettingsScope<ChatSettings>()
    host.publish({ status: 'ready', value: { transcriptView: 'normal' }, revision: 1, writable: true })
    expect(new TranscriptViewPolicy(host.scope).mode.getSnapshot()).toBe('normal')
  })
})
