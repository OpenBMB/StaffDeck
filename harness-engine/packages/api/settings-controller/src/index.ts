/**
 * Host Remote owner for the configuration surfaces over the settings-domain
 * seams. Two namespaces: `settings`, the redacted reads and writes of
 * `ctx.settings`, owned by the class below; and `credentials`, mounted from
 * here as its own plugin.
 *
 * @module @deepseek-ai/dsh-api-settings-controller
 */

import { dirname } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
// Type-only: resolves the `agentPresets` Context augmentation this controller reads.
import type {} from '@deepseek-ai/dsh-agent-presets'
import {
  canOpenNativePath,
  openNativePath,
  openNativeTextFile,
} from '@deepseek-ai/dsh-native-command'
import type { SettingsDescriptor, SettingsPathOp, SettingsProvider } from '@deepseek-ai/dsh-settings'
import type {
  SettingsDescribeValue, SettingsNamespaceView, SettingsPathOpView,
} from '@deepseek-ai/dsh-settings/types'
import { Remote, RemoteError, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { JsonValue } from '@deepseek-ai/dsh-util-values'
import { z } from 'zod'
import { CredentialsController } from './credentials.ts'
import type { AgentPresetDirectoryOpenValue, SettingsDocumentOpenValue } from './types.ts'

export { CredentialsController } from './credentials.ts'
export type * from './types.ts'

const settingsNamespaceRequestSchema = z.object({ ns: z.string().min(1) })

/** Native document-opening policy. */
export interface Config {
  /** Override platform desktop-opener detection. */
  readonly nativeOpen?: boolean
}

/** Read abort state afresh after an awaited provider or opener call. */
function isAborted(signal: AbortSignal): boolean {
  return signal.aborted
}

/** Host integrations replaceable by direct unit tests. */
export interface SettingsControllerInternals {
  readonly openPath?: (path: string, signal: AbortSignal) => Promise<void>
  readonly openTextFile?: (path: string, signal: AbortSignal) => Promise<void>
  readonly canOpenPath?: () => boolean
}

/**
 * Project one redacted descriptor onto its wire view, field by field. The
 * Gateway returns a business result without decoding it, so a provider whose
 * descriptor carried extra enumerable properties would otherwise serialize them
 * to the caller.
 * @param descriptor - one descriptor read under `redactSecrets`.
 * @returns the same facts with nothing else attached.
 */
function namespaceView(descriptor: SettingsDescriptor): SettingsNamespaceView {
  return {
    ns: String(descriptor.ns),
    schema: descriptor.schema as JsonValue,
    value: descriptor.value as JsonValue,
    ...descriptor.base === undefined ? {} : { base: descriptor.base as JsonValue },
    ...descriptor.user === undefined ? {} : { user: descriptor.user as JsonValue },
    applies: descriptor.applies,
    secrets: (descriptor.secrets ?? []).map(secret => ({ path: [...secret.path], set: secret.set })),
    revision: descriptor.revision,
  }
}

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** Host owner of the `settings` Remote namespace. */
    settingsController: SettingsController
  }
}

/**
 * Host service backing the generated `ctx.remote.settings` namespace. Every
 * remote read uses `redactSecrets: true`, so a `role('secret')` field cannot
 * ride a response. Writes expose the settings service's merge, replacement,
 * and path-addressed operations, and classify every provider refusal as
 * `settings/conflict` or `settings/rejected` with the service's message.
 */
export class SettingsController extends TypertRemoteService {
  static Config: Schema<Config> = Schema.object({ nativeOpen: Schema.boolean() })

  private readonly openPath: (path: string, signal: AbortSignal) => Promise<void>
  private readonly openTextFile: (path: string, signal: AbortSignal) => Promise<void>
  private readonly canOpenPath: () => boolean

  /**
   * Register the settings namespace and mount the credentials namespace beside
   * it. Both namespaces stay registered when a provider is absent so calls can
   * return the configuration API's actionable missing-provider diagnostic.
   * @param ctx - Host context where settings and credential providers may be mounted.
   */
  constructor(ctx: Context, config: Config = {}, internals: SettingsControllerInternals = {}) {
    super(ctx, 'settingsController', { namespace: 'settings' })
    this.openPath = internals.openPath ?? openNativePath
    this.openTextFile = internals.openTextFile ?? openNativeTextFile
    this.canOpenPath = internals.canOpenPath
      ?? (() => config.nativeOpen ?? (internals.openPath !== undefined || canOpenNativePath()))
    ctx.plugin(CredentialsController)
  }

