/**
 * SQLite storage primitives: transactional append-batch packing, physical
 * reads, schema validation, revisions, repair, and lifecycle closure.
 * @module @deepseek-ai/dsh-session-persistence-sqlite/store
 */

import { randomUUID } from 'node:crypto'
import { statSync } from 'node:fs'
import { lstat, mkdir, open } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import type { DatabaseSync, StatementSync } from 'node:sqlite'
import {
  type SessionEvent,
  type SessionHeader,
  type SessionId,
} from '@deepseek-ai/dsh-session'
import {
  SessionPersistenceRevision,
  type PersistenceBackend,
  type SessionPersistenceRevision as PersistenceRevision,
  type SessionPersistenceSnapshot,
  type StoredPrefix,
  type StoredSuffix,
} from '@deepseek-ai/dsh-session-persistence'
import {
  MAX_PACKED_ROW_MEMBERS,
  packChunkRuns,
} from './codec.ts'
import {
  bindRecord,
  decodeRow,
  scanRows,
  type BoundRecord,
} from './compression.ts'
import {
  type EventRow,
  type JournalMode,
  decodeEventRow,
  decodeSessionRow,
  decodeStoreIdentity,
  openDatabase,
  validateSchemaForMutation,
  rowToMeta,
  type SessionRow,
} from './schema.ts'
import { sql } from './sql.ts'

/** Storage options resolved by the service provider. */
export interface SqliteStoreOptions {
  readonly path: string
  readonly journalMode: JournalMode
  readonly busyTimeoutMs: number
}

/** SQLite implementation of the coordinator's physical backend hooks. */
export class SqliteStore implements PersistenceBackend<number> {
  readonly name = 'session-persistence-sqlite'
  private db!: DatabaseSync
  private databaseConstructor!: typeof import('node:sqlite')['DatabaseSync']
  private storeIdentity!: string
  private databasePath!: string
  private opened = false
  private pathReady: Promise<void> | undefined
  private ready: Promise<void> | undefined

  constructor(private readonly options: SqliteStoreOptions) {}

  /**
   * Validate filesystem ownership without importing or opening Node SQLite.
   * @returns settlement of the store's one path-validation operation.
   */
  validatePath(): Promise<void> {
    this.pathReady ??= this.preparePath(this.options.path)
    return this.pathReady
  }

  /**
   * Lazily open and validate the database on first persistence use.
   * @returns settlement of the store's one database-open operation.
   */
  open(): Promise<void> {
    this.ready ??= this.openDb()
    return this.ready
  }

  private async preparePath(path: string): Promise<void> {
    const actual = path === ':memory:' ? path : resolve(path)
    if (actual !== ':memory:') {
      await mkdir(dirname(actual), { recursive: true, mode: 0o700 })
      await validateParentDirectory(dirname(actual))
      await validateDatabaseFileIfPresent(actual)
    }
    this.databasePath = actual
  }

  private async openDb(): Promise<void> {
    await this.validatePath()
    if (this.databasePath !== ':memory:') {
      await createDatabaseFile(this.databasePath)
      await validateDatabaseFile(this.databasePath)
    }
    const { DatabaseSync } = await loadNodeSqlite()
    this.databaseConstructor = DatabaseSync
    this.db = await openDatabase(
      DatabaseSync,
      this.databasePath,
      this.options.journalMode,
      this.options.busyTimeoutMs,
    )
    try {
      const row = this.db.prepare(sql('select-store-id')).get()
      if (row === undefined) {
        throw new Error(`session database at "${this.databasePath}" has no valid store identity`)
      }
      let storeId: string
      try {
        storeId = decodeStoreIdentity(row)
      } catch (error: unknown) {
        throw new Error(`session database at "${this.databasePath}" has no valid store identity`, { cause: error })
      }
      if (this.databasePath === ':memory:') {
        this.storeIdentity = `memory:store:${storeId}`
      } else {
        const identity = statSync(this.databasePath, { bigint: true })
        this.storeIdentity = `file:${identity.dev}:${identity.ino}:${identity.birthtimeNs}:store:${storeId}`
      }
      this.opened = true
    } catch (error: unknown) {
      this.db.close()
      throw error
    }
  }

