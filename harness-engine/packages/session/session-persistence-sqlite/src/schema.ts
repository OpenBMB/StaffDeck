/**
 * SQLite schema ownership and durable-row validation.
 * @module @deepseek-ai/dsh-session-persistence-sqlite/schema
 */

import { randomUUID } from 'node:crypto'
import { isAbsolute } from 'node:path'
import { performance } from 'node:perf_hooks'
import type { DatabaseSync } from 'node:sqlite'
import { setTimeout as delay } from 'node:timers/promises'
import { brandString } from '@deepseek-ai/dsh-brand'
import {
  type SessionHeader,
  type SessionId,
} from '@deepseek-ai/dsh-session'
import { sql } from './sql.ts'

/** Current physical-record schema with packed and compressed event rows. */
export const SCHEMA_VERSION = 20
/** Application id reserved for DeepSeek Harness SQLite session databases. */
export const SESSION_PERSISTENCE_SQLITE_APPLICATION_ID = 0x44534850

/** A materialized session's metadata and monotonic revision. */
export interface SessionRow {
  readonly id: string
  readonly version: number
  readonly created_at: number
  readonly cwd: string | null
  readonly parent_session: string | null
  readonly seed_length: number | null
  readonly origin: 'subagent' | null
  readonly incarnation: string
  readonly revision: number
  readonly delegation_depth: number | null
  readonly agent_preset: string | null
}

/** One physical event row; packed rows may represent multiple logical events. */
export interface EventRow {
  readonly seq: number
  readonly type: string
  readonly time: number
  readonly data: string | Uint8Array
  readonly source_event_seqs: Uint8Array | null
  readonly surface_op: string | null
  readonly ignorable: number | null
}

/** Durable journal modes accepted by the backend. */
export type JournalMode = 'wal' | 'delete' | 'truncate' | 'persist'

