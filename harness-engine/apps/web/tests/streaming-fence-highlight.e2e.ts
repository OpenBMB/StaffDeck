/** Keyless assembled-Web evidence for syntax highlighting during a streamed code fence. */

import { fileURLToPath } from 'node:url'
import type { Browser, Page } from 'playwright'
import { chromium } from 'playwright'
import { afterAll, beforeAll, describe, expect, it, onTestFailed } from 'vitest'
import type { GenerateOptions, StreamChunk } from '@deepseek-ai/dsh-llm'
import { LlmAdapter } from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-agent-default-model'
import {
  assertFixtureInventory,
  captureStableAria,
  compareOrRefreshGolden,
  launchWebScaffold,
  watchConsole,
  webSnapshotMode,
  type WebScaffold,
} from './scaffold.ts'
import { connectFreshWorkspace, newEnglishPage, saveFailureShot, writeComposerDraft } from './support.ts'

const SNAPSHOT_DIR = fileURLToPath(new URL('./snapshots/streaming-fence-highlight', import.meta.url))
const MID_EXPECTED = fileURLToPath(new URL('./snapshots/streaming-fence-highlight/mid-stream.expected.md', import.meta.url))
const MODE = webSnapshotMode()
const PROVIDER = 'streaming-fence-highlight-test'
const MODEL = 'streaming-fence'
const PROMPT = 'Stream one TypeScript fence for the highlighting snapshot.'
const OPEN_REPLY = '```ts\nconst first: number = 1\nconst second = "two"\nlet tail'
const REPLY = `${OPEN_REPLY}\n\`\`\``

/** Deterministic model response held after the visible fence body arrives. */
class StreamingFenceAdapter extends LlmAdapter {
  private resolvePaused!: () => void
  private resolveContinuation!: () => void
  private continued = false
  readonly paused = new Promise<void>((resolve) => { this.resolvePaused = resolve })
  private readonly continuation = new Promise<void>((resolve) => { this.resolveContinuation = resolve })

  continue(): void {
    if (this.continued) return
    this.continued = true
    this.resolveContinuation()
  }

  override async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    yield { type: 'block-start', index: 0, blockType: 'text' }
    yield { type: 'text-delta', index: 0, text: OPEN_REPLY }
    this.resolvePaused()
    await this.continuation
    if (options.signal?.aborted === true) throw options.signal.reason
    yield { type: 'text-delta', index: 0, text: '\n```' }
    yield { type: 'block-end', index: 0, block: { type: 'text', text: REPLY } }
    yield { type: 'finish', reason: { kind: 'stop' } }
  }
}

interface FenceTree {
  language: string
  pre: { className: string; style: string | null; tabIndex: string | null }
  lines: { text: string; style: string | null }[][]
}

/** Read the stable, user-visible subset of one rendered code fence. */
async function fenceTree(block: ReturnType<Page['locator']>): Promise<FenceTree> {
  return await block.evaluate((element) => {
    const pre = element.querySelector<HTMLPreElement>('pre.shiki')
    if (pre === null) throw new Error('streaming fence did not render through the shiki arm')
    return {
      language: element.querySelector('[class*="infostring"]')?.textContent ?? '',
      pre: {
        className: pre.className,
        style: pre.style.cssText,
        tabIndex: pre.getAttribute('tabindex'),
      },
      lines: [...pre.querySelectorAll('.line')].map(line =>
        [...line.querySelectorAll('span')].map(span => ({
          text: span.textContent ?? '',
          style: span.style.cssText,
        })),
      ),
    }
  })
}

describe.skipIf(MODE === 'record')('web e2e: streaming code-fence highlighting', () => {
  let scaffold: WebScaffold
  let browser: Browser
  let page: Page
  let tripwire: ReturnType<typeof watchConsole>
  const adapter = new StreamingFenceAdapter()

  beforeAll(async () => {
    scaffold = await launchWebScaffold()
    scaffold.ctx.effect(
      () => scaffold.ctx.llm.registerAdapter([PROVIDER], adapter),
      'streaming fence highlight adapter',
    )
    await scaffold.ctx.agentDefaultModel.saveSelection({ provider: PROVIDER, model: MODEL })
    browser = await chromium.launch()
    page = await newEnglishPage(browser)
    tripwire = watchConsole(page)
    await page.goto(scaffold.authenticatedUrl, { waitUntil: 'load' })
    await page.waitForSelector('[class*="frame"]', { timeout: 30_000 })
    await connectFreshWorkspace(page, scaffold.workspaceCwd)
  }, 120_000)

  afterAll(async () => {
    adapter.continue()
    await browser?.close()
    await scaffold?.close()
  })

  it('renders the growing fence through shiki and preserves its token tree when the turn settles', async () => {
    onTestFailed(() => saveFailureShot(page, 'web-e2e-streaming-fence-highlight'))
    const input = page.locator('[data-composer-input]').first()
    const settled = scaffold.whenTurnSettled(30_000)
    await writeComposerDraft(page, input, PROMPT)
    await input.press('Enter')
    await adapter.paused

    const streaming = page.locator('[data-streaming="true"]')
    await streaming.waitFor({ timeout: 10_000 })
    const block = streaming.locator('.md-code-block').filter({ hasText: 'const first' })
    await block.locator('pre.shiki span[style]').first().waitFor({ timeout: 10_000 })
    const midTree = await fenceTree(block)
    expect(midTree.language).toBe('ts')
    expect(midTree.lines).toHaveLength(3)
    expect(midTree.lines.flat().map(span => span.style)).toContain('color: var(--shiki-token-keyword);')

    const aria = await captureStableAria(page, '[class*="centerCol"]', scaffold.workspaceCwd)
    await compareOrRefreshGolden(
      MID_EXPECTED,
      `${aria}\n\n---\n\n${JSON.stringify(midTree, null, 2)}`,
      MODE,
    )

    adapter.continue()
    await settled
    await expect.poll(() => page.locator('[data-streaming="true"]').count(), { timeout: 10_000 }).toBe(0)
    const settledBlock = page.locator('.md-code-block').filter({ hasText: 'const first' })
    await settledBlock.locator('pre.shiki').waitFor({ timeout: 10_000 })
    expect(await fenceTree(settledBlock)).toEqual(midTree)
    expect(tripwire.pageErrors).toEqual([])
    expect(tripwire.warnings).toEqual([])
    await assertFixtureInventory(SNAPSHOT_DIR, ['mid-stream.expected.md'])
  }, 60_000)
})
