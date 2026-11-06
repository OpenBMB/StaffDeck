// A row that resolves and then refuses to apply. Discovery's health check
// resolves every row's module without importing it, so a fixture naming a file
// that does not exist can no longer reach the mount; a module that loads and
// throws is what still exercises the loader's own failure reporting.
export const name = 'throws'

export function apply(_ctx, config) {
  throw new Error(config.message)
}
