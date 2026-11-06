/**
 * Watch-build for the web dev loop: rebuilds every artifact the browser reads
 * from a source edit. Reload signaling is not this script's business — the host
 * webserver stat-polls the bundles it serves and broadcasts `rebuilt` frames
 * itself (`dsh web`), so any process that rewrites `lib/client.js` files
 * triggers reloads; this script is merely the convenient way to keep them all
 * rebuilt on source change.
 *
 * Three stages, because the compile shell links built lib products rather than
 * sources: `tsc -b tsconfig.client.json` emits `lib/types` (the tsdown lib
 * entries are that emit, not `src`), tsdown bundles `lib/index.js` and
 * `lib/client.js`, and `vite build` rewrites `apps/web/dist`, which `dsh web`
 * serves. A missing stage does not fail — it silently shows the previous
 * artifact, so an edit appears to do nothing.
 *
 * MUST NOT run concurrently with `pnpm run build`: both write the same
 * `lib/` and `apps/web/dist/` trees.
 *
 * Usage: `pnpm exec tsx scripts/dev-web.ts [--poll[=ms]]`. Requires one prior
 * `pnpm run build`: every stage is incremental over the previous stage's output
 * and none of them bootstraps a missing tree. `--poll` switches the source
 * watchers to polling (default 500ms): network mounts (weka) deliver no inotify
 * events, so native watching sees the initial build only and never a source
 * change. Polling has to reach tsc too — a native-watching tsc never re-emits
 * `lib/types`, which strands the other two stages on stale input.
 *
 * Each package keeps its own tsdown.config.ts untouched: this script layers
 * `watch` through API-level inline config (tsdown workspace mode fills inline
 * keys under each package's file config, and no package config defines it).
 */
import { globSync, readFileSync } from 'node:fs'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execa } from 'execa'
import { build } from 'tsdown'
import type { TsdownBundle } from 'tsdown'
import {
  CLIENT_BUILD_PROFILE_SELECTOR,
  clientBuildProcessEnvironment,
  repositoryClientBuildEnvironment,
} from './client-build-environment.ts'

const repoRoot = fileURLToPath(new URL('..', import.meta.url))

/** Client-face type emit feeding every tsdown lib entry in the watch set. */
const CLIENT_TYPE_PROGRAM = 'tsconfig.client.json'

/** Compile-shell workspace whose dist `dsh web` serves. */
const SHELL_PACKAGE = '@deepseek-ai/dsh-web-frontend'

/**
 * Test infrastructure builds through the client preset but never enters the
 * shell's module graph, so it is not a dev-loop artifact.
 */
const TEST_INFRASTRUCTURE_PREFIX = 'packages/test-support/'

/**
 * Sample one local public environment for every long-lived watcher stage.
 * @param root - repository root supplying version and Git metadata.
 * @param environment - watcher launch environment supplying public extensions.
 * @returns process environment shared by tsdown and spawned watcher stages.
 */
export function devWebBuildEnvironment(
  root: string,
  environment: NodeJS.ProcessEnv = process.env,
): NodeJS.ProcessEnv {
  return clientBuildProcessEnvironment(environment, repositoryClientBuildEnvironment(root, environment))
}

/**
 * Discover the watch workspace by declaration: every packages/<group>/<name>
 * whose package.json carries `dsh.client` with platform "web" is a client
 * plugin bundle emitter. Scanned once at startup — a package added while
 * watching means restarting this script.
 * @param root - repository root containing the grouped package directories.
 * @returns workspace-relative plugin package directories.
 */
export function discoverPluginDirs(root = repoRoot): string[] {
  const dirs: string[] = []
  for (const manifestPath of globSync('packages/*/*/package.json', { cwd: root }).sort()) {
    const manifest = JSON.parse(readFileSync(join(root, manifestPath), 'utf8')) as {
      dsh?: { client?: { platform?: unknown } }
    }
    if (manifest.dsh?.client?.platform === 'web') dirs.push(dirname(manifestPath).split(sep).join('/'))
  }
  return dirs
}

