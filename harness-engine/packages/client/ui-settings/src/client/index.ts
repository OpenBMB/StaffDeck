/**
 * Settings domain base plugin, browser half. Provides `ctx.settingsScope`, the
 * settings-namespace scope service every preference row binds its durable
 * section through, and owns the one `settings.describe` reader in the browser:
 * the describe mirror, whose invalidation subscriptions
 * (`settings/document-updated`, `connection/reset`) live here so every derived
 * surface refreshes from a single wire read. It depends on no `ui-*`
 * presentation package, so any feature that owns a preference can reach it:
 * the settings SHELL — the `sidebar.settings` occupant, its navigation, and
 * the chrome — lives in ui-settings-general, because a shell dependency on
 * ui-sidebar would close a reference cycle through ui-layout and ui-theme.
 * Export discipline: packages/client/AGENTS.md.
 */
import type { Context } from '@deepseek-ai/cordis'
// Type-only: the ctx.remote merge, the fixed Host facts, and the carrier's
// `connection/reset` lifecycle event, all through the assembly package.
import type {} from '@deepseek-ai/dsh-api-remotes/client'
// Type-only pair supplying `$on` and its key face without dragging a build
// artifact into the Host graph (rationale beside the same pair in
// settings-scope.ts).
import type {} from '@deepseek-ai/dsh-api-remotes/types'
import type {} from '@deepseek-ai/dsh-settings/types'
import { SettingsSchemaService } from './schema.ts'
import { SettingsScopeBinder } from './settings-scope.ts'
import { SettingsDescribeMirror } from './settings-mirror.ts'

export type {
  SettingsGeneralItemOwnerProps, SettingsHeaderOwnerProps, SettingsOnboardingOwnerProps,
  SettingsPluginsTabOwnerProps, SettingsSectionOwnerProps, SettingsTriggerOwnerProps,
} from './contract/slots.ts'
export type { SettingsScopeController, SettingsScopeBinder } from './settings-scope.ts'
export type { SettingsScope, SettingsScopeSnapshot, SettingsScopeSpec } from './settings-contract.ts'
export type { SettingsSchemaService } from './schema.ts'
export type { SchemaNode } from './schema.ts'
export type {
  SettingsDescribeFace, SettingsDescribeView, SettingsMirrorSnapshot,
} from './settings-mirror.ts'

/**
 * Required services: the Remote namespace the mirror reads through and the
 * forwarded settings invalidation it refreshes on.
 */
export const inject = ['remote', 'remote.settings']

/**
 * Provide the settings-namespace scope service over one shared describe
 * mirror, and keep that mirror fresh on the two signals that can move the
 * settings document: a document commit and a (re)connect.
 *
 * Constructing the service in this plugin's fiber keeps its traced methods
 * bound to each consuming plugin's context.
 * @param ctx - client root context.
 */
export function apply(ctx: Context): void {
  const schema = new SettingsSchemaService(ctx)
  // Resolved once here, where `remote` is declared in this plugin's own
  // `inject`; the binder hands the same answer to every scope it binds.
  const persistence = ctx.remote.$host.isLoopback ? 'host' : 'memory'
  const mirror = new SettingsDescribeMirror(ctx, persistence)
  ctx.effect(() => {
    const disposers = [
      ctx.remote.$on('settings/document-updated', () => { void mirror.load() }),
      ctx.on('connection/reset', () => { void mirror.load() }),
    ]
    // The first connection also emits connection/reset, so startup normally
    // costs two reads (budgeted in startup-rpc-budget.e2e.ts). The in-flight
    // fold does not merge them into one; it guarantees at most one pending
    // read at a time and that no invalidation arriving mid-read is lost.
    void mirror.ensure()
    return () => { for (const dispose of disposers) dispose() }
  }, 'ui-settings: describe mirror invalidations')
  new SettingsScopeBinder(ctx, { mirror, schema, persistence })
}
