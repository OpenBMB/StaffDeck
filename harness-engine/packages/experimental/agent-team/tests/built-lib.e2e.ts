/** Plain-Node smoke for the built Agent Teams service and Remote contribution. */

import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { describe, expect, it } from 'vitest'

const packageDir = fileURLToPath(new URL('..', import.meta.url))
const root = resolve(packageDir, '../../..')
const artifact = (path: string): string => join(root, path)
const artifactUrl = (path: string): string => pathToFileURL(artifact(path)).href

const requiredArtifacts = [
  'packages/experimental/agent-team/lib/index.js',
  'packages/experimental/agent-team/lib/typert.remote-client.js',
].every(path => existsSync(artifact(path)))

describe.skipIf(!requiredArtifacts)('Agent Teams built LIB service', () => {
  it('loads the Host service and its generated browser contribution under plain Node', async () => {
    const urls = {
      host: artifactUrl('packages/experimental/agent-team/lib/index.js'),
      remote: artifactUrl('packages/experimental/agent-team/lib/typert.remote-client.js'),
    }
    const script = `
      const host = await import(${JSON.stringify(urls.host)})
      const remote = await import(${JSON.stringify(urls.remote)})
      console.log(JSON.stringify({
        className: host.default.name,
        methods: remote.default.descriptors.map(descriptor => descriptor.id),
      }))
    `

    const result = await runPlainNode(script)
    expect(result.exitCode, `stderr:\n${result.stderr}`).toBe(0)
    const output = JSON.parse(result.stdout.trim().split('\n').at(-1) ?? '{}') as {
      className: string
      methods: string[]
    }
    expect(output).toEqual({
      className: 'TeamService',
      methods: [
        '@deepseek-ai/dsh-experimental-agent-team#agentTeams/createTask',
        '@deepseek-ai/dsh-experimental-agent-team#agentTeams/updateTask',
        '@deepseek-ai/dsh-experimental-agent-team#agentTeams/view',
      ],
    })
  })
})

function runPlainNode(script: string): Promise<{
  readonly exitCode: number | null
  readonly stdout: string
  readonly stderr: string
}> {
  return new Promise((resolveRun) => {
    execFile(process.execPath, ['--input-type=module', '-e', script], {
      cwd: packageDir,
      encoding: 'utf8',
      timeout: 30_000,
    }, (error, stdout, stderr) => {
      resolveRun({
        exitCode: error === null ? 0 : typeof error.code === 'number' ? error.code : null,
        stdout,
        stderr,
      })
    })
  })
}
