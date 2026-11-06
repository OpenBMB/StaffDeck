/** Test-only loader for fixed SQLite fixtures. */

import { readFileSync } from 'node:fs'

export type TestSqlName =
  | 'add-unexpected-column'
  | 'count-events'
  | 'count-ignorable-events'
  | 'count-packed-events'
  | 'count-physical-types'
  | 'create-loose-schema'
  | 'create-unrelated-table'
  | 'delete-persistence-state'
  | 'delete-session-events'
  | 'empty-store-id'
  | 'insert-corrupt-event'
  | 'measure-write-traffic'
  | 'replace-events-with-nonstrict-table'
  | 'select-last-event'
  | 'select-page-size'
  | 'select-event-rowids'
  | 'select-event-rows'
  | 'select-user-version'
  | 'set-application-id-12345'
  | 'set-page-size-4096'
  | 'set-user-version-15'
  | 'set-user-version-16'
  | 'set-user-version-17'
  | 'set-user-version-18'
  | 'set-user-version-19'
  | 'set-user-version-20'
  | 'update-invalid-session-metadata'
  | 'vacuum'

/** Load one fixed test SQL resource. */
export function testSql(name: TestSqlName): string {
  return readFileSync(new URL(`./resources/sql/${name}.sql`, import.meta.url), 'utf8')
}
