/** Module-HMR ownership across the real shipped profile bundle layers. */

import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { composeEntries, loadOverlayPatches } from '@deepseek-ai/dsh-app-boot'
import type { PatchOptions } from '@deepseek-ai/cordis-plugin-include'

const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url))

/** Load one shipped bundle patch through the same parser as profile boot. */
function bundle(name: 'acp-app' | 'base' | 'headless' | 'sdk-app' | 'sdk-minimal' | 'web-app'): PatchOptions[] {
  return loadOverlayPatches('profile-hmr test', join(REPOSITORY_ROOT, 'packages', 'bundle', name, 'cordis.patch.yml'))
}

/** Resolve the effective HMR row after the supplied layers. */
function hmr(layers: PatchOptions[][]) {
  const row = composeEntries(layers).find(entry => entry.id === 'hmr')
  if (row === undefined) throw new Error('the base bundle must insert the hmr row')
  return row
}

describe('profile module-HMR policy', () => {
  it.each(['web-app', 'headless', 'sdk-app', 'acp-app'] as const)(
    '%s inherits the disabled base row without a mode override',
    (mode) => {
      const modePatches = bundle(mode)
      expect(modePatches.some(patch => patch.id === 'hmr')).toBe(false)
      expect(hmr([bundle('base'), modePatches])).toMatchObject({
        disabled: true,
        config: { root: ['.'] },
      })
    },
  )

  it('requires an explicit later layer to enable source-module reload', () => {
    expect(hmr([bundle('base'), [{ id: 'hmr', disabled: false }]])).toMatchObject({
      disabled: false,
      config: { root: ['.'] },
    })
  })

  it('keeps the standalone sdk-minimal tree free of module HMR', () => {
    expect(composeEntries([bundle('sdk-minimal')]).find(entry => entry.id === 'hmr')).toBeUndefined()
  })
})
