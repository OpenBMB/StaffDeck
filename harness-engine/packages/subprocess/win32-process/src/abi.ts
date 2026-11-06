/** Generic Win32 process, stdio, and Job Object constants verified on x64. */

/** STARTUPINFOW uses the standard input, output, and error handles. */
export const STARTF_USESTDHANDLES = 0x00000100
/** HandleInformation flag that permits child inheritance. */
export const HANDLE_FLAG_INHERIT = 0x1
/** Infinite WaitForSingleObject timeout. */
export const INFINITE = 0xFFFFFFFF
/** CreateProcess flag that prevents user code from running before resume. */
export const CREATE_SUSPENDED = 0x4
/** GetStdHandle selector for standard input. */
export const STD_INPUT_HANDLE = -10
/** GetStdHandle selector for standard output. */
export const STD_OUTPUT_HANDLE = -11
/** GetStdHandle selector for standard error. */
export const STD_ERROR_HANDLE = -12
/** FormatMessage reads the operating system message table. */
export const FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
/** FormatMessage leaves insertion placeholders uninterpreted. */
export const FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200
/** Win32 code reporting a caller-provided buffer is too small. */
export const ERROR_INSUFFICIENT_BUFFER = 122
/** Win32 code reporting that the other pipe end closed. */
export const ERROR_BROKEN_PIPE = 109
/** Win32 code reporting that a pipe has no remaining data. */
export const ERROR_NO_DATA = 232
/** Job limit that terminates every member when the final Job handle closes. */
export const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
/** SetInformationJobObject class for JOBOBJECT_EXTENDED_LIMIT_INFORMATION. */
export const JobObjectExtendedLimitInformation = 9
/** x64 JOBOBJECT_EXTENDED_LIMIT_INFORMATION byte size. */
export const JOBOBJECT_EXTENDED_LIMIT_SIZE = 144
/** Byte offset of BasicLimitInformation.LimitFlags in the extended Job record. */
export const JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET = 16
/** x64 STARTUPINFOW byte size verified by the native probe. */
export const STARTUPINFOW_SIZE = 104
/** x64 PROCESS_INFORMATION byte size verified by the native probe. */
export const PROCESS_INFORMATION_SIZE = 24