  async loadStored(id: SessionId, signal?: AbortSignal): Promise<StoredPrefix<number> | undefined> {
    await this.observe(signal)
    const snapshot = this.readTransaction(() => {
      const row = this.rowFor(id)
      if (row === undefined) return undefined
      const eventRows = this.db.prepare(sql('select-events')).all(this.sessionKey(id)).map(decodeEventRow)
      return { row, eventRows }
    })
    signal?.throwIfAborted()
    if (snapshot === undefined) return undefined
    const scanned = scanRows(snapshot.eventRows)
    return {
      meta: rowToMeta(snapshot.row),
      events: scanned.preserved,
      revision: sqliteRevision(this.storeIdentity, snapshot.row),
      ...scanned.tornFrom === undefined ? {} : { tornMarker: scanned.tornFrom },
    }
  }

  async readStoredRevision(id: SessionId, signal?: AbortSignal): Promise<PersistenceRevision | undefined> {
    await this.observe(signal)
    const row = this.rowFor(id)
    signal?.throwIfAborted()
    return row === undefined ? undefined : sqliteRevision(this.storeIdentity, row)
  }

  async loadStoredFrom(id: SessionId, fromSeq: number, signal?: AbortSignal): Promise<StoredSuffix | undefined> {
    await this.observe(signal)
    const snapshot = this.readTransaction(() => {
      const row = this.rowFor(id)
      if (row === undefined) return undefined
      return { row, ...this.physicalSpanFrom(this.sessionKey(id), fromSeq) }
    })
    signal?.throwIfAborted()
    if (snapshot === undefined) return undefined
    const { preserved } = scanRows(snapshot.eventRows, snapshot.base)
    return { meta: rowToMeta(snapshot.row), events: preserved.filter(event => event.seq >= fromSeq) }
  }

  async appendBatch(
    meta: SessionHeader,
    events: readonly SessionEvent[],
    isMaterialized: boolean,
  ): Promise<void> {
    await this.open()
    if (events.length === 0) return
    this.db.exec(sql('begin-immediate'))
    try {
      validateSchemaForMutation(this.databaseConstructor, this.db, this.databasePath)
      const sessionKey = isMaterialized ? this.sessionKey(meta.id) : this.writeRow(meta)
      const tailRows = this.tailRows(sessionKey)
      const currentLast = this.logicalLastEvent(meta.id, tailRows)
      const expected = currentLast === undefined ? 0 : currentLast.seq + 1
      const first = events[0] as SessionEvent
      if (first.seq !== expected) {
        throw new Error(`session ${meta.id} append starts at seq ${first.seq}, stored next seq is ${expected}`)
      }

      const insert = this.insertStatement()
      for (const record of packChunkRuns(events)) this.insertRecord(insert, sessionKey, bindRecord(record))
      this.incrementRevision(meta.id)
      this.db.exec(sql('commit'))
    } catch (error: unknown) {
      this.rollback(error, 'append')
    }
  }

  async materializeHeader(meta: SessionHeader): Promise<void> {
    await this.open()
    this.db.exec(sql('begin-immediate'))
    try {
      validateSchemaForMutation(this.databaseConstructor, this.db, this.databasePath)
      this.writeRow(meta)
      this.db.exec(sql('commit'))
    } catch (error: unknown) {
      /* v8 ignore next -- validate/write failure uses the same transaction rollback path covered by append and repair. */
      this.rollback(error, 'materialize empty session')
    }
  }

  async commitRepair(
    meta: SessionHeader,
    tornMarker: number | undefined,
    closers: readonly SessionEvent[],
  ): Promise<void> {
    await this.open()
    if (tornMarker === undefined && closers.length === 0) return
    this.db.exec(sql('begin-immediate'))
    try {
      validateSchemaForMutation(this.databaseConstructor, this.db, this.databasePath)
      const row = this.rowFor(meta.id)
      if (row === undefined) throw new Error(`session ${meta.id} metadata row is missing`)
      const sessionKey = this.sessionKey(meta.id)
      const currentRows = this.db.prepare(sql('select-events')).all(sessionKey).map(decodeEventRow)
      const current = scanRows(currentRows)
      if (tornMarker !== undefined) {
        if (current.tornFrom !== tornMarker) {
          throw new Error(`session ${meta.id} repair is stale: physical tail no longer starts at seq ${tornMarker}`)
        }
        this.db.prepare(sql('delete-events-from'))
          .run(sessionKey, tornMarker)
      } else if (current.tornFrom !== undefined) {
        throw new Error(`session ${meta.id} repair omitted current torn tail at seq ${current.tornFrom}`)
      }
      if (closers.length > 0) {
        const expected = current.preserved.at(-1)?.seq === undefined
          ? 0
          : (current.preserved.at(-1) as SessionEvent).seq + 1
        if (closers[0]?.seq !== expected) {
          throw new Error(`session ${meta.id} repair is stale: closer starts at seq ${closers[0]?.seq}, stored next seq is ${expected}`)
        }
        const insert = this.insertStatement()
        for (const closer of closers) this.insertRecord(insert, sessionKey, bindRecord(closer))
      }
      this.incrementRevision(meta.id)
      this.db.exec(sql('commit'))
    } catch (error: unknown) {
      this.rollback(error, 'repair')
    }
  }

