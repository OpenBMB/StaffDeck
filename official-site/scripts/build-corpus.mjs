import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourcePath = path.join(projectRoot, 'content', 'product-source.md');
const outputPath = path.join(projectRoot, 'server', 'data', 'product-corpus.json');
const MAX_CHUNK_LENGTH = 1_350;

function cleanMarkdown(value) {
  return value
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/<video[\s\S]*?<\/video>/gi, '')
    .replace(/<[^>]+>/g, '')
    .replace(/\\([#*_\-[\]().])/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/^\s*[-*+]\s+/gm, '• ')
    .replace(/^\s*\d+[.)]\s+/gm, '• ')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function splitLongParagraph(paragraph) {
  if (paragraph.length <= MAX_CHUNK_LENGTH) return [paragraph];
  const sentences = paragraph.split(/(?<=[。！？.!?])\s*/u).filter(Boolean);
  const parts = [];
  let current = '';
  for (const sentence of sentences) {
    if (current && current.length + sentence.length + 1 > MAX_CHUNK_LENGTH) {
      parts.push(current.trim());
      current = '';
    }
    if (sentence.length > MAX_CHUNK_LENGTH) {
      for (let offset = 0; offset < sentence.length; offset += MAX_CHUNK_LENGTH) {
        const slice = sentence.slice(offset, offset + MAX_CHUNK_LENGTH).trim();
        if (slice) parts.push(slice);
      }
    } else {
      current += `${current ? ' ' : ''}${sentence}`;
    }
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

function buildCorpus(markdown) {
  const lines = markdown.split(/\r?\n/);
  const chunks = [];
  const headingStack = [];
  let body = [];
  let chunkIndex = 1;

  const flush = () => {
    const cleaned = cleanMarkdown(body.join('\n'));
    body = [];
    if (!cleaned) return;
    const title = headingStack.filter(Boolean).join(' / ') || 'StaffDeck 产品介绍';
    const paragraphs = cleaned.split(/\n\n+/).flatMap(splitLongParagraph);
    let current = '';
    for (const paragraph of paragraphs) {
      if (current && current.length + paragraph.length + 2 > MAX_CHUNK_LENGTH) {
        chunks.push({ id: `staffdeck-${chunkIndex++}`, title, text: current.trim() });
        current = '';
      }
      current += `${current ? '\n\n' : ''}${paragraph}`;
    }
    if (current.trim()) {
      chunks.push({ id: `staffdeck-${chunkIndex++}`, title, text: current.trim() });
    }
  };

  for (const line of lines) {
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (!heading) {
      body.push(line);
      continue;
    }
    flush();
    const depth = heading[1].length;
    headingStack.length = depth;
    headingStack[depth - 1] = cleanMarkdown(heading[2]);
  }
  flush();

  return {
    schemaVersion: 1,
    source: 'StaffDeck 产品介绍-V2.md',
    chunks,
  };
}

const markdown = await readFile(sourcePath, 'utf8');
const corpus = buildCorpus(markdown);
await mkdir(path.dirname(outputPath), { recursive: true });
await writeFile(outputPath, `${JSON.stringify(corpus, null, 2)}\n`, 'utf8');
console.log(`Built ${corpus.chunks.length} product knowledge chunks.`);
