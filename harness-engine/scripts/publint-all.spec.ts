import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { afterEach, describe, expect, it } from 'vitest'

const repositoryRoot = fileURLToPath(new URL('..', import.meta.url))
const runner = fileURLToPath(new URL('./publint-all.ts', import.meta.url))
const roots: string[] = []

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true })
})

function fixture(options: {
  exportPath?: string
  indexSource?: string
  files?: Record<string, string>
} = {}): string {
  const root = mkdtempSync(join(tmpdir(), 'dsh-publint-all-'))
  roots.push(root)
  const packageDir = join(root, 'packages/core/probe')
  mkdirSync(join(packageDir, 'lib'), { recursive: true })
  writeFileSync(join(packageDir, 'package.json'), `${JSON.stringify({
    name: '@deepseek-ai/dsh-probe',
    version: '0.0.1',
    type: 'module',
    license: 'MIT',
    engines: { node: '>=22.19' },
    sideEffects: false,
    files: ['lib'],
    exports: { '.': { default: options.exportPath ?? './lib/index.js' } },
  }, null, 2)}\n`)
  writeFileSync(join(packageDir, 'README.md'), '# Probe\n')
  writeFileSync(join(packageDir, 'lib/index.js'), options.indexSource ?? 'export const probe = true\n')
  for (const [path, source] of Object.entries(options.files ?? {})) {
    mkdirSync(join(packageDir, path, '..'), { recursive: true })
    writeFileSync(join(packageDir, path), source)
  }
  writeFileSync(join(packageDir, 'unpublished.js'), 'export const hidden = true\n')
  return root
}

function run(root: string) {
  return spawnSync(process.execPath, [
    '--import', 'tsx', runner,
    '--packages-root', root,
  ], {
    cwd: repositoryRoot,
    encoding: 'utf8',
    timeout: 5_000,
  })
}

describe('publint package runner', () => {
  it('lints recursively declared files from an in-memory publication view', () => {
    const result = run(fixture())
    expect(result.status, result.stderr).toBe(0)
    expect(result.stdout).toContain('linting 1 package(s)')
    expect(result.stdout).toContain('All good!')
  })

  it('rejects an export that exists in the workspace but is not published', () => {
    const result = run(fixture({ exportPath: './unpublished.js' }))
    expect(result.status).toBe(1)
    expect(result.stdout).toContain('unpublished.js')
  })

  it('rejects a public export whose built file is missing', () => {
    const result = run(fixture({ exportPath: './lib/missing.js' }))
    expect(result.status).toBe(1)
    expect(result.stdout).toContain('missing.js')
  })

  it('accepts published relative JavaScript and CSS targets', () => {
    const result = run(fixture({
      indexSource: "export { helper } from './helper.js'\nimport './theme.css'\n",
      files: {
        'lib/helper.js': 'export const helper = true\n',
        'lib/theme.css': ':root {}\n',
      },
    }))
    expect(result.status, result.stderr).toBe(0)
  })

  it('rejects unpublished relative JavaScript and CSS targets', () => {
    const result = run(fixture({
      indexSource: "export { helper } from './missing.js'\nimport './missing.css'\n",
    }))
    expect(result.status).toBe(1)
    expect(result.stderr).toContain('imports "./missing.js"')
    expect(result.stderr).toContain('imports "./missing.css"')
  })
})