interface SchemaObjectRow {
  readonly type: string
  readonly name: string
  readonly tbl_name: string
  readonly sql: string
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/iu
const JOURNAL_BUSY_RETRY_INTERVAL_MS = 10
type DatabaseSyncConstructor = typeof import('node:sqlite')['DatabaseSync']

/**
 * Open and validate a SQLite session database.
 * @param Database - lazily imported Node SQLite constructor.
 * @param path - SQLite path, including `:memory:`.
 * @param journalMode - validated journal pragma.
 * @param busyTimeoutMs - validated maximum wait for a competing SQLite lock.
 * @returns the configured database handle.
 * @throws when connection settings, schema ownership, or SQLite setup cannot be validated.
 */
export async function openDatabase(
  Database: DatabaseSyncConstructor,
  path: string,
  journalMode: JournalMode,
  busyTimeoutMs: number,
): Promise<DatabaseSync> {
  const deadline = performance.now() + busyTimeoutMs
  const db = new Database(path, { timeout: busyTimeoutMs })
  try {
    configureConnectionSecurity(db, path)
    configureDatabase(Database, db, path)
    await selectJournalMode(db, path, journalMode, deadline)
    configureDurability(db, path)
    return db
  } catch (error: unknown) {
    db.close()
    throw error
  }
}

function configureConnectionSecurity(db: DatabaseSync, path: string): void {
  db.exec(sql('trusted-schema-off'))
  const trustedSchema = integerField(db.prepare(sql('select-trusted-schema')).get(), 'trusted_schema')
  /* v8 ignore next 3 -- supported SQLite versions return the fixed setting. */
  if (trustedSchema !== 0) {
    throw new Error(`session database at "${path}" retained trusted_schema=${trustedSchema}, expected 0`)
  }
  db.exec(sql('mmap-off'))
  if (path === ':memory:') return
  const mmapSize = integerField(db.prepare(sql('select-mmap-size')).get(), 'mmap_size')
  /* v8 ignore next 3 -- supported file-backed SQLite connections return the fixed setting. */
  if (mmapSize !== 0) {
    throw new Error(`session database at "${path}" retained mmap_size=${mmapSize}, expected 0`)
  }
}

function configureDatabase(
  Database: DatabaseSyncConstructor,
  db: DatabaseSync,
  path: string,
): void {
  db.exec(sql('page-size'))
  db.exec(sql('foreign-keys-on'))
  let began = false
  try {
    db.exec(sql('begin-immediate'))
    began = true
    const onDisk = integerField(db.prepare(sql('select-user-version')).get(), 'user_version')
    const applicationId = integerField(db.prepare(sql('select-application-id')).get(), 'application_id')
    const userObjectCount = integerField(db.prepare(sql('select-user-object-count')).get(), 'count')
    if (onDisk === 0 && (applicationId !== 0 || userObjectCount > 0)) {
      throw new Error(`session database at "${path}" has an unversioned schema or application identity`)
    }
    if (onDisk !== 0 && onDisk !== SCHEMA_VERSION) {
      throw new Error(
        `session database at "${path}" has schema version ${onDisk}, incompatible with this build (${SCHEMA_VERSION})`,
      )
    }
    if (onDisk !== 0 && applicationId !== SESSION_PERSISTENCE_SQLITE_APPLICATION_ID) {
      throw new Error(
        `session database at "${path}" has application id ${applicationId}, expected ${SESSION_PERSISTENCE_SQLITE_APPLICATION_ID}`,
      )
    }
    if (onDisk === 0) initializeDatabase(db)
    validateRequiredSchema(Database, db, path)
    db.exec(sql('commit'))
    began = false
  } catch (error: unknown) {
    /* v8 ignore else -- a failed begin leaves no transaction to roll back. */
    if (began) {
      /* v8 ignore next 5 -- retain the original ownership failure if rollback fails too. */
      try {
        db.exec(sql('rollback'))
      } catch {
        // The original database-ownership failure remains actionable.
      }
    }
    throw error
  }
}

async function selectJournalMode(
  db: DatabaseSync,
  path: string,
  journalMode: JournalMode,
  deadline: number,
): Promise<void> {
  let result: unknown
  while (true) {
    try {
      result = db.prepare(sql(journalResource(journalMode))).get()
      break
    } catch (error: unknown) {
      const remainingMs = Math.max(0, Math.ceil(deadline - performance.now()))
      if (!isSqliteBusy(error) || remainingMs === 0) throw error
      await delay(Math.min(JOURNAL_BUSY_RETRY_INTERVAL_MS, remainingMs))
      if (performance.now() >= deadline) throw error
    }
  }
  const selected = stringField(result, 'journal_mode').toLowerCase()
  const expected = path === ':memory:' ? 'memory' : journalMode
  /* v8 ignore next 3 -- SQLite returns the selected mode from these fixed, valid pragmas. */
  if (selected !== expected) {
    throw new Error(`session database at "${path}" selected journal mode ${selected}, expected ${expected}`)
  }
}

function configureDurability(db: DatabaseSync, path: string): void {
  db.exec(sql('synchronous-full'))
  const synchronous = integerField(db.prepare(sql('select-synchronous')).get(), 'synchronous')
  /* v8 ignore next 3 -- supported SQLite versions return the fixed setting. */
  if (synchronous !== 2) {
    throw new Error(`session database at "${path}" retained synchronous=${synchronous}, expected FULL (2)`)
  }
}

function isSqliteBusy(error: unknown): boolean {
  return typeof error === 'object'
    && error !== null
    && Reflect.get(error, 'errcode') === 5
}

function journalResource(mode: JournalMode):
  | 'journal-mode-wal'
  | 'journal-mode-delete'
  | 'journal-mode-truncate'
  | 'journal-mode-persist' {
  switch (mode) {
    case 'wal': return 'journal-mode-wal'
    case 'delete': return 'journal-mode-delete'
    case 'truncate': return 'journal-mode-truncate'
    case 'persist': return 'journal-mode-persist'
  }
}

function initializeDatabase(db: DatabaseSync): void {
  db.exec(sql('schema'))
  db.prepare(sql('insert-persistence-state')).run(randomUUID())
  db.exec(sql('set-application-id'))
  db.exec(sql('set-user-version-20'))
}

let canonicalSchema: readonly SchemaObjectRow[] | undefined

function expectedSchema(Database: DatabaseSyncConstructor): readonly SchemaObjectRow[] {
  if (canonicalSchema !== undefined) return canonicalSchema
  const reference = new Database(':memory:')
  try {
    reference.exec(sql('foreign-keys-on'))
    reference.exec(sql('schema'))
    canonicalSchema = schemaObjects(reference)
    return canonicalSchema
  } finally {
    reference.close()
  }
}

function schemaObjects(db: DatabaseSync): SchemaObjectRow[] {
  return db.prepare(sql('select-schema-objects')).all().map((value) => {
    const row = record(value, 'schema object')
    return {
      type: stringField(row, 'type'),
      name: stringField(row, 'name'),
      tbl_name: stringField(row, 'tbl_name'),
      sql: normalizeSql(stringField(row, 'sql')),
    }
  })
}

function normalizeSql(value: string): string {
  return value.replaceAll(/\s+/gu, ' ').trim()
}

function validateRequiredSchema(
  Database: DatabaseSyncConstructor,
  db: DatabaseSync,
  path: string,
): void {
  if (JSON.stringify(schemaObjects(db)) !== JSON.stringify(expectedSchema(Database))) {
    throw new Error(`session database at "${path}" does not contain the required schema objects`)
  }
}

/**
 * Recheck schema ownership inside the caller's mutation transaction.
 * @param Database - constructor used to validate the canonical schema.
 * @param db - open owned database with an active immediate transaction.
 * @param path - database location used in ownership diagnostics.
 * @throws when another writer changed the application identity, schema, or version.
 */
export function validateSchemaForMutation(
  Database: DatabaseSyncConstructor,
  db: DatabaseSync,
  path: string,
): void {
  const version = integerField(db.prepare(sql('select-user-version')).get(), 'user_version')
  const applicationId = integerField(db.prepare(sql('select-application-id')).get(), 'application_id')
  if (applicationId !== SESSION_PERSISTENCE_SQLITE_APPLICATION_ID) {
    throw new Error(
      `session database application id changed before mutation (expected ${SESSION_PERSISTENCE_SQLITE_APPLICATION_ID}, got ${applicationId})`,
    )
  }
  validateRequiredSchema(Database, db, path)
  if (version !== SCHEMA_VERSION) {
    throw new Error(`session database schema changed before mutation (expected ${SCHEMA_VERSION}, got ${version})`)
  }
}

/**
 * Decode and validate one durable session row.
 * @param value - value returned by SQLite.
 * @returns a validated session row.
 */
export function decodeSessionRow(value: unknown): SessionRow {
  const row = record(value, 'stored session metadata')
  const id = nonemptyStringField(row, 'id')
  const version = safeIntegerField(row, 'version')
  const cwd = nullableStringField(row, 'cwd')
  if (cwd !== null && !isAbsolute(cwd)) throw new Error('stored session cwd must be absolute')
  const parent = nullableStringField(row, 'parent_session')
  const origin = nullableStringField(row, 'origin')
  if (origin !== null && origin !== 'subagent') throw new Error('stored session origin must be subagent or null')
  const incarnation = nonemptyStringField(row, 'incarnation')
  if (!UUID.test(incarnation)) throw new Error('stored session incarnation must be a UUID')
  return {
    id,
    version,
    created_at: nonnegativeSafeIntegerField(row, 'created_at'),
    cwd,
    parent_session: parent,
    seed_length: nullableNonnegativeSafeIntegerField(row, 'seed_length'),
    origin,
    delegation_depth: nullableNonnegativeSafeIntegerField(row, 'delegation_depth'),
    agent_preset: nullableStringField(row, 'agent_preset'),
    incarnation,
    revision: nonnegativeSafeIntegerField(row, 'revision'),
  }
}

/**
 * Decode and validate one durable event row before JSON interpretation.
 * @param value - value returned by SQLite.
 * @returns a validated physical event row.
 */
export function decodeEventRow(value: unknown): EventRow {
  const row = record(value, 'stored event')
  const ignorable = nullableSafeIntegerField(row, 'ignorable')
  if (ignorable !== null && ignorable !== 0 && ignorable !== 1) {
    throw new Error('stored event ignorable must be 0, 1, or null')
  }
  return {
    seq: nonnegativeSafeIntegerField(row, 'seq'),
    type: nonemptyStringField(row, 'type'),
    time: safeIntegerField(row, 'time'),
    data: stringOrBlobField(row, 'data'),
    source_event_seqs: nullableBlobField(row, 'source_event_seqs'),
    surface_op: nullableStringField(row, 'surface_op'),
    ignorable,
  }
}

/**
 * Validate the singleton identity read from durable storage.
 * @param value - value returned by SQLite.
 * @returns the UUID store identity.
 */
export function decodeStoreIdentity(value: unknown): string {
  const identity = nonemptyStringField(value, 'store_id')
  if (!UUID.test(identity)) throw new Error('stored store_id must be a UUID')
  return identity
}

/**
 * Reconstruct an immutable session header from a validated metadata row.
 * @param row - validated stored metadata row.
 * @returns the session header.
 */
export function rowToMeta(row: SessionRow): SessionHeader {
  return {
    version: row.version,
    id: brandString<SessionId>(row.id),
    createdAt: row.created_at,
    ...row.cwd === null ? {} : { cwd: row.cwd },
    ...row.parent_session === null ? {} : { parentSession: brandString<SessionId>(row.parent_session) },
    ...row.seed_length === null ? {} : { seedLength: row.seed_length },
    ...row.origin === null ? {} : { origin: row.origin },
    ...row.delegation_depth === null ? {} : { delegationDepth: row.delegation_depth },
    ...row.agent_preset === null ? {} : { agentPreset: row.agent_preset },
  }
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null) throw new Error(`${label} must be an object`)
  return value as Record<string, unknown>
}

