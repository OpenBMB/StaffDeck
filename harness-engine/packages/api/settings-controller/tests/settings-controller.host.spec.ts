import { describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type { SettingsDescriptor } from '@deepseek-ai/dsh-settings'
import { RemoteError, remoteErrorOf, remoteMethods } from '@deepseek-ai/dsh-typert-protocol'
import SettingsController from '../src/index.ts'
import { MemorySettings } from '../../../settings/settings/tests/memory.ts'

const NS = 'ui-test'

const Profile = z.object({
  preference: z.union(['light', 'dark']).default('light'),
  apiKey: z.string().role('secret'),
})

/** A provider that reports a local document, for the `hasDocument` fact. */
class DocumentSettings extends MemorySettings {
  override get documentPath(): string | undefined {
    return '/deployment/settings.yaml'
  }
}

/** A provider whose read forgets the namespace its write just committed. */
class VanishingSettings extends MemorySettings {
  override describe(): SettingsDescriptor[] {
    return []
  }
}

/**
 * A provider whose descriptor omits the secret-slot list. `secrets` is optional
 * on the descriptor, so a foreign provider may leave it out even under
 * `redactSecrets`, and the view still has to declare an empty list.
 */
class SlotlessSettings extends MemorySettings {
  override describe(): SettingsDescriptor[] {
    return [{
      ns: NS,
      schema: Profile.toJSON(),
      value: { preference: 'light' },
      applies: 'live',
      revision: 0,
    } as unknown as SettingsDescriptor]
  }
}

/** A provider that refuses every write the way a read-only backing store would. */
class RefusingSettings extends MemorySettings {
  override mutate(): Promise<void> {
    return Promise.reject(new Error('settings are read-only in this deployment'))
  }
}

/** A provider that refuses with a bare string, the way some storage clients do. */
class LiteralRefusingSettings extends MemorySettings {
  override async mutate(): Promise<void> {
    throw 'the document is locked'
  }
}

async function boot(
  provider: typeof MemorySettings = MemorySettings,
  options: { doc?: Record<string, unknown>; base?: { preference: 'light' | 'dark' } } = {},
): Promise<{ controller: SettingsController; ctx: Context }> {
  const ctx = new Context()
  await ctx.plugin(provider, options.doc === undefined ? {} : { doc: options.doc })
  ctx.settings.register(NS, Profile, options.base === undefined ? {} : { base: options.base })
  await ctx.plugin(SettingsController)
  return { controller: ctx.settingsController, ctx }
}

describe('the settings Remote namespace a configuration page calls', () => {
  it('publishes the settings namespace from its own service key', async () => {
    const { controller } = await boot()
    expect(controller.typertRemote.serviceKey).toBe('settingsController')
    expect(controller.typertRemote.namespace).toBe('settings')
    expect(remoteMethods(controller)).toEqual([
      { method: 'describe', invocation: { kind: 'direct' } },
      { method: 'canOpenAgentPresetDirectory', invocation: { kind: 'direct' } },
      { method: 'update', invocation: { kind: 'direct' } },
      { method: 'replace', invocation: { kind: 'direct' } },
      { method: 'mutate', invocation: { kind: 'direct' } },
      { method: 'openSettingsDocument', invocation: { kind: 'direct' } },
      { method: 'openAgentPresetDirectory', invocation: { kind: 'direct' } },
    ])
  })

  it('reports the actionable configuration error while no settings provider is mounted', async () => {
    const ctx = new Context()
    await ctx.plugin(SettingsController)
    const calls: Array<() => unknown> = [
      () => ctx.settingsController.describe(),
      () => ctx.settingsController.update('ui-test', {}, undefined),
      () => ctx.settingsController.replace('ui-test', {}, undefined),
      () => ctx.settingsController.mutate('ui-test', [], undefined),
      () => ctx.settingsController.openSettingsDocument(new AbortController().signal),
    ]
    for (const call of calls) {
      const failure = await Promise.resolve().then(call).catch((error: unknown) => error)
      expect(remoteErrorOf(failure)).toMatchObject({
        code: 'gateway/internal',
        message: 'settings service is absent: this deployment does not mount a settings provider (e.g. @deepseek-ai/dsh-settings-file) in its composition',
        details: {},
      })
    }
  })

  it('mounts the credentials namespace beside its own', async () => {
    const ctx = new Context()
    await ctx.plugin(MemorySettings)
    ctx.settings.register(NS, Profile)
    const fiber = ctx.plugin(SettingsController)
    await fiber.await()
    expect(ctx.get('credentialsController')).toBeDefined()
    await fiber.dispose()
    expect(ctx.get('settingsController')).toBeUndefined()
    expect(ctx.get('credentialsController')).toBeUndefined()
  })

  it('describes every namespace redacted, with the deployment facts around them', async () => {
    const { controller } = await boot(DocumentSettings, { doc: { 'ui-test': { apiKey: 'sk-stored' } } })
    const value = controller.describe()
    expect(value).toMatchObject({ writable: true, hasDocument: true })
    const [view] = value.namespaces
    expect(view?.ns).toBe('ui-test')
    // The secret never rides; its slot reports only that one is stored.
    expect(JSON.stringify(value)).not.toContain('sk-stored')
    expect(view?.secrets).toEqual([{ path: ['apiKey'], set: true }])
    // Redaction removes the field rather than replacing it, so the layer that
    // stored a secret comes back empty instead of carrying a placeholder.
    expect(view?.user).toEqual({})
  })

  it('reports a read-only provider and omits the layers it has none of', async () => {
    const { controller } = await boot(class extends MemorySettings {
      override get writable(): boolean {
        return false
      }
    })
    const value = controller.describe()
    expect(value).toMatchObject({ writable: false, hasDocument: false })
    const [view] = value.namespaces
    // No composition base was declared and no user section is stored, so
    // neither optional layer appears at all.
    expect(view && 'base' in view).toBe(false)
    expect(view && 'user' in view).toBe(false)
  })

  it('declares an empty slot list when the provider names no secrets', async () => {
    const { controller } = await boot(SlotlessSettings)
    const [view] = controller.describe().namespaces
    expect(view?.secrets).toEqual([])
  })

  it('carries the composition base layer when the registrant declared one', async () => {
    const { controller } = await boot(MemorySettings, { base: { preference: 'dark' } })
    const [view] = controller.describe().namespaces
    expect(view?.base).toEqual({ preference: 'dark' })
  })

  it('applies path-addressed edits and answers with the namespace it just wrote', async () => {
    const { controller } = await boot()
    const view = await controller.mutate('ui-test', [{ op: 'set', path: ['preference'], value: 'dark' }], undefined)
    expect(view).toMatchObject({ ns: 'ui-test', user: { preference: 'dark' } })
    expect(view.revision).toBeGreaterThan(0)
  })

  it('supports merge updates and wholesale replacement on the Remote namespace', async () => {
    const { controller } = await boot(MemorySettings, {
      doc: { 'ui-test': { preference: 'dark', apiKey: 'sk-stored' } },
    })
    const updated = await controller.update('ui-test', { preference: 'light' }, undefined)
    expect(updated.user).toEqual({ preference: 'light' })
    expect(updated.secrets).toEqual([{ path: ['apiKey'], set: true }])

    const replaced = await controller.replace('ui-test', {}, updated.revision)
    expect(replaced.value).toEqual({ preference: 'light' })
    expect(replaced.user).toEqual({})
    expect(replaced.secrets).toEqual([{ path: ['apiKey'], set: false }])
  })

  it('refuses a stale write as settings/conflict carrying both revisions', async () => {
    const { controller } = await boot()
    const held = controller.describe().namespaces[0]!.revision
    await controller.mutate('ui-test', [{ op: 'set', path: ['preference'], value: 'dark' }], held)
    const failure = await controller
      .mutate('ui-test', [{ op: 'set', path: ['preference'], value: 'light' }], held)
      .catch((error: unknown) => error)
    const { code, details } = remoteErrorOf(failure) ?? {}
    expect(code).toBe('settings/conflict')
    expect(details).toMatchObject({ ns: 'ui-test', expected: held })
  })

  it('answers a malformed namespace exactly as an unregistered one', async () => {
    const { controller } = await boot()
    for (const ns of ['Not A Namespace', 'unregistered']) {
      const failure = await controller.mutate(ns, [{ op: 'unset', path: ['preference'] }], undefined)
        .catch((error: unknown) => error)
      expect(remoteErrorOf(failure)).toMatchObject({
        code: 'settings/rejected',
        details: { ns },
      })
    }
  })

  it('reports an empty namespace as bad-request', async () => {
    const { controller } = await boot()
    for (const call of [
      () => controller.update('', {}, undefined),
      () => controller.replace('', {}, undefined),
      () => controller.mutate('', [], undefined),
    ]) {
      const failure = await call().catch((error: unknown) => error)
      expect(remoteErrorOf(failure)).toMatchObject({ code: 'gateway/bad-request' })
    }
  })

  it('reports a refused write as settings/rejected carrying the seam message', async () => {
    const { controller } = await boot(RefusingSettings)
    const failure = await controller.mutate('ui-test', [{ op: 'unset', path: ['preference'] }], undefined)
      .catch((error: unknown) => error)
    const { code, message } = remoteErrorOf(failure) ?? {}
    expect(code).toBe('settings/rejected')
    expect(message).toContain('read-only in this deployment')
  })

  it('stringifies a refusal that is not an Error', async () => {
    const { controller } = await boot(LiteralRefusingSettings)
    const failure = await controller.mutate('ui-test', [{ op: 'unset', path: ['preference'] }], undefined)
      .catch((error: unknown) => error)
    expect(remoteErrorOf(failure)?.message).toBe('the document is locked')
  })

  it('reports a namespace disposed between the write and its read-back', async () => {
    const { controller } = await boot(VanishingSettings)
    const failure = await controller.mutate('ui-test', [{ op: 'set', path: ['preference'], value: 'dark' }], undefined)
      .catch((error: unknown) => error)
    const { code, message } = remoteErrorOf(failure) ?? {}
    expect(code).toBe('gateway/internal')
    expect(message).toContain('was disposed after the mutate')
  })

  it('prepares and opens the provider-owned settings document', async () => {
    const ctx = new Context()
    await ctx.plugin(DocumentSettings)
    const prepare = vi.spyOn(ctx.settings, 'prepareDocument').mockResolvedValue('/tmp/settings.yaml')
    const openTextFile = vi.fn((_path: string, _signal: AbortSignal) => Promise.resolve())
    const controller = new SettingsController(ctx, {}, { openTextFile })
    const signal = new AbortController().signal

    await expect(controller.openSettingsDocument(signal)).resolves.toEqual({ opened: true })
    expect(prepare).toHaveBeenCalledOnce()
    expect(openTextFile).toHaveBeenCalledWith('/tmp/settings.yaml', signal)
  })

  it('preserves settings-document absence, failure, and cancellation', async () => {
    const absent = await boot()
    const missingDocument = absent.controller.openSettingsDocument(new AbortController().signal)
    await expect(missingDocument).rejects.toMatchObject({ code: 'gateway/internal' })
    await expect(missingDocument).rejects.toThrow('no local document')

    const failed = await boot(DocumentSettings)
    vi.spyOn(failed.ctx.settings, 'prepareDocument').mockRejectedValue(new Error('read failed'))
    const failedRead = failed.controller.openSettingsDocument(new AbortController().signal)
    await expect(failedRead).rejects.toMatchObject({ code: 'gateway/internal' })
    await expect(failedRead).rejects.toThrow('read failed')

    const cancelled = new AbortController()
    cancelled.abort(new Error('cancelled'))
    const prepare = vi.spyOn(failed.ctx.settings, 'prepareDocument')
    prepare.mockClear()
    await expect(failed.controller.openSettingsDocument(cancelled.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })
    expect(prepare).not.toHaveBeenCalled()
  })

  it('does not open a settings document cancelled during preparation', async () => {
    const ctx = new Context()
    await ctx.plugin(DocumentSettings)
    const prepared = Promise.withResolvers<string | undefined>()
    vi.spyOn(ctx.settings, 'prepareDocument').mockReturnValue(prepared.promise)
    const openTextFile = vi.fn((_path: string, _signal: AbortSignal) => Promise.resolve())
    const controller = new SettingsController(ctx, {}, { openTextFile })
    const abort = new AbortController()

    const opening = controller.openSettingsDocument(abort.signal)
    abort.abort(new Error('cancelled'))
    prepared.resolve('/tmp/settings.yaml')

    await expect(opening).rejects.toMatchObject({ code: 'gateway/cancelled' })
    expect(openTextFile).not.toHaveBeenCalled()
  })

  it('maps native settings-document opener failures', async () => {
    const ctx = new Context()
    await ctx.plugin(DocumentSettings)
    vi.spyOn(ctx.settings, 'prepareDocument').mockResolvedValue('/tmp/settings.yaml')
    const controller = new SettingsController(ctx, {}, {
      openTextFile: () => Promise.reject(new Error('no default editor')),
    })

    await expect(controller.openSettingsDocument(new AbortController().signal))
      .rejects.toMatchObject({ code: 'gateway/internal', message: 'path open failed: no default editor' })
  })

  it('classifies cancellation while preparing or opening the settings document', async () => {
    const preparing = new Context()
    await preparing.plugin(DocumentSettings)
    const prepareAbort = new AbortController()
    vi.spyOn(preparing.settings, 'prepareDocument').mockImplementation(async () => {
      prepareAbort.abort(new Error('cancelled'))
      throw new Error('preparation stopped')
    })
    const preparingController = new SettingsController(preparing)
    await expect(preparingController.openSettingsDocument(prepareAbort.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })

    const opening = new Context()
    await opening.plugin(DocumentSettings)
    vi.spyOn(opening.settings, 'prepareDocument').mockResolvedValue('/tmp/settings.yaml')
    const openAbort = new AbortController()
    const openingController = new SettingsController(opening, {}, {
      openTextFile: async () => {
        openAbort.abort(new Error('cancelled'))
        throw new Error('opening stopped')
      },
    })
    await expect(openingController.openSettingsDocument(openAbort.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })
  })

  it('opens a user Agent preset directory or returns its path without a native opener', async () => {
    const ctx = new Context()
    ctx.provide('agentPresets', {
      resolve: (id: string) => Promise.resolve({
        id, trust: 'user', path: `/presets/${id}/agent.cordis.yml`,
      }),
    } as never)
    const openPath = vi.fn((_path: string, _signal: AbortSignal) => Promise.resolve())
    const openable = new SettingsController(ctx, { nativeOpen: true }, { openPath })
    expect(openable.canOpenAgentPresetDirectory()).toBe(true)
    const signal = new AbortController().signal
    await expect(openable.openAgentPresetDirectory('mine', signal))
      .resolves.toEqual({ opened: true })
    expect(openPath).toHaveBeenCalledWith('/presets/mine', signal)

    const headless = new Context()
    headless.provide('agentPresets', {
      resolve: (id: string) => Promise.resolve({
        id, trust: 'user', path: `/presets/${id}/agent.cordis.yml`,
      }),
    } as never)
    const reveal = new SettingsController(headless, { nativeOpen: false })
    expect(reveal.canOpenAgentPresetDirectory()).toBe(false)
    await expect(reveal.openAgentPresetDirectory('mine', new AbortController().signal))
      .resolves.toEqual({ opened: false, path: '/presets/mine' })
  })

  it('covers native-open detection defaults and explicit overrides', () => {
    const fromInjectedOpener = new SettingsController(new Context(), {}, {
      openPath: () => Promise.resolve(),
    })
    expect((fromInjectedOpener as unknown as { canOpenPath: () => boolean }).canOpenPath()).toBe(true)

    const detected = new SettingsController(new Context())
    expect(typeof (detected as unknown as { canOpenPath: () => boolean }).canOpenPath()).toBe('boolean')

    const override = vi.fn(() => false)
    const overridden = new SettingsController(new Context(), {}, { canOpenPath: override })
    expect((overridden as unknown as { canOpenPath: () => boolean }).canOpenPath()).toBe(false)
    expect(override).toHaveBeenCalledOnce()
  })

  it('refuses a shipped Agent preset and a missing preset provider', async () => {
    const ctx = new Context()
    ctx.provide('agentPresets', {
      resolve: (id: string) => Promise.resolve({
        id, trust: 'system', path: `/presets/${id}/agent.cordis.yml`,
      }),
    } as never)
    const controller = new SettingsController(ctx)
    await expect(controller.openAgentPresetDirectory('standard', new AbortController().signal))
      .rejects.toMatchObject({ code: 'agent-preset/read-only' })

    const missing = new SettingsController(new Context())
    await expect(missing.openAgentPresetDirectory('mine', new AbortController().signal))
      .rejects.toMatchObject({ code: 'agent-preset/not-found' })
  })

  it('rejects an empty Agent preset id before resolving a provider', async () => {
    const resolve = vi.fn()
    const ctx = new Context()
    ctx.provide('agentPresets', { resolve } as never)
    const controller = new SettingsController(ctx)

    await expect(controller.openAgentPresetDirectory('', new AbortController().signal))
      .rejects.toMatchObject({ code: 'gateway/bad-request' })
    expect(resolve).not.toHaveBeenCalled()
  })

  it('raises an Agent preset resolution failure as the roster reported it', async () => {
    const ctx = new Context()
    const reported = new RemoteError('agent-preset/not-found', 'no such preset', {
      agentPreset: 'mine', available: ['standard'],
    })
    ctx.provide('agentPresets', { resolve: async () => { throw reported } } as never)
    const controller = new SettingsController(ctx)

    await expect(controller.openAgentPresetDirectory('mine', new AbortController().signal))
      .rejects.toBe(reported)
  })

  it('classifies cancellation and non-Error failures from the preset opener', async () => {
    const ctx = new Context()
    ctx.provide('agentPresets', {
      resolve: (id: string) => Promise.resolve({
        id, trust: 'user', path: `/presets/${id}/agent.cordis.yml`,
      }),
    } as never)
    const abort = new AbortController()
    const openPath = vi.fn()
      .mockImplementationOnce(async () => {
        abort.abort(new Error('cancelled'))
        throw new Error('opening stopped')
      })
      .mockRejectedValueOnce('desktop unavailable')
    const controller = new SettingsController(ctx, { nativeOpen: true }, { openPath })

    await expect(controller.openAgentPresetDirectory('first', abort.signal))
      .rejects.toMatchObject({ code: 'gateway/cancelled' })
    await expect(controller.openAgentPresetDirectory('second', new AbortController().signal))
      .rejects.toMatchObject({ code: 'gateway/internal', message: 'path open failed: desktop unavailable' })
  })
})