  /**
   * Describe every registered namespace for a configuration page: redacted
   * layered values plus the serialized schema the page renders its form from.
   * @returns provider writability, local-document presence, and one view per namespace.
   * @throws RemoteError when no settings provider is mounted.
   */
  @Remote
  describe(): SettingsDescribeValue {
    const settings = this.provider()
    return {
      writable: settings.writable,
      hasDocument: settings.documentPath !== undefined,
      namespaces: settings.describe({ redactSecrets: true }).map(namespaceView),
    }
  }

  /**
   * Report whether this deployment can open an authored Agent preset directory natively.
   * @returns true when the matching open operation is available.
   */
  @Remote
  canOpenAgentPresetDirectory(): boolean {
    return this.canOpenPath()
  }

  /**
   * Merge a patch into one namespace's stored user section.
   * @param ns - namespace key to write.
   * @param patch - fields to merge into the user section.
   * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
   * @returns the namespace's redacted view after the write.
   * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
   */
  @Remote
  update(
    ns: string,
    patch: Record<string, JsonValue>,
    expectedRevision: number | undefined,
  ): Promise<SettingsNamespaceView> {
    return this.write(ns, 'update', patch, expectedRevision)
  }

  /**
   * Replace one namespace's stored user section wholesale.
   * @param ns - namespace key to write.
   * @param section - complete replacement user section.
   * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
   * @returns the namespace's redacted view after the write.
   * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
   */
  @Remote
  replace(
    ns: string,
    section: Record<string, JsonValue>,
    expectedRevision: number | undefined,
  ): Promise<SettingsNamespaceView> {
    return this.write(ns, 'replace', section, expectedRevision)
  }

  /**
   * Apply path-addressed edits to one namespace's user section, resolved against
   * the section as stored rather than against whatever the caller last read,
   * then answer with that namespace's new redacted view.
   * @param ns - namespace key to write.
   * @param ops - the edits to apply, in order.
   * @param expectedRevision - revision the caller read; `undefined` writes unconditionally.
   * @returns the namespace's redacted view after the write.
   * @throws RemoteError when the request is invalid, no provider is mounted, or the provider refuses the write.
   */
  @Remote
  async mutate(
    ns: string,
    ops: SettingsPathOpView[],
    expectedRevision: number | undefined,
  ): Promise<SettingsNamespaceView> {
    return this.write(ns, 'mutate', ops, expectedRevision)
  }

  /**
   * Materialize the provider-owned settings document and open it in a native text editor.
   * @param signal - caller lifetime; abort terminates preparation or the native command.
   * @returns confirmation after the native opener accepts the document.
   * @throws RemoteError when no document exists, preparation fails, or opening fails.
   */
  @Remote
  async openSettingsDocument(signal: AbortSignal): Promise<SettingsDocumentOpenValue> {
    const settings = this.provider()
    if (isAborted(signal)) throw new RemoteError('gateway/cancelled', 'settings document open was aborted', {})
    let path: string | undefined
    try {
      path = await settings.prepareDocument()
    } catch (error: unknown) {
      if (isAborted(signal)) throw new RemoteError('gateway/cancelled', 'settings document preparation was aborted', {})
      throw new RemoteError('gateway/internal', `settings document preparation failed: ${messageOf(error)}`, {}, { cause: error })
    }
    if (path === undefined) {
      throw new RemoteError('gateway/internal', 'settings provider has no local document to open', {})
    }
    if (isAborted(signal)) throw new RemoteError('gateway/cancelled', 'settings document open was aborted', {})
    try {
      await this.openTextFile(path, signal)
      return { opened: true }
    } catch (error: unknown) {
      if (isAborted(signal)) throw new RemoteError('gateway/cancelled', 'settings document open was aborted', {})
      throw new RemoteError('gateway/internal', `path open failed: ${messageOf(error)}`, {}, { cause: error })
    }
  }

