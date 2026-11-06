#!/usr/bin/env node
/** Inspect the public Claude Code Bundle composition without invoking the product. */

import { boot, loadOverlayPatches, resolveConfigPath } from '@deepseek-ai/dsh-app-boot'
import type {} from '@deepseek-ai/dsh-subagent'
import type {} from '@deepseek-ai/dsh-tools'

const configPath = process.argv[2]
const bundlePatchPath = process.argv[3]
if (configPath === undefined || bundlePatchPath === undefined) {
  throw new Error('Claude Code Loader composition driver requires config and Bundle patch paths')
}

let starts = 0
const ctx = await boot(
  'subagent-claude-code-loader-composition',
  resolveConfigPath(configPath, undefined),
  loadOverlayPatches('subagent-claude-code-loader-composition', bundlePatchPath),
  (hostCtx) => {
    hostCtx.on('subagent/start', () => {
      starts += 1
    })
  },
)

try {
  const providerNames = [
    'codex',
    'claude-code',
    'claude-primary',
    'claude-secondary',
  ] as const
  const toolNames = [
    'subagent_codex',
    'subagent_claude_code',
    'subagent_claude_primary',
    'subagent_claude_secondary',
  ] as const
  const providers = providerNames.map((providerName) => {
    const provider = ctx.subagents.getProvider(providerName)
    if (provider === undefined) {
      throw new Error(`${providerName} provider was not registered`)
    }
    return {
      name: provider.name,
      capabilities: provider.capabilities,
      inheritsParentContext: provider.inheritsParentContext,
    }
  })
  const tools = toolNames.map((toolName) => {
    const tool = ctx.tools.schemas().find(schema => schema.name === toolName)
    if (tool === undefined) throw new Error(`${toolName} tool was not registered`)
    const properties = tool.parameters.properties
    if (
      typeof properties !== 'object'
      || properties === null
      || Array.isArray(properties)
    ) {
      throw new Error(`${toolName} has invalid parameter properties`)
    }
    return {
      name: tool.name,
      parameterNames: Object.keys(properties).sort(),
      required: tool.parameters.required,
    }
  })
  const jobTools = ctx.tools.schemas()
    .map(schema => schema.name)
    .filter(name => name === 'job_kill' || name === 'job_list' || name === 'job_output')
    .sort()

  process.stdout.write(`${JSON.stringify({
    registeredProviders: ctx.subagents.list().sort(),
    providers,
    tools,
    jobTools,
    starts,
  })}\n`)
} finally {
  await ctx.fiber.dispose()
}
