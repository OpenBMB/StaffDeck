/** The standalone SDK-minimal bundle's complete declared Cordis tree. */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as yaml from 'js-yaml'
import { describe, expect, it } from 'vitest'
import { entryListSchema } from '@deepseek-ai/cordis-plugin-include'

describe('dsh-sdk-minimal bundle', () => {
  it('declares one standalone allowlisted tree with every row dependency', () => {
    const root = fileURLToPath(new URL('..', import.meta.url))
    const manifest = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8')) as {
      dependencies?: Record<string, string>
      dsh?: { bundle?: { patch?: string } }
    }
    expect(manifest.dsh?.bundle?.patch).toBe('./cordis.patch.yml')
    const patches = yaml.load(
      readFileSync(resolve(root, manifest.dsh!.bundle!.patch!), 'utf8'),
      { schema: entryListSchema },
    ) as Array<{ insert?: Array<{ id?: string; inject?: string[]; name?: string; config?: Record<string, unknown>; disabled?: unknown }> }>
    expect(patches).toHaveLength(1)
    const rows = patches[0]?.insert ?? []
    expect(rows.map(row => [row.id, row.name])).toEqual([
      ['sdk-app-startup', '@deepseek-ai/dsh-sdk-app'],
      ['sdk-jsonrpc-server', '@deepseek-ai/dsh-sdk-jsonrpc-server'],
      ['deepseek-llm-api-extensions', '@deepseek-ai/dsh-deepseek-llm-api-extensions'],
      ['session-log-deepseek', '@deepseek-ai/dsh-session-log-deepseek'],
      ['plugin-package-inventory-deepseek', '@deepseek-ai/dsh-plugin-package-inventory-deepseek'],
      ['llm-deepseek', '@deepseek-ai/dsh-llm-deepseek'],
      ['sandbox', '@deepseek-ai/dsh-sandbox-local'],
      ['session-projection', '@deepseek-ai/dsh-session-projection'],
      ['sandbox-policy', '@deepseek-ai/dsh-sandbox-policy'],
      ['subprocess', '@deepseek-ai/dsh-subprocess-local'],
      ['pty', '@deepseek-ai/dsh-terminal'],
      ['terminal-bash', '@deepseek-ai/dsh-terminal-bash'],
      ['terminal-pwsh', '@deepseek-ai/dsh-terminal-bash'],
      ['fs-local', '@deepseek-ai/dsh-fs-local'],
      ['agent-spine', '@deepseek-ai/dsh-agent-spine-demo'],
      ['persistent-bash', '@deepseek-ai/dsh-tool-bash-persistent'],
      ['persistent-pwsh', '@deepseek-ai/dsh-tool-pwsh-persistent'],
      ['str-replace-editor', '@deepseek-ai/dsh-tool-str-replace-editor'],
      ['sessions', '@deepseek-ai/dsh-session-persistence-jsonl'],
    ])
    expect(rows.find(row => row.id === 'sdk-app-startup')?.config).toEqual({ profile: 'sdk-minimal' })
    expect(rows.find(row => row.id === 'sdk-jsonrpc-server')).toMatchObject({
      inject: ['sdkAppStartup', 'loader'],
      config: { maxTokensAsSuccess: false },
    })
    expect(rows.find(row => row.id === 'llm-deepseek')?.config).toEqual({
      apiKeyEnv: 'DEEPSEEK_API_KEY',
      defaultContextWindow: { __jsExpr: 'Number(process.env.DSH_CONTEXT_WINDOW ?? 1000000)' },
      streamIdleTimeoutMs: 172800000,
    })
    expect(rows.find(row => row.id === 'agent-spine')?.config).toMatchObject({
      includeHarnessIdentity: false,
      includeRuntimeContext: false,
      workspaceContext: false,
      skills: { enabled: false },
      toolBash: false,
      toolJobs: false,
    })
    expect(rows.find(row => row.id === 'terminal-bash')).toMatchObject({
      disabled: { __jsExpr: "process.platform === 'win32'" },
    })
    expect(rows.find(row => row.id === 'terminal-pwsh')).toMatchObject({
      disabled: { __jsExpr: "process.platform !== 'win32'" },
      config: { shellDialect: 'pwsh', timeoutMs: 300000 },
    })
    expect(Object.keys(manifest.dependencies ?? {}).sort()).toEqual(
      [...new Set(rows.map(row => row.name).filter((name): name is string => name !== undefined))].sort(),
    )
  })
})
