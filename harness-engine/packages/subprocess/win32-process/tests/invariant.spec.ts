import { describe, expect, it, vi } from 'vitest'
import { apply, inject, name } from '../src/invariant.ts'

describe('win32-process invariant companion', () => {
  it('registers the package-owned empty invariant', async () => {
    const dispose = vi.fn()
    const register = vi.fn((_packageName: string, _installer: () => void) => dispose)
    const ctx = { invariants: { register } } as never
    await expect(apply(ctx)).resolves.toBe(dispose)
    expect(name).toBe('win32-process-invariant')
    expect(inject).toEqual(['invariants'])
    expect(register).toHaveBeenCalledWith('@deepseek-ai/dsh-win32-process', expect.any(Function))
    const installer = register.mock.calls[0]![1]
    installer()
  })
})
