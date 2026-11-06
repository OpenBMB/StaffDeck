/** Regression tests for bilingual snapshots, corpus scope, and structure. */

import { execFileSync, spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  gitBlobHash,
  gitIndexPaths,
  readGitIndexBlob,
  storeGitBlob,
} from './translation-pairing-git.ts'
import {
  parseTranslationPairingRecord,
  renderTranslationPairingRecord,
  translationPairPaths,
} from './translation-pairing-record.ts'
import {
  blobHash,
  isTranslationPairingManifestExcluded,
  isTranslationScopeFile,
  languageSwitcherTargets,
  pairAnchorOfArgument,
  parseTranslationMarkdown,
  parseTranslationPairingCliArgs,
  parseTranslationPairingManifest,
  partitionGeneratedRegions,
  requiresSourceLanguageSwitcher,
  translationPairSourcePredicate,
  translationStructureDiff,
  translationStructureSignature,
} from './translation-pairing.ts'

const fixturePairSource = (): boolean => true

function signature(markdown: string) {
  return translationStructureSignature(
    parseTranslationMarkdown(markdown),
    'counterpart.zh.md',
    {
      repoRoot: process.cwd(), sourcePath: 'counterpart.md',
      isTranslationPairSource: fixturePairSource, repositoryFileExists: () => true, markdown,
    },
  )
}

function fixtureSignature(
  root: string,
  sourcePath: string,
  markdown: string,
  switcherTarget: string,
) {
  return translationStructureSignature(
    parseTranslationMarkdown(markdown),
    switcherTarget,
    { repoRoot: root, sourcePath, isTranslationPairSource: fixturePairSource, markdown },
  )
}