  async list(signal?: AbortSignal): Promise<SessionHeader[]> {
    await this.observe(signal)
    const rows = this.sessionRows()
    signal?.throwIfAborted()
    return rows.map(rowToMeta)
  }

  /**
   * Return every materialized header with its source-qualified revision.
   * @param signal - optional cancellation before or after the metadata query.
   * @returns stored headers and revisions without loading event rows.
   */
  async listSnapshots(signal?: AbortSignal): Promise<SessionPersistenceSnapshot[]> {
    await this.observe(signal)
    const rows = this.sessionRows()
    signal?.throwIfAborted()
    return rows.map(row => ({
      header: rowToMeta(row),
      revision: sqliteRevision(this.storeIdentity, row),
    }))
  }

  async close(): Promise<void> {
    if (this.ready === undefined) {
      if (this.pathReady !== undefined) await Promise.allSettled([this.pathReady])
      return
    }
    await Promise.allSettled([this.ready])
    if (!this.opened) return
    this.opened = false
    this.db.close()
  }

  private rowFor(id: SessionId): SessionRow | undefined {
    const value = this.db.prepare(sql('select-session')).get(id)
    return value === undefined ? undefined : decodeSessionRow(value)
  }

  private sessionKey(id: SessionId): number {
    const row = this.db.prepare(sql('select-session-key')).get(id) as { id: number } | undefined
    if (row === undefined) throw new Error(`session ${id} metadata row is missing`)
    return row.id
  }

  private async observe(signal: AbortSignal | undefined): Promise<void> {
    signal?.throwIfAborted()
    await this.open()
    signal?.throwIfAborted()
  }

  private readTransaction<T>(read: () => T): T {
    this.db.exec(sql('begin'))
    try {
      const value = read()
      this.db.exec(sql('commit'))
      return value
    } catch (error: unknown) {
      this.rollback(error, 'read')
    }
  }

  private sessionRows(): SessionRow[] {
    return this.db.prepare(sql('select-sessions')).all().map(decodeSessionRow)
  }

  private rollback(error: unknown, operation: string): never {
    try {
      this.db.exec(sql('rollback'))
    } catch (rollbackError: unknown) {
      /* v8 ignore next -- requires SQLite to fail both an operation and its immediate rollback. */
      throw new AggregateError([error, rollbackError], `${this.name} ${operation} failed and rollback also failed`)
    }
    throw error
  }

  private incrementRevision(id: SessionId): void {
    const updated = this.db.prepare(sql('update-session-revision'))
      .run(id)
    /* v8 ignore next -- materialized writes follow coordinator create(); other writes upsert in this transaction. */
    if (Number(updated.changes) !== 1) throw new Error(`session ${id} metadata row is missing`)
  }

  private tailRows(sessionKey: number): EventRow[] {
    const tail = this.db.prepare(sql('select-tail-events')).all(sessionKey, 2).map(decodeEventRow).reverse()
    if (tail.length === 0) return []
    return this.physicalSpanFrom(sessionKey, (tail[0] as EventRow).seq).eventRows
  }

  /** Select the bounded physical span that may represent `fromSeq`. */
  private physicalSpanFrom(
    sessionKey: number,
    fromSeq: number,
  ): { readonly base: number; readonly eventRows: EventRow[] } {
    const packedFloor = Math.max(0, fromSeq - MAX_PACKED_ROW_MEMBERS + 1)
    const packedPredecessors = this.db.prepare(sql('select-packed-predecessors'))
      .all(sessionKey, packedFloor, fromSeq)
      .map(decodeEventRow)
    let base = fromSeq
    for (const predecessor of packedPredecessors) {
      try {
        const last = decodeRow(predecessor).at(-1)
        if (last !== undefined && last.seq >= fromSeq) base = Math.min(base, predecessor.seq)
      } catch {
        // A malformed bounded predecessor may cover fromSeq; include it so the scanner fails closed.
        base = Math.min(base, predecessor.seq)
      }
    }
    const eventRows = this.db.prepare(sql('select-events-from')).all(sessionKey, base).map(decodeEventRow)
    return { base, eventRows }
  }

