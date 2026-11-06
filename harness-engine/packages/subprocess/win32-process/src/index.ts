/** Low-level Win32 process, stdio, and Job Object primitives used by the Windows ACL sandbox. */

export { ERROR_INSUFFICIENT_BUFFER } from './abi.ts'
export * from './errors.ts'
export {
  allocPtrSlot,
  allocUint32,
  decodePtr,
  decodeUint32,
  extendWin32ProcessBindings,
  isNullPtr,
  throwLastError,
  throwWin32,
} from './ffi.ts'
export type {
  NativePtr,
  Win32ProcessBindings,
} from './ffi.ts'
export {
  drainPipe,
  spawnInheritedJobProcess,
  spawnPipedProcess,
  waitForProcessExit,
} from './process.ts'
export type {
  SpawnedJobProcess,
  SpawnedPipedProcess,
} from './process.ts'
