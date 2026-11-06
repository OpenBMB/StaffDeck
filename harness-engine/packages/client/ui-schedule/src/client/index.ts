/** Browser half of the read-only Schedule catalog. */

import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-client-locale/client'
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type {} from '@deepseek-ai/dsh-client-ui-session/client'
import type {} from '@deepseek-ai/dsh-schedule/client'
import { ScheduleCatalogAction } from './ScheduleCatalogAction.tsx'
import { en, NS, zh, type ScheduleCatalogKey } from './locales.ts'

declare module '@deepseek-ai/dsh-client-ui-slots' {
  interface LocaleNamespaceMap {
    /** Read-only active Schedule catalog copy. */
    'schedule.catalog': ScheduleCatalogKey
  }
}

/** Required services for locale registration and header-slot contribution. */
export const inject = ['slots', 'locale']

/** Register the dictionaries and Session-header catalog action. */
export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'ui-schedule: dictionaries')
  ctx.slots.inject(
    'conversation.session.header.actions',
    () => ctx.slots.register({
      name: 'conversation.session.header.actions',
      id: 'schedule-catalog',
      // Static Session identity precedes this entry; background jobs follow it.
      order: 10,
      locale: NS,
    }, ScheduleCatalogAction),
  )
}