function stringField(value: unknown, key: string): string {
  const field = record(value, 'SQLite row')[key]
  if (typeof field !== 'string') throw new Error(`stored ${key} must be a string`)
  return field
}

function nonemptyStringField(value: unknown, key: string): string {
  const field = stringField(value, key)
  if (field.length === 0) throw new Error(`stored ${key} must not be empty`)
  return field
}

function nullableStringField(value: unknown, key: string): string | null {
  const field = record(value, 'SQLite row')[key]
  if (field === null) return null
  if (typeof field !== 'string') throw new Error(`stored ${key} must be a string or null`)
  return field
}

function stringOrBlobField(value: unknown, key: string): string | Uint8Array {
  const field = record(value, 'SQLite row')[key]
  if (typeof field === 'string' || field instanceof Uint8Array) return field
  throw new Error(`stored ${key} must be a string or blob`)
}

function nullableBlobField(value: unknown, key: string): Uint8Array | null {
  const field = record(value, 'SQLite row')[key]
  if (field === null || field instanceof Uint8Array) return field
  throw new Error(`stored ${key} must be a blob or null`)
}

function integerField(value: unknown, key: string): number {
  const field = record(value, 'SQLite row')[key]
  if (!Number.isSafeInteger(field)) throw new Error(`stored ${key} must be a safe integer`)
  return field as number
}

function safeIntegerField(value: unknown, key: string): number {
  return integerField(value, key)
}

function nonnegativeSafeIntegerField(value: unknown, key: string): number {
  const field = integerField(value, key)
  if (field < 0) throw new Error(`stored ${key} must be non-negative`)
  return field
}

function nullableSafeIntegerField(value: unknown, key: string): number | null {
  const field = record(value, 'SQLite row')[key]
  if (field === null) return null
  if (!Number.isSafeInteger(field)) throw new Error(`stored ${key} must be a safe integer or null`)
  return field as number
}

function nullableNonnegativeSafeIntegerField(value: unknown, key: string): number | null {
  const field = nullableSafeIntegerField(value, key)
  if (field !== null && field < 0) throw new Error(`stored ${key} must be non-negative or null`)
  return field
}
