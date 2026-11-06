/** ACL/token-specific Win32 constants. */

/** OpenProcess access required to query the current process token. */
export const PROCESS_QUERY_INFORMATION = 0x0400
/** Token right required by CreateProcessAsUserW. */
export const TOKEN_ASSIGN_PRIMARY = 0x0001
/** Token right required by DuplicateTokenEx. */
export const TOKEN_DUPLICATE = 0x0002
/** Token right required to read token information. */
export const TOKEN_QUERY = 0x0008
/** Token right required to replace the token default DACL. */
export const TOKEN_ADJUST_DEFAULT = 0x0080
/** Group attribute identifying the token logon SID. */
export const SE_GROUP_LOGON_ID = 0xC0000000
/** Standard-rights portion excluded from the write capability grant. */
export const STANDARD_RIGHTS_WRITE = 0x00020000
/** Generic file write access bits. */
export const FILE_GENERIC_WRITE = 0x00120116
/** Delete or rename an object. */
export const DELETE = 0x00010000
/** Delete or rename a directory child. */
export const FILE_DELETE_CHILD = 0x0040
/**
 * Capability-SID access mask granting write, delete, and child deletion.
 * WRITE_DAC and WRITE_OWNER stay excluded so a confined child cannot rewrite
 * DACLs or take ownership to escape the allowlist.
 */
export const GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE
/** Full access used in the restricted token default DACL. */
export const FILE_ALL_ACCESS = 0x1F01FF
/** CreateRestrictedToken flag that disables maximum privileges. */
export const DISABLE_MAX_PRIVILEGE = 0x1
/** CreateRestrictedToken limited-user flag. */
export const LUA_TOKEN = 0x4
/** Restrict write access to the listed restricting SIDs. */
export const WRITE_RESTRICTED = 0x8
/** WELL_KNOWN_SID_TYPE value for Everyone. */
export const WinWorldSid = 1
/** TOKEN_INFORMATION_CLASS value for token groups. */
export const TokenGroups = 2
/** TOKEN_INFORMATION_CLASS value for the token default DACL. */
export const TokenDefaultDacl = 6
/** SECURITY_INFORMATION flag selecting the DACL. */
export const DACL_SECURITY_INFORMATION = 0x00000004
/** SE_OBJECT_TYPE value for filesystem objects. */
export const SE_FILE_OBJECT = 1
/** TRUSTEE_TYPE value used when trustee classification is unknown. */
export const TRUSTEE_IS_UNKNOWN = 0
/** TRUSTEE_FORM value indicating a SID pointer. */
export const TRUSTEE_IS_SID = 0
/** Trustee record has no chained trustee. */
export const NO_MULTIPLE_TRUSTEE = 0
/** EXPLICIT_ACCESS mode that grants access. */
export const GRANT_ACCESS = 1
/** EXPLICIT_ACCESS mode that revokes access. */
export const REVOKE_ACCESS = 4
/** ACE inheritance flags for child containers and objects. */
export const SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3
/** Legacy Win32 maximum path character count used by GetTempPathW. */
export const MAX_PATH = 260
/** Successful Win32 status code. */
export const ERROR_SUCCESS = 0
/** Win32 error reported when an immediate byte-range lock cannot be obtained. */
export const ERROR_LOCK_VIOLATION = 33
/** Generic read access bit. */
export const GENERIC_READ = 0x80000000
/** Generic write access bit. */
export const GENERIC_WRITE = 0x40000000
/** CreateFile share-read flag. */
export const FILE_SHARE_READ = 0x00000001
/** CreateFile share-write flag. */
export const FILE_SHARE_WRITE = 0x00000002
/** CreateFile share-delete flag. */
export const FILE_SHARE_DELETE = 0x00000004
/** CreateFile disposition that opens or creates the file. */
export const OPEN_ALWAYS = 4
/** LockFileEx exclusive-lock flag. */
export const LOCKFILE_EXCLUSIVE_LOCK = 0x2
/** LockFileEx immediate-failure flag. */
export const LOCKFILE_FAIL_IMMEDIATELY = 0x1
/** ACE type for an allowed-access entry. */
export const ACCESS_ALLOWED_ACE_TYPE = 0
/** Maximum SID sub-authority count. */
export const SID_MAX_SUB_AUTHORITIES = 15
/** ACE flag marking inherited entries. */
export const INHERITED_ACE = 0x10
/** Maximum SID allocation size in bytes. */
export const SECURITY_MAX_SID_SIZE = 68
/** x64 SID_AND_ATTRIBUTES byte size. */
export const SID_AND_ATTRIBUTES_SIZE = 16
/** x64 TOKEN_GROUPS offset of the first group entry. */
export const TOKEN_GROUPS_OFFSET = 8
/** x64 EXPLICIT_ACCESS_W byte size. */
export const EXPLICIT_ACCESS_W_SIZE = 48
/** x64 offset of TRUSTEE_W inside EXPLICIT_ACCESS_W. */
export const TRUSTEE_W_OFFSET = 16
/** x64 offset of ptstrName inside TRUSTEE_W. */
export const TRUSTEE_W_PTSTRNAME_OFFSET = 24
