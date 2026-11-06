/**
 * Route-aware surface pricing: projects the fold's fixed-heuristic nodes onto
 * the routed model's request, replacing every image occurrence's structural
 * price with the route's declared visual tokens plus the model-visible text it
 * actually sends. Without declared pricing every node keeps its fixed
 * heuristic price, so provider-neutral behavior is unchanged.
 *
 * @module @deepseek-ai/dsh-token-meter/route-pricing
 */

import type { LlmImageRequestPricing } from '@deepseek-ai/dsh-llm'
import { estimateContent } from './estimate.ts'
import type { MeterSurfaceNode } from './surface-fold.ts'
import type { TokenSurfaceNode } from './types.ts'

/** One surface priced for a request route: public nodes plus their total. */
export interface PricedSurface {
  /** Positional nodes carrying both the route price and the fixed-heuristic price. */
  readonly nodes: TokenSurfaceNode[]
  /** Sum of the route prices across the surface. */
  readonly surfaceTokens: number
}

/**
 * Price one ordered surface under a route's request-image pricing.
 * @param nodes - the fold's current or snapshotted surface, in model-visible order.
 * @param pricing - the routed model's image pricing, or undefined to keep the fixed heuristic.
 * @returns detached public nodes and their route-priced total.
 * @throws when the pricing answers a different occurrence count than it was
 *   asked — misalignment would silently misprice nodes, so it must fail loud.
 */
export function priceSurface(
  nodes: readonly MeterSurfaceNode[],
  pricing: LlmImageRequestPricing | undefined,
): PricedSurface {
  const images = pricing === undefined ? [] : nodes.flatMap(node => node.images)
  if (pricing === undefined || images.length === 0) {
    let surfaceTokens = 0
    const publicNodes = nodes.map((node) => {
      surfaceTokens += node.heuristicTokens
      return { seq: node.seq, tokens: node.heuristicTokens, heuristicTokens: node.heuristicTokens }
    })
    return { nodes: publicNodes, surfaceTokens }
  }
  const prices = pricing.priceImages(images)
  if (prices.length !== images.length) {
    throw new Error(
      `token meter: route image pricing answered ${prices.length} prices for ${images.length} occurrences`,
    )
  }
  let cursor = 0
  let surfaceTokens = 0
  const publicNodes = nodes.map((node) => {
    let tokens = node.heuristicTokens
    if (node.images.length > 0) {
      tokens = node.imageFreeTokens
      for (let occurrence = 0; occurrence < node.images.length; occurrence += 1) {
        // oxlint-disable-next-line typescript/no-non-null-assertion -- length equality is asserted above
        const price = prices[cursor]!
        cursor += 1
        tokens += price.visualTokens + estimateContent([{ type: 'text', text: price.text }])
      }
    }
    surfaceTokens += tokens
    return { seq: node.seq, tokens, heuristicTokens: node.heuristicTokens }
  })
  return { nodes: publicNodes, surfaceTokens }
}