  /**
   * Open one user-authored Agent preset directory or return its path when no native opener exists.
   * @param agentPreset - preset id resolved against Host-owned roots.
   * @param signal - caller lifetime; abort terminates the native command.
   * @returns an opened confirmation or the resolved directory for text display.
   * @throws RemoteError when the preset is missing, read-only, invalid, or cannot be opened.
   */
  @Remote
  async openAgentPresetDirectory(
    agentPreset: string,
    signal: AbortSignal,
  ): Promise<AgentPresetDirectoryOpenValue> {
    if (agentPreset.length === 0) {
      throw new RemoteError('gateway/bad-request', 'agent preset id must not be empty', {})
    }
    const presets = this.ctx.get('agentPresets')
    if (presets === undefined) {
      throw new RemoteError(
        'agent-preset/not-found',
        'this deployment composes no agent presets',
        { agentPreset, available: [] },
      )
    }
    const preset = await presets.resolve(agentPreset)
    if (preset.trust !== 'user') {
      throw new RemoteError(
        'agent-preset/read-only',
        `agent-presets: preset "${preset.id}" cannot be written: it ships with the deployment`,
        { agentPreset: preset.id, reason: 'it ships with the deployment' },
      )
    }
    const directory = dirname(preset.path)
    if (!this.canOpenPath()) return { opened: false, path: directory }
    try {
      await this.openPath(directory, signal)
      return { opened: true }
    } catch (error: unknown) {
      if (signal.aborted) throw new RemoteError('gateway/cancelled', 'path open was aborted', {})
      throw new RemoteError('gateway/internal', `path open failed: ${messageOf(error)}`, {}, { cause: error })
    }
  }

  private async write(
    ns: string,
    mode: 'update' | 'replace' | 'mutate',
    input: Record<string, JsonValue> | SettingsPathOpView[],
    expectedRevision: number | undefined,
  ): Promise<SettingsNamespaceView> {
    const parsed = settingsNamespaceRequestSchema.safeParse({ ns })
    if (!parsed.success) {
      throw new RemoteError('gateway/bad-request', `invalid payload for settings.${mode}`, { issues: parsed.error.issues })
    }
    const settings = this.provider()
    const namespace = parsed.data.ns
    try {
      if (mode === 'update') await settings.update(namespace, input, expectedRevision)
      else if (mode === 'replace') await settings.replace(namespace, input, expectedRevision)
      else await settings.mutate(namespace, input as SettingsPathOp[], expectedRevision)
    } catch (error: unknown) {
      throw rejected(ns, error)
    }
    const descriptor = settings.describe({ redactSecrets: true }).find(candidate => candidate.ns === namespace)
    if (descriptor === undefined) {
      // The write committed but the namespace vanished before this read: only a
      // concurrent registrant disposal can produce it.
      throw new RemoteError('gateway/internal', `settings namespace "${ns}" was disposed after the ${mode}`, {})
    }
    return namespaceView(descriptor)
  }

  /** Resolve the optional provider or report how to supply it. */
  private provider(): SettingsProvider {
    const settings = this.ctx.get('settings')
    if (settings === undefined) {
      throw new RemoteError(
        'gateway/internal',
        'settings service is absent: this deployment does not mount a settings provider (e.g. @deepseek-ai/dsh-settings-file) in its composition',
        {},
      )
    }
    return settings
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

interface SettingsConflict {
  readonly code: 'SETTINGS_CONFLICT'
  readonly message: string
  readonly expected: number
  readonly actual: number
}

function settingsConflictOf(error: unknown): SettingsConflict | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  if (Reflect.get(error, 'code') !== 'SETTINGS_CONFLICT'
    || typeof Reflect.get(error, 'message') !== 'string'
    || typeof Reflect.get(error, 'expected') !== 'number'
    || typeof Reflect.get(error, 'actual') !== 'number') return undefined
  return error as SettingsConflict
}

/**
 * Classify one seam refusal. A stale writer is its own outcome, not a malformed
 * request: the client must re-read and re-apply rather than treat the write as
 * invalid.
 * @param ns - the namespace the write addressed.
 * @param error - whatever the seam threw.
 * @returns the failure to raise for that refusal.
 */
function rejected(ns: string, error: unknown): RemoteError {
  const conflict = settingsConflictOf(error)
  if (conflict !== undefined) {
    return new RemoteError(
      'settings/conflict',
      conflict.message,
      { ns, expected: conflict.expected, actual: conflict.actual },
      { cause: error },
    )
  }
  return new RemoteError('settings/rejected', messageOf(error), { ns }, { cause: error })
}

export default SettingsController