/**
 * Discover the statically linked library packages: the other half of the same
 * partition {@link discoverPluginDirs} takes. A package that builds through the
 * client preset without declaring `dsh.client` has no loader-delivered browser
 * half, so the compile shell links its `lib/index.js` instead — and an edit to
 * its source reaches the browser only once that bundle is rewritten. Deriving
 * the set from the build preset rather than a hand list keeps it correct when
 * dependency sections move around; deriving it from `dependencies` would not,
 * because client packages declare their build inputs as devDependencies.
 * @param root - repository root containing the grouped package directories.
 * @returns workspace-relative library package directories.
 */
export function discoverLibraryDirs(root = repoRoot): string[] {
  const dirs: string[] = []
  for (const configPath of globSync('packages/*/*/tsdown.config.ts', { cwd: root }).sort()) {
    const dir = dirname(configPath).split(sep).join('/')
    if (dir.startsWith(TEST_INFRASTRUCTURE_PREFIX)) continue
    if (!readFileSync(join(root, configPath), 'utf8').includes('tsdown.client.ts')) continue
    const manifest = JSON.parse(readFileSync(join(root, dir, 'package.json'), 'utf8')) as {
      dsh?: { client?: unknown }
    }
    if (manifest.dsh?.client === undefined) dirs.push(dir)
  }
  return dirs
}

/**
 * Start the tsdown watch build used by `pnpm run dev:web`.
 * @param root - repository or fixture root passed to tsdown.
 * @param pluginDirs - workspace-relative package directories to watch.
 * @param pollInterval - optional source-watcher polling interval in milliseconds.
 * @returns live bundles after every watcher has completed its initial build.
 */
export async function watchClientPlugins(
  root: string,
  pluginDirs: readonly string[],
  pollInterval?: number,
): Promise<TsdownBundle[]> {
  let resolveInitialBuilds: (() => void) | undefined
  const initialBuilds = new Promise<void>((resolve) => { resolveInitialBuilds = resolve })
  const initialized = new WeakSet<object>()
  const readiness: { expectedBuilds?: number; initializedBuilds: number } = { initializedBuilds: 0 }
  const bundles = await build({
    cwd: root,
    workspace: [...pluginDirs],
    watch: true,
    hooks: {
      'build:done': ({ options }) => {
        if (initialized.has(options)) return
        initialized.add(options)
        readiness.initializedBuilds += 1
        if (
          readiness.expectedBuilds !== undefined
          && readiness.initializedBuilds >= readiness.expectedBuilds
        ) resolveInitialBuilds?.()
      },
    },
    ...pollInterval !== undefined
      ? { inputOptions: { watch: { watcher: { usePolling: true, pollInterval } } } }
      : {},
  })
  readiness.expectedBuilds = bundles.length
  if (readiness.initializedBuilds >= readiness.expectedBuilds) resolveInitialBuilds?.()
  await initialBuilds
  return bundles
}

/**
 * Live watcher processes to terminate when this script is interrupted. Stages
 * register themselves as they start, so the set is complete from the first
 * spawn: an interrupt during a later stage's startup still tears down the
 * earlier ones instead of orphaning them.
 */
const stages: StageHandle[] = []

/**
 * Spawn one watcher stage, inheriting stdio, registering it for teardown, and
 * failing loud if it ever exits: a dead stage leaves the artifact chain silently
 * stale, which reads as "my edit did nothing" — the one failure this script
 * exists to prevent.
 * @param stage - command label used in the exit diagnostic.
 * @param command - executable, resolved from the workspace bin when local.
 * @param args - command arguments.
 * @param local - whether to resolve `command` from the workspace's installed bins.
 */
function spawnStage(stage: string, command: string, args: readonly string[], local: boolean): void {
  const child = execa(command, [...args], {
    cwd: repoRoot,
    stdio: 'inherit',
    preferLocal: local,
    reject: false,
  })
  stages.push({ kill: () => { child.kill() } })
  void child.then((result) => {
    console.error(`dev-web: ${stage} exited (code ${String(result.exitCode)}); the artifact chain is now stale`)
    process.exit(1)
  })
}

