import koffi from 'koffi'
import { describe, expect, it, vi } from 'vitest'
import {
  Win32Error,
  allocPtrSlot,
  decodePtr,
  isNullPtr,
  throwLastError,
} from '../src/index.ts'
import { PROCESS_INFORMATION_SIZE, STARTUPINFOW_SIZE } from '../src/abi.ts'
import { PROCESS_INFORMATION, STARTUPINFOW, errorText } from '../src/ffi.ts'
import type { NativePtr, Win32ProcessBindings } from '../src/index.ts'

describe('shared Win32 process ABI', () => {
  it('matches the verified x64 structure sizes', () => {
    expect(STARTUPINFOW.size).toBe(STARTUPINFOW_SIZE)
    expect(PROCESS_INFORMATION.size).toBe(PROCESS_INFORMATION_SIZE)
  })

  it('handles NULL pointer out-parameters', () => {
    const slot = allocPtrSlot()
    expect(decodePtr(slot)).toBeNull()
    expect(isNullPtr(0n as NativePtr)).toBe(true)
    expect(isNullPtr(1n as NativePtr)).toBe(false)
  })

  it('formats and throws the exact Win32 error', () => {
    const api = {
      getLastError: vi.fn(() => 5),
      formatMessageW: vi.fn((_flags, _source, _id, _language, buffer: Buffer) => {
        buffer.write('access denied', 'utf16le')
        return 'access denied'.length
      }),
    } as unknown as Win32ProcessBindings
    expect(errorText(api, 5)).toBe('access denied')
    expect(() => throwLastError(api, 'Probe')).toThrow(Win32Error)
    expect(new Win32Error('CloseHandle', 6).message).toBe('CloseHandle failed (Win32 6)')
  })

  it('decodes a pointer stored by Koffi', () => {
    const slot = allocPtrSlot()
    koffi.encode(slot, koffi.pointer('void'), 42n)
    expect(decodePtr(slot)).toBe(42n)
  })
})
