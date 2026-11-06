import { defineConfig } from 'vitest/config'
import webConfig from './vitest.web.config.ts'
import { vitestExecArgv } from './vitest.shared.ts'

// Manual high-cardinality diagnostics stay outside every default Vitest
// inventory and therefore outside CI's executed test lanes.
export default defineConfig({
  ...webConfig,
  test: {
    ...webConfig.test,
    // Memory diagnostics use forced-GC baselines only in this manual inventory.
    execArgv: [...vitestExecArgv, '--expose-gc'],
    include: [
      'apps/web/tests/**/*.perf.ts',
      'packages/client/ui-conversation/tests/**/*.perf.client.ts',
    ],
    disableConsoleIntercept: true,
    hookTimeout: 180_000,
    testTimeout: 600_000,
  },
})
