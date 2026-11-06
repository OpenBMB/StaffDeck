import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import {
  LOADER_SMOKE_TEST_TIMEOUT_MS,
  runLoaderSmoke,
} from '@deepseek-ai/dsh-loader-smoke'

const fixtureDir = fileURLToPath(new URL(
  './fixtures/loader/',
  import.meta.url,
))
const driver = join(fixtureDir, 'driver.ts')
const configPath = join(fixtureDir, 'cordis.yml')
const packageDir = fileURLToPath(new URL('..', import.meta.url))
const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8')) as {
  dsh?: { bundle?: { patch?: string } }
}
const bundlePatch = manifest.dsh?.bundle?.patch
if (bundlePatch === undefined) throw new Error('Claude Code package must declare a Bundle patch')
const bundlePatchPath = join(packageDir, bundlePatch)
const repoTsconfig = fileURLToPath(new URL('../../../../tsconfig.json', import.meta.url))

describe('product-provider public Loader composition', () => {
  it('loads the Bundle default, two named Claude instances, their tools, and Codex without starting either product', async () => {
    const { stdout, stderr } = await runLoaderSmoke({
      label: 'product-provider Loader composition',
      tempDirPrefix: 'dsh-product-provider-loader-',
      binScript: driver,
      libBinScript: driver,
      configPath,
      binArgs: [configPath, bundlePatchPath],
      tsconfigPath: repoTsconfig,
      env: {
        // Loading the optional package must not probe or start a Claude binary.
        PATH: '',
      },
    })

    expect(stderr).toBe('')
    expect(JSON.parse(stdout)).toEqual({
      registeredProviders: ['claude-code', 'claude-primary', 'claude-secondary', 'codex'],
      providers: [
        {
          name: 'codex',
          capabilities: {
            agentOptions: false,
            outputSchema: false,
            depthLimit: false,
            toolFilter: false,
            persona: false,
          },
          inheritsParentContext: false,
        },
        {
          name: 'claude-code',
          capabilities: {
            agentOptions: false,
            outputSchema: false,
            depthLimit: false,
            toolFilter: false,
            persona: false,
          },
          inheritsParentContext: false,
        },
        {
          name: 'claude-primary',
          capabilities: {
            agentOptions: false,
            outputSchema: false,
            depthLimit: false,
            toolFilter: false,
            persona: false,
          },
          inheritsParentContext: false,
        },
        {
          name: 'claude-secondary',
          capabilities: {
            agentOptions: false,
            outputSchema: false,
            depthLimit: false,
            toolFilter: false,
            persona: false,
          },
          inheritsParentContext: false,
        },
      ],
      tools: [
        {
          name: 'subagent_codex',
          parameterNames: ['description', 'prompt', 'run_in_background'],
          required: ['description', 'prompt'],
        },
        {
          name: 'subagent_claude_code',
          parameterNames: ['description', 'prompt', 'run_in_background'],
          required: ['description', 'prompt'],
        },
        {
          name: 'subagent_claude_primary',
          parameterNames: ['description', 'prompt', 'run_in_background'],
          required: ['description', 'prompt'],
        },
        {
          name: 'subagent_claude_secondary',
          parameterNames: ['description', 'prompt', 'run_in_background'],
          required: ['description', 'prompt'],
        },
      ],
      jobTools: ['job_kill', 'job_list', 'job_output'],
      starts: 0,
    })
  }, LOADER_SMOKE_TEST_TIMEOUT_MS)
})
