import { describe, expect, it } from 'vitest'
import { deepSeekImageTokens } from '../src/image-tokens.ts'

describe('DeepSeek v4 image tokens', () => {
  // Reference values from the provider's published image token calculator
  // (api-docs.deepseek.com, Token & Token Usage), at the worst-case pad.
  it.each([
    [100, 100, 117],
    [384, 384, 117],
    [640, 480, 209],
    [800, 800, 349],
    [1024, 768, 357],
    [1920, 1080, 369],
    [2000, 2000, 349],
    [5000, 5000, 349],
    [300, 50, 101],
  ])('prices %sx%s as %s tokens', (width, height, expected) => {
    expect(deepSeekImageTokens(width, height)).toBe(expected)
  })

  it('caps every image at 384 tokens regardless of source size', () => {
    for (const [width, height] of [[2000, 2000], [5000, 5000], [8192, 8192], [16, 8192]]) {
      expect(deepSeekImageTokens(width!, height!)).toBeLessThanOrEqual(384)
    }
  })

  it('prices small images at the documented scale-up floor', () => {
    // Below roughly 384x384 total pixels the provider scales up, so a tiny
    // square costs the same as a 384x384 one.
    expect(deepSeekImageTokens(100, 100)).toBe(deepSeekImageTokens(384, 384))
  })

  it('clamps extreme width by the aspect-ratio bound', () => {
    // Width beyond 8x height projects onto the same clamped grid.
    expect(deepSeekImageTokens(9000, 1)).toBe(113)
    expect(deepSeekImageTokens(8192, 100)).toBe(113)
  })

  it('solves a one-column grid for an extremely tall image', () => {
    // Height-dominant aspect drives the solver's single-column branch.
    expect(deepSeekImageTokens(16, 8192)).toBe(381)
    expect(deepSeekImageTokens(1, 9000)).toBe(381)
  })

  it('trims an odd solved grid height to the even row count', () => {
    expect(deepSeekImageTokens(100, 4036)).toBe(253)
  })

  it('converges through a second projection pass when the first is not a fixpoint', () => {
    expect(deepSeekImageTokens(4921, 353)).toBe(289)
    expect(deepSeekImageTokens(97, 7289)).toBe(245)
  })
})
