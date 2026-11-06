import type { Context } from '@deepseek-ai/cordis'

/** Provide an in-memory credential-record owner for a mounted Connection plugin. */
export function provideBrowserCredentials(ctx: Context): void {
  const records = new Map<unknown, unknown>()
  ctx.provide('credentials', {
    async modifyRecord(
      key: unknown,
      mutate: (current: unknown) => Promise<unknown>,
    ): Promise<unknown> {
      const current = records.get(key)
      const next = await mutate(current)
      if (next !== undefined) records.set(key, next)
      return next ?? current
    },
  } as never)
}
