import { describe, expect, it } from 'vitest'
import { SessionId } from '@deepseek-ai/dsh-session'
import { parseSessionReferenceText, decodeSessionReferenceUri, encodeSessionReferenceUri, type ParsedSessionReferenceText } from '../src/uri.ts'
import { SessionReferenceError } from '../src/config.ts'
import type { SessionReferenceInput } from '../src/types.ts'

function legacyParse(text: string): ParsedSessionReferenceText {
  const references: SessionReferenceInput[] = []
  const pattern = /@\[((?:\\.|[^\\\]])*)\]\((dsh-session:[^\s)]*)\)|(dsh-session:[A-Za-z0-9_-]+)/gu
  const rendered = text.replace(pattern, (
    _match,
    rawLabel: string | undefined,
    markdownUri: string | undefined,
    bareUri: string | undefined,
  ) => {
    const uri = markdownUri ?? bareUri
    /* v8 ignore next -- the two-alternative regex always captures exactly one URI group. */
    if (uri === undefined) throw new SessionReferenceError('session reference URI is missing', 'SESSION_REFERENCE_INVALID_REFERENCE')
    const sessionId = decodeSessionReferenceUri(uri)
    const label = rawLabel === undefined ? sessionId : unescapeLabel(rawLabel)
    references.push({ sessionId, label })
    return `@${label}`
  })
  return { text: rendered, references }
}

function unescapeLabel(label: string): string {
  return label.replace(/\\(.)/gu, '$1')
}


describe('linear session mention scan', () => {
  it('preserves legacy parsing for short nested, escaped and malformed inputs', () => {
    const uri = encodeSessionReferenceUri(SessionId('source'))
    const fragments = ['@[', 'a', '](', ')', 'x', '\n', String.fromCharCode(92), 'dsh-session:', uri]
    let seed = 41
    const outcome = (run: () => unknown) => { try { return { value: run() } } catch { return { failed: true } } }
    for (let i = 0; i < 1500; i++) {
      let text = ''
      for (let j = 0; j < 12; j++) { seed = (seed * 1664525 + 1013904223) >>> 0; text += fragments[seed % fragments.length] }
      expect(outcome(() => parseSessionReferenceText(text))).toEqual(outcome(() => legacyParse(text)))
    }
  })
  it('handles repeated unterminated markers without rescanning every prefix', () => {
    const text = '@['.repeat(50000)
    expect(parseSessionReferenceText(text)).toEqual({ text, references: [] })
  })
})