function gitSupportsObjectFormat(format: 'sha256'): boolean {
  const root = mkdtempSync(join(tmpdir(), 'dsh-git-object-format-'))
  try {
    return spawnSync('git', ['init', '--quiet', `--object-format=${format}`, root], {
      stdio: 'ignore',
    }).status === 0
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
}

const supportsSha256ObjectFormat = gitSupportsObjectFormat('sha256')

describe('translation pairing snapshots', () => {
  it('stores exact uncommitted bytes for later recovery by object ID', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-pairing-'))
    try {
      execFileSync('git', ['init', '--quiet', root], {
        env: { ...process.env, GIT_DEFAULT_HASH: 'sha1' },
      })
      const content = Buffer.from([0x75, 0x6e, 0x63, 0x6f, 0x6d, 0x6d, 0x69, 0x74, 0x74, 0x65, 0x64, 0x0a, 0xff])

      const objectId = storeGitBlob(root, content)

      expect(objectId).toBe(gitBlobHash(content))
      expect(execFileSync('git', [
        '-C', root, 'rev-parse', `refs/dsh/translation-pairing/snapshots/${objectId}`,
      ], { encoding: 'utf8' }).trim()).toBe(objectId)
      execFileSync('git', ['-C', root, 'gc', '--prune=now'])
      expect(execFileSync('git', ['-C', root, 'cat-file', '-p', objectId])).toEqual(content)
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('fails before a sidecar can reference an unavailable object', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-pairing-'))
    try {
      expect(() => storeGitBlob(root, Buffer.from('snapshot'))).toThrow('git hash-object -w --stdin failed')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('fails clearly when Git cannot be started', () => {
    const previousPath = process.env.PATH
    try {
      process.env.PATH = ''
      expect(() => storeGitBlob('.', Buffer.from('snapshot'))).toThrow('git hash-object -w --stdin failed')
    } finally {
      process.env.PATH = previousPath
    }
  })

  it('reads staged bytes independently of the working tree', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-pairing-index-'))
    try {
      execFileSync('git', ['init', '--quiet', root], {
        env: { ...process.env, GIT_DEFAULT_HASH: 'sha1' },
      })
      execFileSync('git', ['-C', root, 'config', 'user.email', 'pairing@example.test'])
      execFileSync('git', ['-C', root, 'config', 'user.name', 'Pairing Test'])
      writeFileSync(join(root, 'owner.md'), 'staged')
      execFileSync('git', ['-C', root, 'add', 'owner.md'])
      writeFileSync(join(root, 'owner.md'), 'unstaged')

      const indexed = readGitIndexBlob(root, 'owner.md')

      expect(indexed?.content.toString('utf8')).toBe('staged')
      expect(indexed?.objectId).toBe(gitBlobHash(Buffer.from('staged')))
      expect(readGitIndexBlob(root, 'absent.md')).toBeUndefined()
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('lists exact index files without treating a directory prefix as one entry', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-pairing-index-'))
    try {
      execFileSync('git', ['init', '--quiet', root], {
        env: { ...process.env, GIT_DEFAULT_HASH: 'sha1' },
      })
      mkdirSync(join(root, 'docs'), { recursive: true })
      writeFileSync(join(root, 'docs/reference.md'), '# Reference\n')
      writeFileSync(join(root, 'docs/reference.zh.md'), '# 参考\n')
      execFileSync('git', ['-C', root, 'add', 'docs'])

      expect(gitIndexPaths(root)).toEqual(new Set([
        'docs/reference.md',
        'docs/reference.zh.md',
      ]))
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it.skipIf(!supportsSha256ObjectFormat)('rejects an object format that pairing records cannot represent', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-pairing-'))
    try {
      execFileSync('git', ['init', '--quiet', '--object-format=sha256', root])
      expect(() => storeGitBlob(root, Buffer.from('snapshot'))).toThrow('returned unexpected object ID')
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

describe('translation pairing manifest', () => {
  it('accepts an exclusions-only manifest', () => {
    const manifest = parseTranslationPairingManifest(JSON.stringify({
      excluded: ['docs/generated/'],
    }))
    expect(manifest).toEqual({
      excluded: ['docs/generated/'],
    })
    expect(isTranslationPairingManifestExcluded('docs/generated/page.md', manifest)).toBe(true)
    expect(translationPairSourcePredicate(manifest)('docs/generated/page.md')).toBe(false)
    expect(translationPairSourcePredicate(manifest)('docs/guide.md')).toBe(true)
    expect(translationPairSourcePredicate(manifest)('packages/example/guide.md')).toBe(false)
  })

  it.each([
    ['required', ['packages/README.md']],
    ['requiredClasses', ['readme']],
    ['requiredSince', '2026-07-14'],
  ] as const)('rejects obsolete policy field %s instead of accepting an inert requirement', (field, value) => {
    expect(() => parseTranslationPairingManifest(JSON.stringify({
      excluded: [],
      [field]: value,
    }))).toThrow(`unsupported field(s): ${field}; every in-scope document is required`)
  })

  it('rejects a missing or non-string exclusion list', () => {
    expect(() => parseTranslationPairingManifest('{}')).toThrow('excluded must be an array of strings')
    expect(() => parseTranslationPairingManifest(JSON.stringify({
      excluded: [42],
    }))).toThrow('excluded must be an array of strings')
  })
})

describe('translation pairing switchers', () => {
  it('exempts only paired generated English sources from reciprocal switchers', () => {
    expect(requiresSourceLanguageSwitcher('docs/config-catalog.md')).toBe(false)
    expect(requiresSourceLanguageSwitcher('docs/cordis-api/context.md')).toBe(false)
    expect(requiresSourceLanguageSwitcher('docs/cordis-api/inherited.md')).toBe(false)
    expect(requiresSourceLanguageSwitcher('docs/architecture.md')).toBe(true)
    expect(requiresSourceLanguageSwitcher('packages/core/session/README.md')).toBe(true)
  })

  it('accepts only the canonical public URL for an absolute switcher', () => {
    const targets = languageSwitcherTargets('python/sdk/README.zh.md')
    const canonicalMarkdown = '# README\n\nEnglish | [中文](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.zh.md)\n'
    const canonical = parseTranslationMarkdown(canonicalMarkdown)
    const wrongMarkdown = '# README\n\nEnglish | [中文](https://github.com/deepseek-ai/deepseek-harness/blob/master/other/README.zh.md)\n'
    const wrongPath = parseTranslationMarkdown(wrongMarkdown)

    expect(translationStructureSignature(canonical, targets, {
      repoRoot: process.cwd(),
      sourcePath: 'python/sdk/README.md',
      isTranslationPairSource: fixturePairSource,
      markdown: canonicalMarkdown,
    }).links).toEqual([])
    expect(translationStructureSignature(wrongPath, targets, {
      repoRoot: process.cwd(),
      sourcePath: 'python/sdk/README.md',
      isTranslationPairSource: fixturePairSource,
      markdown: wrongMarkdown,
    }).links).toEqual([
      'https://github.com/deepseek-ai/deepseek-harness/blob/master/other/README.zh.md',
    ])
  })

  it('excludes only the header switcher from the structural links', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-switcher-'))
    try {
      writeFileSync(join(root, 'guide.md'), '# Guide\n')
      writeFileSync(join(root, 'guide.zh.md'), '# 指南\n')
      const markdown = '# 指南\n\n[English](guide.md) | 中文\n\n[正文](guide.md)\n'
      expect(translationStructureSignature(
        parseTranslationMarkdown(markdown),
        languageSwitcherTargets('guide.md'),
        {
          repoRoot: root, sourcePath: 'guide.zh.md',
          isTranslationPairSource: fixturePairSource, markdown,
        },
      ).links).toEqual(['dsh-translation-target:guide.md'])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })
})

describe('translation pairing link language parity', () => {
  it('compares a .zh.md target and its .md sibling as the same document', () => {
    const en = 'See [docs](persistence.md) and [notes](note.md#anchor).'
    const zh = '参见[文档](persistence.zh.md)与[笔记](note.zh.md#anchor)。'
    expect(
      translationStructureDiff(
        signature(en),
        signature(zh),
      ),
    ).toEqual([])
  })

  it('still rejects a genuinely different target', () => {
    const en = 'See [docs](persistence.md).'
    const zh = '参见[文档](other.md)。'
    expect(
      translationStructureDiff(
        signature(en),
        signature(zh),
      ),
    ).not.toEqual([])
  })
})

describe('translation pairing records', () => {
  const paths = translationPairPaths('docs/foo.md')
  const record = {
    sourceHash: '1'.repeat(40),
    zhHash: '2'.repeat(40),
  }

  it('round-trips the canonical two-hash record', () => {
    expect(parseTranslationPairingRecord(renderTranslationPairingRecord(paths, record), paths)).toEqual(record)
  })

  it('rejects duplicate or unexpected keys', () => {
    expect(parseTranslationPairingRecord([
      `foo.md: ${'1'.repeat(40)}`,
      `foo.md: ${'3'.repeat(40)}`,
      `foo.zh.md: ${'2'.repeat(40)}`,
      '',
    ].join('\n'), paths)).toBeUndefined()
    expect(parseTranslationPairingRecord([
      `foo.md: ${'1'.repeat(40)}`,
      `bar.zh.md: ${'2'.repeat(40)}`,
      '',
    ].join('\n'), paths)).toBeUndefined()
  })
})

describe('translation scope discovery', () => {
  it.each([
    'README.md',
    'CONTRIBUTING.md',
    'CONTRIBUTING.zh.md',
    'CONTRIBUTING.i18n.yaml',
    'BRAND_GUIDELINES.md',
    'BRAND_GUIDELINES.zh.md',
    'BRAND_GUIDELINES.i18n.yaml',
    'SAFETY.md',
    'SAFETY.zh.md',
    'SAFETY.i18n.yaml',
    'apps/cli/README.md',
    'future/subtree/readme.md',
    'packages/example/README.zh.md',
    'native/example/README.i18n.yaml',
    '.agents/notes/proposed/feature.md',
    'docs/guide.md',
    'python/guide.md',
  ])('includes %s', (file) => {
    expect(isTranslationScopeFile(file)).toBe(true)
  })

  it.each([
    'packages/example/guide.md',
    'packages/example/CONTRIBUTING.md',
    'packages/example/BRAND_GUIDELINES.md',
    'other/tutorial.md',
    'website/reference.md',
    'packages/example/README.txt',
    'vendor/example/README.md',
    'packages/example/node_modules/dependency/README.md',
    'packages/example/lib/README.md',
    'coverage/report/README.md',
    'python/sdk-runtime/src/deepseek_harness_runtime/runtime/deepseek-harness-sdk-runtime-macos-arm64/README.md',
    'python/sdk-runtime/src/deepseek_harness_runtime/runtime/node/README.md',
  ])('excludes non-source or non-README path %s', (file) => {
    expect(isTranslationScopeFile(file)).toBe(false)
  })
})

describe('translation structural signature', () => {
  it('retains external GFM autolinks without parsing inline-link syntax', () => {
    const markdown = '<https://example.com/reference.md>\n'
    expect(signature(markdown).links).toEqual(['https://example.com/reference.md'])
  })

  it('retains exact authored bytes for ordinary external link targets', () => {
    const escaped = signature('[External](https://example.com/?x=1&amp;y=2)\n')
    const literal = signature('[External](https://example.com/?x=1&y=2)\n')
    expect(escaped.links).toEqual(['https://example.com/?x=1&amp;y=2'])
    expect(translationStructureDiff(escaped, literal)).toEqual([
      'link target #1 diverges between the pair: "https://example.com/?x=1&amp;y=2" vs "https://example.com/?x=1&y=2"',
    ])
  })

  it('treats target-locale siblings as one semantic link target', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-structure-'))
    try {
      writeFileSync(join(root, 'reference.md'), '# Reference\n')
      writeFileSync(join(root, 'reference.zh.md'), '# 参考\n')
      const sourceMarkdown = '[Reference](reference.md?view=full#section)\n'
      const counterpartMarkdown = '[参考](reference.zh.md?view=full#section)\n'
      const source = fixtureSignature(root, 'guide.md', sourceMarkdown, 'guide.zh.md')
      const counterpart = fixtureSignature(root, 'guide.zh.md', counterpartMarkdown, 'guide.md')
      expect(translationStructureDiff(source, counterpart)).toEqual([])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('includes reference-style document links but excludes image-only definitions', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-structure-'))
    try {
      writeFileSync(join(root, 'reference.md'), '# Reference\n')
      writeFileSync(join(root, 'reference.zh.md'), '# 参考\n')
      const markdown = [
        '[Reference][doc]',
        '',
        '![Preview][asset]',
        '',
        '[doc]: reference.md',
        '[asset]: reference.zh.md',
        '',
      ].join('\n')
      expect(translationStructureSignature(
        parseTranslationMarkdown(markdown),
        'guide.zh.md',
        {
          repoRoot: root, sourcePath: 'guide.md',
          isTranslationPairSource: fixturePairSource, markdown,
        },
      ).links).toEqual(['dsh-translation-target:reference.md'])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('compares the first duplicate reference definition that CommonMark resolves', () => {
    const root = mkdtempSync(join(tmpdir(), 'dsh-translation-structure-'))
    try {
      for (const name of ['reference', 'different', 'other']) {
        writeFileSync(join(root, `${name}.md`), `# ${name}\n`)
        writeFileSync(join(root, `${name}.zh.md`), `# ${name} zh\n`)
      }
      const sourceMarkdown = '[Reference][ref]\n\n[ref]: reference.md\n[ref]: other.md\n'
      const counterpartMarkdown = '[参考][ref]\n\n[ref]: different.zh.md\n[ref]: other.zh.md\n'
      const source = fixtureSignature(root, 'guide.md', sourceMarkdown, 'guide.zh.md')
      const counterpart = fixtureSignature(root, 'guide.zh.md', counterpartMarkdown, 'guide.md')
      expect(translationStructureDiff(source, counterpart)).toEqual([
        'link target #1 diverges between the pair: "dsh-translation-target:reference.md" vs "dsh-translation-target:different.md"',
      ])
    } finally {
      rmSync(root, { recursive: true, force: true })
    }
  })

  it('accepts matching list kinds, starts, and item counts', () => {
    const source = signature('3. One\n4. Two\n\n- A\n- B\n')
    const counterpart = signature('3. 一\n4. 二\n\n- 甲\n- 乙\n')
    expect(translationStructureDiff(source, counterpart)).toEqual([])
  })

  it('rejects an altered ordered-list start', () => {
    const source = signature('3. One\n4. Two\n\n- A\n- B\n')
    const counterpart = signature('1. 一\n2. 二\n\n- 甲\n- 乙\n')
    expect(translationStructureDiff(source, counterpart)).toEqual([
      'list (kind, start, item count) #1 diverges between the pair: "ordered:start=3:items=2" vs "ordered:start=1:items=2"',
    ])
  })

  it('rejects a missing list item', () => {
    const source = signature('- A\n- B\n')
    const counterpart = signature('- 甲\n')
    expect(translationStructureDiff(source, counterpart)).toEqual([
      'list (kind, start, item count) #1 diverges between the pair: "bullet:items=2" vs "bullet:items=1"',
    ])
  })

  it('rejects altered table row or column counts', () => {
    const source = signature('| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n')
    const counterpart = signature('| 甲 | 乙 |\n|---|---|\n| 一 | 二 |\n')
    expect(translationStructureDiff(source, counterpart)).toEqual([
      'table (row x column count) #1 diverges between the pair: "3x2" vs "2x2"',
    ])
  })
})

describe('pair CLI arguments', () => {
  it('normalizes any pair file or bare stem to the English anchor', () => {
    expect(pairAnchorOfArgument('docs/foo.md')).toBe('docs/foo.md')
    expect(pairAnchorOfArgument('docs/foo.zh.md')).toBe('docs/foo.md')
    expect(pairAnchorOfArgument('docs/foo.i18n.yaml')).toBe('docs/foo.md')
    expect(pairAnchorOfArgument('docs/foo')).toBe('docs/foo.md')
    expect(pairAnchorOfArgument('.\\docs\\foo.zh.md')).toBe('docs/foo.md')
  })

  it('scopes a check to named pairs and dedupes the three spellings', () => {
    expect(parseTranslationPairingCliArgs(['docs/foo.zh.md', 'docs/foo.i18n.yaml', 'docs/bar.md'])).toEqual({
      input: 'worktree',
      mode: 'check',
      scope: 'pairs',
      anchors: ['docs/bar.md', 'docs/foo.md'],
    })
    expect(parseTranslationPairingCliArgs([])).toEqual({
      input: 'worktree',
      mode: 'check',
      scope: 'corpus',
      anchors: [],
    })
  })

  it('requires --write to name confirmed pairs or opt into --all', () => {
    expect(() => parseTranslationPairingCliArgs(['--write'])).toThrow('requires the pair(s) you confirmed')
    expect(parseTranslationPairingCliArgs(['--write', 'docs/foo.md'])).toEqual({
      input: 'worktree',
      mode: 'write',
      scope: 'pairs',
      anchors: ['docs/foo.md'],
    })
    expect(parseTranslationPairingCliArgs(['--write', '--all'])).toEqual({
      input: 'worktree',
      mode: 'write',
      scope: 'corpus',
      anchors: [],
    })
    expect(() => parseTranslationPairingCliArgs(['--write', '--all', 'docs/foo.md'])).toThrow('not both')
  })

  it('keeps --list corpus-only and rejects unknown flags', () => {
    expect(parseTranslationPairingCliArgs(['--list'])).toEqual({
      input: 'worktree',
      mode: 'list',
      scope: 'corpus',
      anchors: [],
    })
    expect(() => parseTranslationPairingCliArgs(['--list', 'docs/foo.md'])).toThrow('takes no other flags or paths')
    expect(() => parseTranslationPairingCliArgs(['--all'])).toThrow('--all only applies to --write')
    expect(() => parseTranslationPairingCliArgs(['--frobnicate'])).toThrow('unknown flag(s): --frobnicate')
  })

  it('makes cached verification a named, read-only index check', () => {
    expect(parseTranslationPairingCliArgs(['--cached', 'docs/foo.i18n.yaml'])).toEqual({
      input: 'index',
      mode: 'check',
      scope: 'pairs',
      anchors: ['docs/foo.md'],
    })
    expect(() => parseTranslationPairingCliArgs(['--cached'])).toThrow('requires the staged pair paths')
    expect(() => parseTranslationPairingCliArgs(['--cached', '--write', 'docs/foo.md'])).toThrow('read-only')
  })
})

describe('generated regions', () => {
  const BEGIN = '<!-- BEGIN GENERATED cordis-surface (gen-cordis-catalog.ts) — do not edit between markers -->'
  const END = '<!-- END GENERATED cordis-surface -->'

  it('partitions marker-delimited regions from the hand-owned remainder', () => {
    const doc = `# T\n\nprose\n\n${BEGIN}\ninjected\n${END}\ntail\n`
    const { regions, stripped } = partitionGeneratedRegions(doc)
    expect(regions).toEqual([`${BEGIN}\ninjected\n${END}`])
    expect(stripped).toBe('# T\n\nprose\n\ntail\n')
  })

  it('treats a document without markers as one hand-owned remainder', () => {
    const { regions, stripped } = partitionGeneratedRegions('# T\n\nprose\n')
    expect(regions).toEqual([])
    expect(stripped).toBe('# T\n\nprose\n')
  })

  it('rejects unbalanced or nested markers', () => {
    expect(() => partitionGeneratedRegions(`${END}\n`)).toThrow('without a BEGIN')
    expect(() => partitionGeneratedRegions(`${BEGIN}\n`)).toThrow('without an END')
    expect(() => partitionGeneratedRegions(`${BEGIN}\n${BEGIN}\n${END}\n`)).toThrow('nested')
  })

  it('rejects mismatched slugs and malformed marker lines', () => {
    expect(() => partitionGeneratedRegions('<!-- BEGIN GENERATED a -->\nx\n<!-- END GENERATED b -->\n'))
      .toThrow("END slug 'b' does not match its BEGIN slug 'a'")
    expect(() => partitionGeneratedRegions('<!-- BEGIN GENERATED a --> trailing\nx\n<!-- END GENERATED a -->\n'))
      .toThrow('malformed generated region marker line')
    expect(() => partitionGeneratedRegions('x\n<!-- END GENERATED a --> tail\n'))
      .toThrow('malformed generated region marker line')
  })

  it('computes the exact git blob hash', () => {
    // `git hash-object` of the empty file and of "x\n" — pinned upstream values.
    expect(blobHash(Buffer.from(''))).toBe('e69de29bb2d1d6434b8b29ae775ad8c2e48c5391')
    expect(blobHash(Buffer.from('x\n'))).toBe('587be6b4c3f93f93c489c0111bba5596147a26cb')
  })
})
