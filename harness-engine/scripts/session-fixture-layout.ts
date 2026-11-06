/** Canonical packed-row and envelope projection helpers for repository session fixtures. */

import { deepStrictEqual } from 'node:assert'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { packChunkRuns, type SessionEvent } from '@deepseek-ai/dsh-session'
import { parseSessionLog } from '@deepseek-ai/dsh-llm-replay'

/** Physical persistence artifacts validated by the WebWorker runtime fixture spec. */
const WEBWORKER_PHYSICAL_SESSION_FIXTURE_ROOT =
  'packages/experimental/webworker-runtime/tests/fixtures/vfs-example/home/sessions/'

/** Installed-runtime snapshots that preserve the JSONL writer's physical encoding. */
const PYTHON_RUNTIME_PHYSICAL_SESSION_FIXTURE_ROOT =
  'scripts/snapshots/python-sdk-single-exe/'

/** One repository session fixture and its canonical projected representation. */
export interface SessionFixtureLayout {
  /** Repository-relative path with `/` separators. */
  path: string
  /** Current fixture bytes decoded as UTF-8. */
  source: string
  /** Canonical projected fixture bytes. */
  canonical: string
}

/**
 * Whether a repository JSONL preserves physical persistence encoding rather
 * than the logical event projection owned by this script.
 * @param path - Repository-relative path with `/` separators.
 * @returns True for physical WebWorker and installed-runtime session logs.
 */
export function isPhysicalSessionFixture(path: string): boolean {
  if (path.startsWith(WEBWORKER_PHYSICAL_SESSION_FIXTURE_ROOT)) {
    return path.endsWith('/session.jsonl')
  }
  return path.startsWith(PYTHON_RUNTIME_PHYSICAL_SESSION_FIXTURE_ROOT)
    && /\/session(?:\.\d+)?\.jsonl$/.test(path)
}

function isSessionHeader(value: unknown): boolean {
  return value !== null && typeof value === 'object' && (value as { type?: unknown }).type === 'session'
}

function renderFixture(headerLine: string, events: readonly SessionEvent[]): string {
  return [
    headerLine,
    ...packChunkRuns(events).map((stored) => {
      const record = stored as unknown as Record<string, unknown>
      delete record.seq
      delete record.time
      delete record.seq0
      delete record.time0
      return JSON.stringify(record)
    }),
    '',
  ].join('\n')
}

function withoutEnvelope(events: readonly SessionEvent[]): Array<Omit<SessionEvent, 'seq' | 'time'>> {
  return events.map((event) => {
    const { seq: _seq, time: _time, ...projected } = event
    return projected
  })
}

/**
 * Canonicalize one JSONL document when its first record is a session header.
 * The header line remains byte-identical; body records decode to logical events,
 * re-encode with {@link packChunkRuns}, and omit storage sequence/time envelopes.
 * Non-session JSONL returns undefined.
 *
 * @param content - JSONL source text.
 * @param label - path-like diagnostic label.
 * @returns Canonical text for a session fixture, otherwise undefined.
 */
export function canonicalSessionFixture(content: string, label = '<session-fixture>'): string | undefined {
  const headerLine = content.split(/\r?\n/).find(line => line.trim().length > 0)
  if (headerLine === undefined) return undefined

  let headerValue: unknown
  try {
    headerValue = JSON.parse(headerLine) as unknown
  } catch {
    return undefined
  }
  if (!isSessionHeader(headerValue)) return undefined

  let events
  try {
    events = parseSessionLog(content)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new Error(`${label}: ${detail}`, { cause: error })
  }
  const canonical = renderFixture(headerLine, events)
  const decoded = parseSessionLog(canonical)
  try {
    deepStrictEqual(withoutEnvelope(decoded), withoutEnvelope(events))
  } catch (error) {
    throw new Error(`${label}: packed snapshot rewrite changed the event payload stream`, { cause: error })
  }
  if (renderFixture(headerLine, decoded) !== canonical) {
    throw new Error(`${label}: packed rewrite is not idempotent`)
  }
  return canonical
}

/**
 * Discover tracked and unignored untracked JSONL files through Git.
 *
 * @param root - repository root.
 * @returns Stable repository-relative paths.
 */
function discoverJsonlFiles(root: string): string[] {
  return execFileSync(
    'git',
    ['ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '*.jsonl'],
    { cwd: root, encoding: 'utf8' },
  ).split('\0')
    .filter(path => path.length > 0 && existsSync(resolve(root, path)))
    .sort()
}

/**
 * Inspect every repository JSONL whose first record is a session header.
 *
 * @param root - repository root.
 * @returns Session fixtures with current and canonical text.
 */
export function inspectSessionFixtureLayouts(root: string): SessionFixtureLayout[] {
  return discoverJsonlFiles(root).flatMap((path) => {
    if (isPhysicalSessionFixture(path)) return []
    const source = readFileSync(resolve(root, path), 'utf8')
    const canonical = canonicalSessionFixture(source, path)
    return canonical === undefined ? [] : [{ path, source, canonical }]
  })
}
