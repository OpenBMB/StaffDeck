const fs = require('fs');
const path = require('path');
const ts = require('typescript');

const projectRoot = path.resolve(__dirname, '..');
const sourceRoot = path.join(projectRoot, 'src');
const catalog = require(path.join(sourceRoot, 'i18n', 'en.json'));
const indonesianCatalog = require(path.join(sourceRoot, 'i18n', 'id.json'));
const ignoredFiles = new Set([
  path.join(sourceRoot, 'components', 'LanguageSwitcher.tsx'),
]);
const ignoredFragments = ["after:content-['展开']"];
const missing = new Map();

function sourceFiles(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'i18n') return [];
      return sourceFiles(fullPath);
    }
    return /\.(ts|tsx)$/.test(entry.name) ? [fullPath] : [];
  });
}

function record(rawValue, sourceFile, node, kind) {
  const value = rawValue.replace(/\s+/g, ' ').trim();
  if (!/[\u3400-\u9fff]/.test(value)) return;
  if (ignoredFragments.some((fragment) => value.includes(fragment))) return;
  if (Object.prototype.hasOwnProperty.call(catalog, value)) return;
  const line = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
  const key = `${sourceFile.fileName}:${line}:${kind}`;
  missing.set(key, value);
}

for (const filePath of sourceFiles(sourceRoot)) {
  if (ignoredFiles.has(filePath)) continue;
  const sourceText = fs.readFileSync(filePath, 'utf8');
  const sourceFile = ts.createSourceFile(
    filePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const visit = (node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      record(node.text, sourceFile, node, 'string');
    }
    if (ts.isJsxText(node)) record(node.getText(sourceFile), sourceFile, node, 'jsx');
    if (ts.isTemplateExpression(node)) {
      const parts = [
        node.head.text,
        ...node.templateSpans.map((span, index) => `{${index + 1}}${span.literal.text}`),
      ];
      record(parts.join(''), sourceFile, node, 'template');
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
}

const invalidTargets = Object.entries(catalog).filter(
  ([, target]) => !target.trim() || /[\u3400-\u9fff]/.test(target),
);

const idMissingKeys = Object.keys(catalog).filter((key) => !(key in indonesianCatalog));
const idInvalidTargets = Object.entries(indonesianCatalog).filter(
  ([, target]) => !target.trim(),
);

if (missing.size || invalidTargets.length || idMissingKeys.length || idInvalidTargets.length) {
  if (missing.size) {
    console.error(`Missing English translations (${missing.size}):`);
    for (const [location, value] of missing) {
      console.error(`- ${path.relative(projectRoot, location)}: ${JSON.stringify(value)}`);
    }
  }
  if (invalidTargets.length) {
    console.error(`Invalid English translations (${invalidTargets.length}):`);
    for (const [source, target] of invalidTargets) {
      console.error(`- ${JSON.stringify(source)} => ${JSON.stringify(target)}`);
    }
  }
  if (idMissingKeys.length) {
    console.error(`Missing Indonesian translations (${idMissingKeys.length}):`);
    for (const key of idMissingKeys.slice(0, 20)) {
      console.error(`- ${JSON.stringify(key)}`);
    }
    if (idMissingKeys.length > 20) console.error(`  ... and ${idMissingKeys.length - 20} more`);
  }
  if (idInvalidTargets.length) {
    console.error(`Invalid Indonesian translations (${idInvalidTargets.length}):`);
    for (const [source, target] of idInvalidTargets.slice(0, 20)) {
      console.error(`- ${JSON.stringify(source)} => ${JSON.stringify(target)}`);
    }
    if (idInvalidTargets.length > 20) console.error(`  ... and ${idInvalidTargets.length - 20} more`);
  }
  process.exit(1);
}

console.log(`i18n coverage OK: ${Object.keys(catalog).length} English translations, ${Object.keys(indonesianCatalog).length} Indonesian translations.`);