  private logicalLastEvent(id: SessionId, tailRows: readonly EventRow[]): SessionEvent | undefined {
    if (tailRows.length === 0) return undefined
    const { preserved, tornFrom } = scanRows(tailRows, (tailRows[0] as EventRow).seq)
    if (tornFrom !== undefined) throw new Error(`session ${id} has an invalid physical tail at seq ${tornFrom}`)
    return preserved.at(-1)
  }

  private insertStatement(): StatementSync {
    return this.db.prepare(sql('insert-event'))
  }

  private insertRecord(insert: StatementSync, sessionKey: number, record: BoundRecord): void {
    insert.run(
      sessionKey,
      record.seq,
      record.type,
      record.time,
      record.data,
      record.sourceEventSeqs,
      record.surfaceOp,
      record.ignorable,
    )
  }

  private writeRow(meta: SessionHeader): number {
    const inserted = this.db.prepare(sql('upsert-session')).get(
      meta.id,
      meta.version,
      meta.createdAt,
      meta.cwd ?? null,
      meta.parentSession ?? null,
      meta.seedLength ?? null,
      meta.origin ?? null,
      meta.delegationDepth ?? null,
      meta.agentPreset ?? null,
      randomUUID(),
    ) as { id: number }
    return inserted.id
  }
}

function sqliteRevision(storeIdentity: string, row: SessionRow): PersistenceRevision {
  return SessionPersistenceRevision(
    `${storeIdentity}:incarnation:${row.incarnation}:revision:${row.revision}`,
  )
}

async function createDatabaseFile(path: string): Promise<void> {
  try {
    const handle = await open(path, 'wx', 0o600)
    await handle.close()
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
  }
}

async function validateParentDirectory(path: string): Promise<void> {
  const parent = await lstat(path)
  if (parent.isSymbolicLink() || !parent.isDirectory()) {
    throw new Error(`session database parent "${path}" must be a real directory`)
  }
  const uid = process.getuid?.()
  /* v8 ignore start -- Windows exposes neither process.getuid nor meaningful
   * uid/mode bits; POSIX tests cover owner and mode rejection. */
  if (uid !== undefined && (parent.uid !== uid || (parent.mode & 0o022) !== 0)) {
    throw new Error(`session database parent "${path}" must be owned by the current user and not group/world-writable`)
  }
  /* v8 ignore stop */
}

async function validateDatabaseFile(path: string): Promise<void> {
  const file = await lstat(path)
  if (file.isSymbolicLink() || !file.isFile()) {
    throw new Error(`session database "${path}" must be a regular file, not a symbolic link`)
  }
  const uid = process.getuid?.()
  /* v8 ignore start -- Windows exposes neither process.getuid nor meaningful
   * uid/mode bits; POSIX tests cover owner and mode rejection. */
  if (uid !== undefined && (file.uid !== uid || (file.mode & 0o077) !== 0)) {
    throw new Error(`session database "${path}" must be owned by the current user and accessible only by that user`)
  }
  /* v8 ignore stop */
}

async function validateDatabaseFileIfPresent(path: string): Promise<void> {
  try {
    await validateDatabaseFile(path)
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
}

let nodeSqlite: Promise<typeof import('node:sqlite')> | undefined

/** Load Node SQLite once so concurrent stores share one warning-filter lifetime. */
function loadNodeSqlite(): Promise<typeof import('node:sqlite')> {
  nodeSqlite ??= importNodeSqlite()
  return nodeSqlite
}

/** Import Node 22's SQLite dependency without its process-wide experimental warning. */
async function importNodeSqlite(): Promise<typeof import('node:sqlite')> {
  const emitWarning = Reflect.get(process, 'emitWarning')
  /* v8 ignore start -- Node 22 alone emits this warning; primary coverage runs on Node 24. */
  const filteredEmitWarning = (warning: string | Error, ...args: unknown[]): void => {
    const message = warning instanceof Error ? warning.message : warning
    const first = args[0]
    const type = warning instanceof Error
      ? warning.name
      : typeof first === 'string'
        ? first
        : typeof first === 'object' && first !== null && 'type' in first
          ? first.type
          : undefined
    if (message === 'SQLite is an experimental feature and might change at any time'
      && type === 'ExperimentalWarning') return
    Reflect.apply(emitWarning, process, [warning, ...args])
  }
  Reflect.set(process, 'emitWarning', filteredEmitWarning)
  try {
    return await import('node:sqlite')
  } finally {
    Reflect.set(process, 'emitWarning', emitWarning)
  }
  /* v8 ignore stop */
}
