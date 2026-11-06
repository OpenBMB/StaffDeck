/** Typed Win32 process operations over the shared binding table. */

import koffi from 'koffi'
import * as abi from './abi.ts'
import {
  allocProcessInfo,
  allocPtrSlot,
  allocStartupInfo,
  allocUint32,
  decodeProcessInfo,
  decodePtr,
  decodeUint32,
  encodeStartupInfo,
  isNullPtr,
  throwLastError,
  throwWin32,
} from './ffi.ts'
import type { NativePtr, Win32ProcessBindings } from './ffi.ts'

/**
 * Quote one argument according to CommandLineToArgvW parsing.
 * @param argument - one argv entry.
 * @returns bare or quoted command-line segment.
 */
export function quoteArg(argument: string): string {
  if (argument === '') return '""'
  if (!/[\s"]/u.test(argument)) return argument
  let quoted = '"'
  for (let index = 0; index < argument.length; index++) {
    let backslashes = 0
    while (index < argument.length && argument.charAt(index) === '\\') {
      backslashes += 1
      index += 1
    }
    if (index === argument.length) {
      quoted += '\\'.repeat(backslashes * 2)
    } else if (argument.charAt(index) === '"') {
      quoted += '\\'.repeat(backslashes * 2 + 1) + '"'
    } else {
      quoted += '\\'.repeat(backslashes) + argument.charAt(index)
    }
  }
  return quoted + '"'
}

/**
 * Build the mutable command line accepted by CreateProcessAsUserW.
 * @param program - executable argv entry.
 * @param args - remaining argv entries.
 * @returns joined Win32 command line.
 */
export function buildCommandLine(program: string, args: readonly string[]): string {
  return [program, ...args].map(quoteArg).join(' ')
}

/** Restricted-token process creation inputs owned by the Windows ACL sandbox. */
export interface RestrictedProcessSpawnOptions {
  /** Executable argv entry passed through CreateProcessAsUserW. */
  command: string
  /** Arguments excluding the executable. */
  args: readonly string[]
  /** Existing child working directory. */
  cwd: string
  /** Restricted primary token supplied by sandbox policy. */
  token: NativePtr
}

/** Piped child resources whose process and read handles remain caller-owned. */
export interface SpawnedPipedProcess {
  /** Direct child process id. */
  pid: number
  /** Process handle closed by waitForProcessExit. */
  process: NativePtr
  /** Stdout pipe read end closed by drainPipe. */
  stdoutRead: NativePtr
  /** Stderr pipe read end closed by drainPipe. */
  stderrRead: NativePtr
}

/** Suspended child assigned to one caller-owned kill-on-close Job before resume. */
export interface SpawnedJobProcess {
  /** Direct child process id. */
  pid: number
  /** Process handle closed by waitForProcessExit. */
  process: NativePtr
  /** Job handle closed by the lifecycle owner. */
  job: NativePtr
}

interface PipePair {
  read: NativePtr
  write: NativePtr
}

function freeNative(pointer: NativePtr | undefined): void {
  if (pointer !== undefined) koffi.free(pointer)
}

function closeBestEffort(api: Win32ProcessBindings, handle: NativePtr | null | undefined): void {
  if (!isNullPtr(handle)) api.closeHandle(handle)
}

function createPipe(api: Win32ProcessBindings, owned: Set<NativePtr>): PipePair {
  const readSlot = allocPtrSlot()
  let writeSlot: NativePtr | undefined
  try {
    writeSlot = allocPtrSlot()
    if (api.createPipe(readSlot, writeSlot, null, 0) === 0) throwLastError(api, 'CreatePipe')
    const read = decodePtr(readSlot)
    const write = decodePtr(writeSlot)
    if (read === null || write === null) {
      closeBestEffort(api, read)
      closeBestEffort(api, write)
      throwLastError(api, 'CreatePipe', 'null pipe handle')
    }
    owned.add(read)
    owned.add(write)
    return { read, write }
  } finally {
    freeNative(writeSlot)
    koffi.free(readSlot)
  }
}

function closeOwned(api: Win32ProcessBindings, owned: Set<NativePtr>, handle: NativePtr): void {
  /* v8 ignore next -- each successfully decoded pipe end is uniquely owned. */
  if (!owned.delete(handle)) return
  api.closeHandle(handle)
}

function closeAllOwned(api: Win32ProcessBindings, owned: Set<NativePtr>): void {
  for (const handle of owned) api.closeHandle(handle)
  owned.clear()
}

function createRestrictedProcess(
  api: Win32ProcessBindings,
  options: RestrictedProcessSpawnOptions,
  commandLine: string,
  creationFlags: number,
  startupInfo: NativePtr,
  processInfo: NativePtr,
): number {
  // The sandbox mutates its process environment before this call. Passing an
  // explicit block through Koffi makes CreateProcessAsUserW reject the request
  // with ERROR_INVALID_PARAMETER, so lpEnvironment remains NULL.
  return api.createProcessAsUserW(
    options.token,
    null,
    commandLine,
    null,
    null,
    1,
    creationFlags,
    null,
    options.cwd,
    startupInfo,
    processInfo,
  )
}

/**
 * Spawn a process with anonymous-pipe stdout/stderr and immediate stdin EOF.
 * @param api - active binding table.
 * @param options - command, cwd, args, and restricted primary token.
 * @returns caller-owned process and pipe read handles.
 */
export function spawnPipedProcess(
  api: Win32ProcessBindings,
  options: RestrictedProcessSpawnOptions,
): SpawnedPipedProcess {
  const owned = new Set<NativePtr>()
  let startupInfo: NativePtr | undefined
  let processInfo: NativePtr | undefined
  try {
    const stdIn = createPipe(api, owned)
    const stdOut = createPipe(api, owned)
    const stdErr = createPipe(api, owned)
    for (const [handle, label] of [
      [stdIn.read, 'stdin read end'],
      [stdOut.write, 'stdout write end'],
      [stdErr.write, 'stderr write end'],
    ] as const) {
      if (api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, abi.HANDLE_FLAG_INHERIT) === 0) {
        throwLastError(api, 'SetHandleInformation', label)
      }
    }
    startupInfo = allocStartupInfo()
    encodeStartupInfo(startupInfo, {
      cb: abi.STARTUPINFOW_SIZE,
      dwFlags: abi.STARTF_USESTDHANDLES,
      hStdInput: stdIn.read,
      hStdOutput: stdOut.write,
      hStdError: stdErr.write,
    })
    processInfo = allocProcessInfo()
    const created = createRestrictedProcess(
      api,
      options,
      buildCommandLine(options.command, options.args),
      0,
      startupInfo,
      processInfo,
    )
    if (created === 0) {
      const win32Code = api.getLastError()
      throwWin32(api, 'CreateProcessAsUserW', win32Code, `command: ${options.command}, cwd: ${options.cwd}`)
    }
    const info = decodeProcessInfo(processInfo)
    if (info.hProcess === null || info.hThread === null) {
      if (info.hProcess !== null) api.terminateProcess(info.hProcess, 1)
      closeBestEffort(api, info.hThread)
      closeBestEffort(api, info.hProcess)
      throw new Error(`CreateProcessAsUserW succeeded but returned null process/thread handles (pid ${info.dwProcessId})`)
    }
    closeOwned(api, owned, stdIn.read)
    closeOwned(api, owned, stdIn.write)
    closeOwned(api, owned, stdOut.write)
    closeOwned(api, owned, stdErr.write)
    closeBestEffort(api, info.hThread)
    owned.delete(stdOut.read)
    owned.delete(stdErr.read)
    return {
      pid: info.dwProcessId,
      process: info.hProcess,
      stdoutRead: stdOut.read,
      stderrRead: stdErr.read,
    }
  } catch (error) {
    closeAllOwned(api, owned)
    throw error
  } finally {
    freeNative(processInfo)
    freeNative(startupInfo)
  }
}

/**
 * Drain one anonymous pipe until the writer closes it.
 * @param api - active binding table.
 * @param handle - caller-owned pipe read end.
 * @returns complete bytes read before EOF; the handle is always closed.
 * @throws when a Win32 pipe operation fails.
 */
export async function drainPipe(
  api: Win32ProcessBindings,
  handle: NativePtr,
): Promise<Buffer> {
  const chunks: Buffer[] = []
  let countSlot: NativePtr | undefined
  try {
    countSlot = allocUint32()
    for (;;) {
      const peeked = api.peekNamedPipe(handle, null, 0, null, countSlot, null)
      if (peeked === 0) {
        const win32Code = api.getLastError()
        if (win32Code === abi.ERROR_BROKEN_PIPE || win32Code === abi.ERROR_NO_DATA) break
        throwLastError(api, 'PeekNamedPipe', `drain failure after ${chunks.length} chunk(s)`)
      }
      const available = decodeUint32(countSlot)
      if (available > 0) {
        const chunk = Buffer.alloc(available)
        if (api.readFile(handle, chunk, chunk.length, countSlot, null) === 0) {
          throwLastError(api, 'ReadFile', `drain failure after ${chunks.length} chunk(s)`)
        }
        chunks.push(chunk.subarray(0, decodeUint32(countSlot)))
      }
      await new Promise<void>(resolve => setTimeout(resolve, 1))
    }
    return Buffer.concat(chunks)
  } finally {
    freeNative(countSlot)
    api.closeHandle(handle)
  }
}

/**
 * Wait for a process and always close its handle.
 * @param api - active binding table.
 * @param process - caller-owned process handle.
 * @returns direct process exit code.
 */
export function waitForProcessExit(api: Win32ProcessBindings, process: NativePtr): number {
  let exitCodeSlot: NativePtr | undefined
  try {
    if (api.waitForSingleObject(process, abi.INFINITE) === 0xFFFFFFFF) {
      throwLastError(api, 'WaitForSingleObject')
    }
    exitCodeSlot = allocUint32()
    if (api.getExitCodeProcess(process, exitCodeSlot) === 0) throwLastError(api, 'GetExitCodeProcess')
    return decodeUint32(exitCodeSlot)
  } finally {
    freeNative(exitCodeSlot)
    api.closeHandle(process)
  }
}

function createKillOnCloseJob(api: Win32ProcessBindings): NativePtr {
  const job = api.createJobObjectW(null, null)
  if (isNullPtr(job)) throwLastError(api, 'CreateJobObjectW')
  const information = Buffer.alloc(abi.JOBOBJECT_EXTENDED_LIMIT_SIZE)
  information.writeUInt32LE(
    abi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    abi.JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET,
  )
  if (api.setInformationJobObject(
    job,
    abi.JobObjectExtendedLimitInformation,
    information,
    information.length,
  ) === 0) {
    const win32Code = api.getLastError()
    api.closeHandle(job)
    throwWin32(api, 'SetInformationJobObject', win32Code)
  }
  return job
}

/**
 * Spawn suspended, assign the child to a kill-on-close Job, then resume it.
 * @param api - active binding table.
 * @param options - command, cwd, args, and restricted primary token.
 * @returns caller-owned process and Job handles after successful resume.
 * @remarks Node clears stdio handle inheritability at startup through
 * uv_disable_stdio_inheritance. This operation temporarily restores the bits
 * required by STARTF_USESTDHANDLES. Restoring them afterward is best-effort:
 * failure must not replace the already-created child's outcome.
 */
export function spawnInheritedJobProcess(
  api: Win32ProcessBindings,
  options: RestrictedProcessSpawnOptions,
): SpawnedJobProcess {
  const job = createKillOnCloseJob(api)
  const getStdHandle = (selector: number, label: string): NativePtr => {
    const handle = api.getStdHandle(selector)
    if (!isNullPtr(handle)) return handle
    const win32Code = api.getLastError()
    api.closeHandle(job)
    throwWin32(api, 'GetStdHandle', win32Code, `null ${label} handle`)
  }
  const stdIn = getStdHandle(abi.STD_INPUT_HANDLE, 'stdin')
  const stdOut = getStdHandle(abi.STD_OUTPUT_HANDLE, 'stdout')
  const stdErr = getStdHandle(abi.STD_ERROR_HANDLE, 'stderr')
  const enabled: NativePtr[] = []
  let startupInfo: NativePtr | undefined
  let processInfo: NativePtr | undefined
  let created = 0
  let createFailureCode = 0
  try {
    for (const [handle, label] of [
      [stdIn, 'stdin'],
      [stdOut, 'stdout'],
      [stdErr, 'stderr'],
    ] as const) {
      if (api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, abi.HANDLE_FLAG_INHERIT) === 0) {
        throwLastError(api, 'SetHandleInformation', `${label} (enable inherit)`)
      }
      enabled.push(handle)
    }
    startupInfo = allocStartupInfo()
    encodeStartupInfo(startupInfo, {
      cb: abi.STARTUPINFOW_SIZE,
      dwFlags: abi.STARTF_USESTDHANDLES,
      hStdInput: stdIn,
      hStdOutput: stdOut,
      hStdError: stdErr,
    })
    processInfo = allocProcessInfo()
    created = createRestrictedProcess(
      api,
      options,
      buildCommandLine(options.command, options.args),
      abi.CREATE_SUSPENDED,
      startupInfo,
      processInfo,
    )
    if (created === 0) createFailureCode = api.getLastError()
  } catch (error) {
    freeNative(processInfo)
    api.closeHandle(job)
    throw error
  } finally {
    freeNative(startupInfo)
    for (const handle of enabled) {
      // The runner spawns nothing else; cleanup failure must not mask the child.
      api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, 0)
    }
  }
  if (created === 0) {
    freeNative(processInfo)
    api.closeHandle(job)
    throwWin32(
      api,
      'CreateProcessAsUserW',
      createFailureCode,
      `command: ${options.command}, cwd: ${options.cwd}`,
    )
  }
  let info: ReturnType<typeof decodeProcessInfo>
  try {
    info = decodeProcessInfo(processInfo)
  } finally {
    freeNative(processInfo)
  }
  if (info.hProcess === null || info.hThread === null) {
    if (info.hProcess !== null) api.terminateProcess(info.hProcess, 1)
    api.closeHandle(job)
    closeBestEffort(api, info.hThread)
    closeBestEffort(api, info.hProcess)
    throw new Error(`CreateProcessAsUserW succeeded but returned null process/thread handles (pid ${info.dwProcessId})`)
  }
  if (api.assignProcessToJobObject(job, info.hProcess) === 0) {
    const win32Code = api.getLastError()
    api.terminateProcess(info.hProcess, 1)
    closeBestEffort(api, info.hThread)
    closeBestEffort(api, info.hProcess)
    api.closeHandle(job)
    throwWin32(api, 'AssignProcessToJobObject', win32Code, `pid ${info.dwProcessId}`)
  }
  if (api.resumeThread(info.hThread) === 0xFFFFFFFF) {
    const win32Code = api.getLastError()
    closeBestEffort(api, info.hThread)
    closeBestEffort(api, info.hProcess)
    api.closeHandle(job)
    throwWin32(api, 'ResumeThread', win32Code, `pid ${info.dwProcessId}`)
  }
  closeBestEffort(api, info.hThread)
  return { pid: info.dwProcessId, process: info.hProcess, job }
}
