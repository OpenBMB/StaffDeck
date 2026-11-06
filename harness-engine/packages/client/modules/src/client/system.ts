/**
 * ClientModuleSystem — the implementation behind the {@link ClientModuleLoader}
 * contract. The conceptual contract (lazy CJS model, resolution branch order) is
 * documented on the public interfaces in `./manifest.ts`; this file owns the
 * state tables and the load/materialize machinery.
 */
import { stripClientSuffix } from './manifest.ts'
import type {
  BootManifest, BootModuleRow, ClientBundleRegistration, ClientModuleLoader, ClientModuleRecord,
  ClientModuleSystemOptions,
} from './manifest.ts'

/** Default bundle-load hook: same-origin external classic script. */
const defaultLoadBundle = (url: string): Promise<void> => new Promise((resolve, reject) => {
  const el = document.createElement('script')
  el.async = true
  el.src = url
  el.addEventListener('load', () => {
    el.remove()
    resolve()
  }, { once: true })
  el.addEventListener('error', () => {
    el.remove()
    reject(new Error(`client-modules: bundle script ${url} failed to load`))
  }, { once: true })
  document.head.append(el)
})

/** Replace the rev query while preserving absolute, protocol-relative, or path-relative form. */
function atRevision(url: string, rev: string): string {
  if (!/[?&]rev=[^&#]*/.test(url)) {
    throw new Error(`client-modules: bundle URL ${url} has no revision`)
  }
  return url.replace(/([?&]rev=)[^&#]*/, `$1${encodeURIComponent(rev)}`)
}

/**
 * Claim and inventory the <style> tags a factory injected during
 * materialization: preset-emitted tags arrive pre-tagged with data-plugin;
 * any untagged tag is claimed for the materializing plugin (HMR bookkeeping).
 */
const claimStyles = (id: string): string[] => {
  if (typeof document === 'undefined') return []
  for (const el of document.querySelectorAll('style:not([data-plugin])')) {
    el.setAttribute('data-plugin', id)
  }
  const owned: string[] = []
  for (const el of document.querySelectorAll(`style[data-plugin=${JSON.stringify(id)}]`)) {
    owned.push(el.getAttribute('data-plugin-css') ?? id)
  }
  return owned
}

/**
 * The client module system: state tables plus the arrival/materialization
 * machinery implementing {@link ClientModuleLoader} (whose members carry the
 * contract documentation). Construction indexes the boot rows, retains the
 * already-materialized bootstrap module, and switches the HTML-installed
 * loader facade from its pending queue to live registration.
 */
export class ClientModuleSystem implements ClientModuleLoader {
  readonly version = 'client'
  readonly manifest: BootManifest
  readonly loadCache = new Map<string, ClientModuleRecord>()

  private readonly seed: Map<string, unknown>
  private readonly factories = new Map<string, ClientBundleRegistration['factory']>()
  private readonly bootstrapIds = new Set<string>()
  /** In-flight script transport per URL; every row in one batch shares it. */
  private readonly pendingArrival = new Map<string, Promise<void>>()
  /** Single-resource combo URL selected by HMR after invalidating one row. */
  private readonly reloadUrls = new Map<string, string>()
  /** Materialization re-entrancy guard: factory-form CJS cannot deliver partial exports, so a cycle is fatal. */
  private readonly materializing = new Set<string>()
  private readonly graphRows = new Map<string, BootModuleRow>()
  private readonly loadBundle: (url: string) => Promise<void>

  /**
   * Build the module system over the parsed boot rows.
   * @param options - Parsed graph, platform seed, bootstrap module, registration facade, and transport.
   */
  constructor(options: ClientModuleSystemOptions) {
    this.manifest = options.manifest
    this.seed = new Map(Object.entries(options.staticModules))
    this.loadBundle = options.loadBundle ?? defaultLoadBundle

    for (const row of options.manifest.modules) {
      if (this.graphRows.has(row.id)) throw new Error(`client-modules: duplicate graph entry "${row.id}"`)
      this.graphRows.set(row.id, row)
    }

    const bootstrapId = stripClientSuffix(options.bootstrapModule.id)
    this.bootstrapIds.add(bootstrapId)
    this.loadCache.set(bootstrapId, {
      id: bootstrapId,
      exports: options.bootstrapModule.exports,
      styles: [],
      edges: new Set(),
    })

    const target = options.registrationTarget
    if (target.mode !== 'queue') {
      throw new Error('client-modules: window.__ModuleLoader__.create called after module-system boot')
    }
    const pending = target.pendingQueue.splice(0)
    // Switch first: a bundle that executes while pending registrations drain
    // must register live rather than append behind the drain.
    target.mode = 'live'
    target.load = (registration) => { this.register(registration) }
    for (const registration of pending) target.load(registration)
  }

  /** Register one bundle factory, rejecting a script that executes twice without invalidation. */
  private register(registration: ClientBundleRegistration): void {
    const id = stripClientSuffix(registration.id)
    if (this.bootstrapIds.has(id) || this.factories.has(id)) {
      throw new Error(`client-modules: duplicate factory registration for "${registration.id}" (bundle executed twice without invalidate?)`)
    }
    this.factories.set(id, registration.factory)
  }

  /** Load one graph row so its factory is registered (idempotent per in-flight arrival). */
  private arrive(row: BootModuleRow): Promise<void> {
    const { id } = row
    if (this.loadCache.has(id) || this.factories.has(id)) return Promise.resolve()
    const reloadUrl = this.reloadUrls.get(id)
    const url = reloadUrl ?? row.initialUrl
    let transport = this.pendingArrival.get(url)
    if (transport === undefined) {
      transport = this.loadBundle(url).finally(() => { this.pendingArrival.delete(url) })
      this.pendingArrival.set(url, transport)
    }
    return transport.then(() => {
      if (!this.factories.has(id)) {
        throw new Error(`client-modules: bundle ${url} loaded without registering "${id}" via __ModuleLoader__.load`)
      }
      if (reloadUrl !== undefined && this.reloadUrls.get(id) === reloadUrl) {
        this.reloadUrls.delete(id)
      }
    })
  }

  /** Register each injected package and unresolved dynamic request before its consumer. */
  private async arriveGraphRow(
    row: BootModuleRow,
    open: readonly string[] = [],
    visited = new Set<string>(),
  ): Promise<void> {
    const cycleStart = open.indexOf(row.id)
    if (cycleStart !== -1) {
      throw new Error(
        `client-modules: module arrival cycle ${[...open.slice(cycleStart), row.id].join(' -> ')} `
        + '(the host must reject this graph before serving it)',
      )
    }
    if (visited.has(row.id)) return
    visited.add(row.id)
    const next = [...open, row.id]
    for (const request of row.external) {
      const id = stripClientSuffix(request)
      if (this.seed.has(request) || this.loadCache.has(id)) continue
      const dependency = this.graphRows.get(id)
      if (dependency !== undefined) await this.arriveGraphRow(dependency, next, visited)
    }
    for (const packageName of row.inject) {
      const dependency = this.graphRows.get(packageName)
      if (dependency !== undefined) await this.arriveGraphRow(dependency, [], visited)
    }
    await this.arrive(row)
  }

  /** Materialize a registered factory (synchronous; memoized in loadCache). */
  private materialize(id: string): ClientModuleRecord {
    const existing = this.loadCache.get(id)
    if (existing !== undefined) return existing
    const registered = this.factories.get(id)
    /* v8 ignore next -- callers check the factory branch before dispatching here. */
    if (registered === undefined) throw new Error(`client-modules: no registered factory for "${id}"`)
    if (this.materializing.has(id)) {
      throw new Error(`client-modules: require cycle through "${id}" (factory-form CJS cannot deliver partial exports)`)
    }
    this.materializing.add(id)
    try {
      const edges = new Set<string>()
      const exports = registered(this.makeRequire(edges))
      const record: ClientModuleRecord = { id, exports, styles: claimStyles(id), edges }
      this.loadCache.set(id, record)
      return record
    } finally {
      this.materializing.delete(id)
    }
  }

  /**
   * The synchronous require answered to factories: seed → memoized record →
   * registered factory. Fetching is async and therefore unreachable
   * from here; an external dynamic package must have arrived before its
   * consumer materializes.
   */
  private makeRequire(edges: Set<string>): (spec: string) => unknown {
    return (spec: string): unknown => {
      edges.add(spec)
      if (this.seed.has(spec)) return this.seed.get(spec)
      const id = stripClientSuffix(spec)
      const record = this.loadCache.get(id)
      if (record !== undefined) return record.exports
      if (this.factories.has(id)) return this.materialize(id).exports
      throw new Error(
        `client-modules: require("${spec}") missed the module table — not a platform seed word, not a materialized module, `
        + 'and no registered package factory (a build-time externals drift, or a dynamic dependency that did not arrive)',
      )
    }
  }

  async import(specifier: string): Promise<unknown> {
    if (this.seed.has(specifier)) return this.seed.get(specifier)
    const id = stripClientSuffix(specifier)
    const existing = this.loadCache.get(id)
    if (existing !== undefined) return existing.exports
    const row = this.graphRows.get(id)
    if (row !== undefined) {
      await this.arriveGraphRow(row)
    } else if (!this.factories.has(id)) {
      throw new Error(
        `client-modules: cannot resolve "${specifier}" — not a seed word, not a materialized module, `
        + 'and not a row in the boot graph (the runtime mirror of the bundle purity gate)',
      )
    }
    return this.materialize(id).exports
  }

  async prefetch(id: string): Promise<void> {
    const normalized = stripClientSuffix(id)
    if (this.loadCache.has(normalized)) return
    const row = this.graphRows.get(normalized)
    if (row === undefined) throw new Error(`client-modules: prefetch("${id}") — not a graph entry`)
    await this.arriveGraphRow(row)
  }

  invalidate(id: string, rev?: string): void {
    const normalized = stripClientSuffix(id)
    if (this.bootstrapIds.has(normalized)) return
    const row = this.graphRows.get(normalized)
    if (row !== undefined) this.reloadUrls.set(normalized, atRevision(row.url, rev ?? row.rev))
    else this.reloadUrls.delete(normalized)
    this.factories.delete(normalized)
    this.loadCache.delete(normalized)
  }
}
