/**
 * Feedback controls stylesheet contract, asserted against the CSS text on disk.
 *
 * A `--dsw-*` name the theme never declares fails silently, and for this sheet
 * it failed loudly in the product: `border`, `background`, and the primary
 * button's fill and label each named a token that does not exist, so every one
 * of those declarations was invalid at computed-value time and dropped. The
 * note editor shipped with no border and no surface, and its Save button with
 * neither fill nor readable label. Nothing downstream reports this — the sheet
 * parses, the classes attach, and the DOM snapshots are unchanged.
 *
 * The editor is a popover portaled to `document.body` and fixed-positioned
 * from the note trigger's rect, so it never enters the IconActions row's flex
 * layout at all — the row keeps its single 28px line of icons and the note
 * trigger, and no wrapping (`flex-wrap`) or `order` is needed for it. The
 * width-independent half of that contract is asserted here (the panel is a
 * fixed portal, not an inline flex item); the resulting geometry is measured
 * in a real engine by `apps/web/tests/message-feedback-layout`.
 */
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const css = readFileSync(
  fileURLToPath(new URL('../src/client/MessageFeedbackActions.module.css', import.meta.url)),
  'utf8',
)
// The theme package maps `./styles/*` to `./src/styles/*`, so the declarations
// stay on the source plane rather than needing a build. Every theme sheet, not
// just the platform tokens: font and scrollbar variables are declared in
// siblings, and a gate reading one file would call their names undeclared.
const tokens = readdirSync(fileURLToPath(new URL('../../ui-theme/src/styles/', import.meta.url)))
  .filter(name => name.endsWith('.css'))
  .map(name => readFileSync(fileURLToPath(new URL(`../../ui-theme/src/styles/${name}`, import.meta.url)), 'utf8'))
  .join('\n')

/**
 * The declarations of one top-level rule, by selector.
 * @param selector - the class selector to read, including its leading dot.
 * @returns the rule's declaration text.
 */
function block(selector: string): string {
  const match = new RegExp(`^\\${selector} \\{([^}]*)\\}`, 'm').exec(css)
  if (match === null) throw new Error(`MessageFeedbackActions.module.css has no \`${selector}\` rule`)
  return match[1] ?? ''
}

describe('MessageFeedbackActions theme styles', () => {
  it('names only theme variables the token sheet defines', () => {
    // The regression that motivated this file. An undeclared custom property
    // has no fallback and does not inherit a usable value: the entire
    // declaration is thrown away, so the control renders as if the line had
    // never been written. Every theme-variable prefix the sheets actually use,
    // not just `--dsw-`: a `--dsh-` name reads as a plausible sibling and would
    // otherwise slip past into an invalid declaration.
    const named = [...css.matchAll(/var\((--(?:dsw|dsh|ds)-[a-z0-9-]+)/g)].map(match => match[1])
    // Vacuity guard: the sheet has to actually name tokens, or the filter below
    // is satisfied by an empty list and this test proves nothing.
    expect(named.length).toBeGreaterThan(5)
    const undeclared = [...new Set(named)].filter(name => !tokens.includes(`  ${String(name)}:`))
    expect(undeclared).toEqual([])
  })

  it('never falls back to a literal colour', () => {
    // A token that resolves is never the problem; an undeclared one takes this
    // branch, and a literal here is a single colour for both themes.
    expect(css).not.toMatch(/var\(--dsw-[a-z0-9-]+\s*,\s*(?:#|rgb|rgba|hsl|hsla)/)
  })

  it('keeps the note editor out of the row as a fixed portal, not a flex item', () => {
    // The editor is a popover portaled to document.body, so the IconActions row
    // never has to grow or wrap around it. Fixed positioning comes from the
    // placement code (inline `left`/`top`), not a class, so only `position:
    // fixed` and the elevated surface live in the sheet — plus the absence of a
    // flex rule on the panel, which would resurrect the row-overflow defect an
    // inline editor had. The row stays one 28px line, so a fixed `width` on the
    // panel is fine (it floats, it does not compete for row space).
    expect(block('.notePanel')).toMatch(/position:\s*fixed/)
    // The panel flex-sets its own children (textarea over buttons), which is
    // fine. What must be absent is the flex-SIZING that made an inline editor a
    // row item: grow/shrink/basis (or the `flex:` shorthand) would let it rejoin
    // the IconActions layout, resurrecting the overflow defect.
    expect(block('.notePanel')).not.toMatch(/flex-(?:grow|shrink|basis)\s*:/)
    expect(block('.notePanel')).not.toMatch(/^\s*flex\s*:/m)
  })

  it('closes every block, so no rule is swallowed by the one above it', () => {
    // A missing `}` is not a parse error: every rule after it silently becomes
    // part of the block above, and the controls would paint unstyled.
    const bare = css.replace(/\/\*[\s\S]*?\*\//g, '')
    expect((bare.match(/\}/g) ?? []).length).toBe((bare.match(/\{/g) ?? []).length)
  })

  it('slot-injected actions ride the content font-size axis like their host row', () => {
    // These buttons render inside ui-chat's MessageIconActions row; a fixed
    // 28px would leave them undersized (or overflowing) once the Settings
    // font size moves the row.
    expect(block('.action')).toMatch(/width:\s*calc\(28px \+ var\(--dsh-content-font-delta, 0px\)\)/)
    expect(block('.action')).toMatch(/height:\s*calc\(28px \+ var\(--dsh-content-font-delta, 0px\)\)/)
    expect(block('.action svg')).toMatch(/width:\s*calc\(15px \+ var\(--dsh-content-font-delta, 0px\)\)/)
    expect(block('.noteOpen')).toMatch(/line-height:\s*calc\(28px \+ var\(--dsh-content-font-delta, 0px\)\)/)
  })
})