/** The only capability this script needs from a live watcher process. */
interface StageHandle {
  readonly kill: () => void
}

const invokedPath = process.argv[1]
const isMain = invokedPath !== undefined && import.meta.url === pathToFileURL(resolve(invokedPath)).href
if (isMain) {
  const buildEnvironment = devWebBuildEnvironment(repoRoot, process.env)
  for (const name of Object.keys(process.env)) {
    if (name === CLIENT_BUILD_PROFILE_SELECTOR || name.startsWith('DSH_CLIENT_')) {
      Reflect.deleteProperty(process.env, name)
    }
  }
  for (const [name, value] of Object.entries(buildEnvironment)) {
    if (name.startsWith('DSH_CLIENT_') && value !== undefined) process.env[name] = value
  }

  const pluginDirs = discoverPluginDirs()
  const libraryDirs = discoverLibraryDirs()
  if (pluginDirs.length === 0) {
    console.error('dev-web: no dsh.client (platform "web") packages found under packages/')
    process.exit(1)
  }
  if (libraryDirs.length === 0) {
    console.error('dev-web: no client-preset library packages found under packages/ — the compile shell links their lib products, so an empty set means the discovery predicate is stale')
    process.exit(1)
  }

  const args = process.argv.slice(2)
  const pollArg = args.find(a => a === '--poll' || a.startsWith('--poll='))
  if (args.some(a => a !== pollArg)) {
    console.error('dev-web: usage: tsx scripts/dev-web.ts [--poll[=ms]]')
    process.exit(1)
  }
  const pollInterval = pollArg === undefined ? undefined : Number(pollArg.split('=')[1] ?? '500')
  if (pollInterval !== undefined && (!Number.isInteger(pollInterval) || pollInterval <= 0)) {
    console.error(`dev-web: invalid --poll interval "${pollArg ?? ''}"`)
    process.exit(1)
  }

  // Registered before any stage starts: `stages` is read at signal time, so an
  // interrupt during tsdown's initial builds still kills whatever is running.
  const stop = (): void => { for (const stage of stages) stage.kill() }
  process.once('SIGINT', stop)
  process.once('SIGTERM', stop)

  // tsc has no polling interval flag, so `--poll` selects its fixed-interval
  // watchers rather than an interval. Dropping that translation leaves tsc
  // natively watching on a network mount where inotify never fires: it stops
  // re-emitting lib/types, and the two later stages then rebuild forever from
  // stale input without printing anything.
  spawnStage(`tsc -b ${CLIENT_TYPE_PROGRAM} --watch`, 'tsc', [
    '-b', CLIENT_TYPE_PROGRAM, '--watch', '--preserveWatchOutput',
    ...pollInterval !== undefined
      ? ['--watchFile', 'fixedPollingInterval', '--watchDirectory', 'fixedPollingInterval']
      : [],
  ], true)

  // tsdown's initial builds are awaited before the dist watcher starts so vite's
  // first build reads current lib bundles rather than whatever the last full
  // build left. Its own watch then covers later lib rewrites — those files are
  // in its module graph.
  await watchClientPlugins(repoRoot, [...pluginDirs, ...libraryDirs], pollInterval)
  // Through the shell's own `watch` script rather than vite's API: vite is not a
  // repository-root dependency, and more importantly the vite root is its
  // working directory — `resolve.dedupe` resolves react from that root, so
  // running vite from anywhere but apps/web silently switches which react copy
  // the bundle gets.
  spawnStage('vite build --watch', 'pnpm', ['--filter', SHELL_PACKAGE, 'run', 'watch'], false)

  console.log(
    `dev-web: watching ${String(pluginDirs.length)} dsh.client plugin packages`
    + ` and ${String(libraryDirs.length)} statically linked library packages`
    + (pollInterval !== undefined ? ` (polling ${String(pollInterval)}ms)` : '')
    + `, plus tsc -b ${CLIENT_TYPE_PROGRAM} and the ${SHELL_PACKAGE} dist build:\n  `
    + [...pluginDirs, ...libraryDirs].join('\n  '),
  )
}
