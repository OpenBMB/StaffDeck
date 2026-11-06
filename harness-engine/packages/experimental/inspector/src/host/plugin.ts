/** Host Cordis plugin for the cross-realm Inspector Worker and full fetch capture. */

import type { Context } from '@deepseek-ai/cordis'
import type { IndexInjection } from '@deepseek-ai/dsh-host-webserver'
import { resolveInspectorOptions, startInspector, type InspectorOptions } from './bridge/controller.ts'
import { createInspectorService } from '../shared/service.ts'
import { publishCordisTree } from './inspection/cordis.ts'

export { resolveInspectorOptions, startInspector } from './bridge/controller.ts'
export type { InspectorEndpoint, InspectorHandle, InspectorOptions, InspectorSpec } from './bridge/controller.ts'
export type { CordisRuntimeTreeReader } from '../shared/cordis/reader.ts'
export type {
  CordisRuntimeConnection,
  CordisRuntimeContext,
  CordisRuntimeFiber,
  CordisRuntimeNode,
  CordisRuntimeRealm,
  CordisRuntimeSource,
  CordisRuntimeTree,
} from '../shared/cordis/model.ts'
export type { InspectorClientBootstrap } from '../shared/bridge/messages/control.ts'
export type { InspectorRecordInput, InspectorSourceDescriptor, InspectorSourceKind } from '../shared/bridge/messages/observation.ts'
export type { InspectorJsonObject, InspectorJsonPrimitive, InspectorJsonValue } from '../shared/json.ts'
export type {
  CordisContextTreeNode,
  CordisFiberTreeNode,
  CordisTreeNode,
  CordisTreeSnapshot,
} from '../shared/cordis/snapshot.ts'

/** Configuration consumed by the Host implementation after package-entry validation. */
export interface HostPluginConfig extends Omit<InspectorOptions, 'clientOrigins'> {
  /** Browser origins allowed to open the Client ingest WebSocket. */
  clientOrigins?: string[]
}

/** Start the Worker, expose `ctx.inspector`, and inject the matching Client bootstrap. */
export async function apply(ctx: Context, config: HostPluginConfig): Promise<void> {
  await ctx.effect(async () => {
    const spec = resolveInspectorOptions(config)
    const handle = await startInspector(spec)
    const disposers: Array<() => unknown> = []
    try {
      disposers.push(publishCordisTree(ctx, handle.source, {
        maxNodes: spec.maxCordisNodes,
        maxBytes: spec.maxSourceFrameBytes - 4_096,
      }))
      disposers.push(ctx.provide('inspector', createInspectorService(handle.source)))
      disposers.push(ctx.on('webserver/index-inject', (table: IndexInjection[]) => {
        table.push({ kind: 'global', name: '__DSH_INSPECTOR__', value: handle.endpoint.client })
      }))
      // This readiness URL is emitted while the plugin tree is still loading, before a logger sink is guaranteed.
      console.log(`dsh inspector: ${handle.endpoint.devtoolsFrontendUrl}`)
    } catch (error) {
      await disposeInspector(handle, disposers).catch((cleanupError: unknown) => {
        ctx.logger.error('experimental-inspector: initialization rollback failed', cleanupError)
      })
      throw error
    }
    return async () => { await disposeInspector(handle, disposers) }
  }, 'experimental-inspector: Host Worker')
}

async function disposeInspector(
  handle: Awaited<ReturnType<typeof startInspector>>,
  disposers: readonly (() => unknown)[],
): Promise<void> {
  const failures: unknown[] = []
  for (const dispose of [...disposers].reverse()) {
    try {
      await dispose()
    } catch (error) {
      failures.push(error)
    }
  }
  try {
    await handle.close()
  } catch (error) {
    failures.push(error)
  }
  if (failures.length > 0) throw new AggregateError(failures, 'experimental-inspector: disposal failed')
}
