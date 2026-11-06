import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const PACKAGE_ROOT = fileURLToPath(new URL('../', import.meta.url))
const SQL_LITERAL = /^\s*(?:ALTER|ATTACH|BEGIN|COMMIT|CREATE|DELETE|DETACH|DROP|INSERT|PRAGMA|REINDEX|RELEASE|ROLLBACK|SAVEPOINT|SELECT|UPDATE|VACUUM|WITH)\s/iu // eslint-disable-line @stylistic/max-len

async function filesUnder(path: string): Promise<string[]> {
  const entries = await readdir(path, { withFileTypes: true })
  return (await Promise.all(entries.map(async entry => entry.isDirectory()
    ? filesUnder(`${path}/${entry.name}`)
    : [`${path}/${entry.name}`]))).flat()
}

function sqlLiteralText(node: ts.Node): string | undefined {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text
  if (node.kind === ts.SyntaxKind.TemplateHead) {
    return (node as ts.Node & { readonly text: string }).text
  }
  return undefined
}

function isOwnedSqlSource(node: ts.Expression | undefined, source: ts.SourceFile): boolean {
  if (node === undefined) return false
  if (ts.isCallExpression(node)
    && ts.isIdentifier(node.expression)
    && (node.expression.text === 'sql' || node.expression.text === 'testSql')) return true
  if (!ts.isIdentifier(node) || node.text !== 'source') return false
  const call = node.parent
  if (!ts.isCallExpression(call)
    || call.arguments.length !== 1
    || call.arguments[0] !== node
    || !ts.isPropertyAccessExpression(call.expression)
    || call.expression.expression.kind !== ts.SyntaxKind.SuperKeyword
    || call.expression.name.text !== 'prepare') return false
  let method: ts.Node | undefined = node.parent
  while (method !== undefined && !ts.isMethodDeclaration(method)) method = method.parent
  if (method === undefined
    || method.name.getText(source) !== 'prepare'
    || method.parameters.length !== 1
    || method.parameters[0]?.name.getText(source) !== 'source') return false
  let classNode: ts.Node | undefined = method.parent
  while (classNode !== undefined && !ts.isClassExpression(classNode)) classNode = classNode.parent
  if (classNode === undefined || classNode.name?.text !== 'JournalFailureDatabase') return false
  const guard = method.body?.statements[0]
  if (guard === undefined
    || !ts.isIfStatement(guard)
    || !ts.isBinaryExpression(guard.expression)
    || guard.expression.operatorToken.kind !== ts.SyntaxKind.ExclamationEqualsEqualsToken
    || guard.expression.left.getText(source) !== 'source'
    || guard.expression.right.getText(source) !== "sql('journal-mode-wal')") return false
  return ts.isReturnStatement(guard.thenStatement)
    && guard.thenStatement.expression === call
}

describe('SQLite SQL resource boundary', () => {
  it('keeps statements and query assembly out of TypeScript files', async () => {
    const files = (await Promise.all([
      filesUnder(`${PACKAGE_ROOT}/src`),
      filesUnder(`${PACKAGE_ROOT}/tests`),
    ])).flat().filter(path => path.endsWith('.ts'))
    const violations: string[] = []
    for (const path of files) {
      const source = ts.createSourceFile(path, await readFile(path, 'utf8'), ts.ScriptTarget.Latest, true)
      const usesNodeSqlite = source.statements.some(statement => ts.isImportDeclaration(statement)
        && ts.isStringLiteral(statement.moduleSpecifier)
        && statement.moduleSpecifier.text === 'node:sqlite')
      const visit = (node: ts.Node): void => {
        const literal = sqlLiteralText(node)
        if (literal !== undefined && SQL_LITERAL.test(literal)) {
          violations.push(`${path}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: SQL literal`)
        }
        // Awaited prepare() is SessionPersistence; DatabaseSync.prepare() is synchronous.
        if (usesNodeSqlite
          && ts.isCallExpression(node)
          && ts.isPropertyAccessExpression(node.expression)
          && (node.expression.name.text === 'exec'
            || (node.expression.name.text === 'prepare' && !ts.isAwaitExpression(node.parent)))) {
          const argument = node.arguments[0]
          if (!isOwnedSqlSource(argument, source)) {
            violations.push(`${path}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: unowned query source`)
          }
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
    expect(violations).toEqual([])
  })

  it('keeps resource text static instead of interpolated', async () => {
    const files = (await Promise.all([
      filesUnder(`${PACKAGE_ROOT}/resources/sql`),
      filesUnder(`${PACKAGE_ROOT}/tests/resources/sql`),
    ])).flat()
    for (const path of files) {
      expect(path.endsWith('.sql')).toBe(true)
      expect(await readFile(path, 'utf8')).not.toContain('${')
    }
  })
})
