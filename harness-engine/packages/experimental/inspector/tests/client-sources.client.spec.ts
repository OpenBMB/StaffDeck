/** Client-face source catalog behavior. */

import { describe, expect, it } from 'vitest'
import { ClientSourceCatalog } from '../src/client/cdp/sources.ts'
import { inspectorId } from '../src/shared/bridge/ids.ts'

const scriptKey = inspectorId<'RuntimeScriptKey'>('bundle', 'scriptKey')

describe('Client source catalog', () => {
  it('describes scripts and transfers UTF-8 source and maps in bounded chunks', async () => {
    const source = 'const greeting = "你好"\nconsole.log(greeting)\n'
    const sourceMap = JSON.stringify({ version: 3, sources: ['client.ts'], mappings: 'AAAA' })
    const catalog = new ClientSourceCatalog([{
      scriptKey,
      url: 'http://client.test/plugins/inspector/client.js?rev=abc',
      hash: 'abc',
      sourceMapUrl: 'http://client.test/plugins/inspector/client.js.map?rev=abc',
      isModule: false,
      loadSource: async () => source,
      loadSourceMap: async () => sourceMap,
    }])

    await expect(catalog.execute({ op: 'list-scripts' }, 1_024)).resolves.toEqual({
      op: 'list-scripts',
      scripts: [{
        scriptKey,
        url: 'http://client.test/plugins/inspector/client.js?rev=abc',
        hash: 'abc',
        buildId: '',
        sourceMapUrl: 'http://client.test/plugins/inspector/client.js.map?rev=abc',
        startLine: 0,
        startColumn: 0,
        endLine: 2,
        endColumn: 0,
        isModule: false,
        length: source.length,
      }],
    })

    const bytes: Uint8Array[] = []
    let offset = 0
    while (true) {
      const result = await catalog.execute({
        op: 'get-content-chunk',
        scriptKey,
        content: 'source',
        offset,
        maxBytes: 7,
      }, 1_024)
      if (result.op !== 'get-content-chunk' || !result.available) throw new Error('missing source chunk')
      bytes.push(Uint8Array.from(atob(result.data), character => character.charCodeAt(0)))
      offset = result.nextOffset
      if (result.eof) break
    }
    const combined = new Uint8Array(bytes.reduce((total, chunk) => total + chunk.byteLength, 0))
    let cursor = 0
    for (const chunk of bytes) {
      combined.set(chunk, cursor)
      cursor += chunk.byteLength
    }
    expect(new TextDecoder().decode(combined)).toBe(source)

    const map = await catalog.execute({
      op: 'get-content-chunk',
      scriptKey,
      content: 'source-map',
      offset: 0,
      maxBytes: 1_024,
    }, 1_024)
    if (map.op !== 'get-content-chunk' || !map.available) throw new Error('missing source map')
    expect(new TextDecoder().decode(Uint8Array.from(atob(map.data), character => character.charCodeAt(0))))
      .toBe(sourceMap)
  })

  it('rejects assets above the configured aggregate limit', async () => {
    const catalog = new ClientSourceCatalog([{
      scriptKey,
      url: 'http://client.test/client.js',
      hash: 'abc',
      loadSource: async () => 'x'.repeat(101),
    }])
    await expect(catalog.execute({ op: 'list-scripts' }, 100)).rejects.toMatchObject({ code: 'result-too-large' })
  })
})
