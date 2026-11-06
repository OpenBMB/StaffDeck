import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { describe, expect, it } from 'vitest'

const repoRoot = fileURLToPath(new URL('../../../../', import.meta.url))
const builtBundle = fileURLToPath(new URL('../lib/index.js', import.meta.url))
const execFileAsync = promisify(execFile)

const probe = String.raw`
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const load = path => import(pathToFileURL(resolve(path)).href);
const [{ Context }, { default: SessionStore }, { default: Sqlite }] = await Promise.all([
  load('vendor/cordis/lib/index.js'),
  load('packages/core/session/lib/index.js'),
  load('packages/session/session-persistence-sqlite/lib/index.js'),
]);
const ctx = new Context();
await ctx.plugin(SessionStore);
await ctx.plugin(Sqlite, { path: ':memory:' });
console.log(JSON.stringify(await ctx.sessionPersistence.list()));
await ctx.fiber.dispose();
`

describe.skipIf(!existsSync(builtBundle))('SQLite built package', () => {
  it('loads packaged SQL resources from the published entry', async () => {
    const { stdout, stderr } = await execFileAsync(process.execPath, ['--input-type=module', '-e', probe], {
      cwd: repoRoot,
      timeout: 15_000,
    })
    expect(stderr).toBe('')
    expect(JSON.parse(stdout) as unknown).toEqual([])
  })
})
