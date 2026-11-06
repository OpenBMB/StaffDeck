/**
 * Duplicate-install-safe nominal string helpers.
 *
 * A brand makes structurally-identical strings non-interchangeable at the type
 * level: a `SessionId` cannot be passed where a `ToolCallId` is expected, even
 * though both are plain strings at runtime. Comparison, logging, and
 * serialization all behave as ordinary strings.
 *
 * This package owns no concrete id and keeps no runtime identity or mutable
 * state, so independently installed copies produce interchangeable values.
 *
 * @module @deepseek-ai/dsh-brand
 */

declare const BRAND: unique symbol

/** A string carrying a compile-time-only brand `B`. */
export type Branded<B extends string> = string & { readonly [BRAND]: B }

/**
 * Apply a compile-time string brand without changing the value.
 * @param value - string admitted by the domain that owns the target brand.
 * @returns the same string with the requested compile-time brand.
 */
export function brandString<T extends Branded<string>>(value: string | T): T {
  return value as T
}
