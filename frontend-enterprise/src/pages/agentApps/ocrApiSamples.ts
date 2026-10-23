/**
 * OCR 助手开放 API 的示例代码生成。
 *
 * 注意：这里刻意**不出现中文**——示例代码是给用户复制到终端/IDE 直接跑的，
 * 中英文注释会随 i18n 目录一起被判为「待翻译文案」（`npm run i18n:check`）。
 * 面向用户的中文说明放在 `OcrApiDialog.tsx` 的界面文案里。
 */

export type OcrApiSamples = {
  curl: string;
  python: string;
  node: string;
};

const PLACEHOLDER_KEY = 'sd_live_REPLACE_WITH_YOUR_KEY';

/** 生成 curl / Python / Node 三种 multipart 调用示例。 */
export function buildOcrApiSamples(endpoint: string): OcrApiSamples {
  const curl = [
    `curl -X POST '${endpoint}' \\`,
    `  -H 'Authorization: Bearer ${PLACEHOLDER_KEY}' \\`,
    "  -F 'files=@scan.pdf' \\",
    "  -F 'files=@invoice.png' \\",
    "  -F 'format=json' \\",
    "  -F 'max_pages=5' \\",
    "  -F 'math=true'",
  ].join('\n');

  const python = [
    'import requests',
    '',
    '',
    `API_KEY = "${PLACEHOLDER_KEY}"`,
    `ENDPOINT = "${endpoint}"`,
    '',
    'files = [',
    '    ("files", ("scan.pdf", open("scan.pdf", "rb"), "application/pdf")),',
    '    ("files", ("invoice.png", open("invoice.png", "rb"), "image/png")),',
    ']',
    'form = {"format": "json", "max_pages": "5", "math": "true"}',
    '',
    'response = requests.post(',
    '    ENDPOINT,',
    '    headers={"Authorization": f"Bearer {API_KEY}"},',
    '    files=files,',
    '    data=form,',
    '    timeout=300,',
    ')',
    'response.raise_for_status()',
    '',
    'payload = response.json()',
    'for result in payload["results"]:',
    '    for page in result["pages"]:',
    '        print(page["index"], page["source"], page["markdown"])',
  ].join('\n');

  const node = [
    "import { readFile } from 'node:fs/promises';",
    '',
    `const API_KEY = '${PLACEHOLDER_KEY}';`,
    `const ENDPOINT = '${endpoint}';`,
    '',
    "const form = new FormData();",
    "const pdf = await readFile('scan.pdf');",
    "form.append('files', new Blob([pdf], { type: 'application/pdf' }), 'scan.pdf');",
    "form.append('format', 'json');",
    "form.append('max_pages', '5');",
    "form.append('math', 'true');",
    '',
    'const response = await fetch(ENDPOINT, {',
    "  method: 'POST',",
    "  headers: { Authorization: 'Bearer ' + API_KEY },",
    '  body: form,',
    '});',
    '',
    'if (!response.ok) throw new Error(await response.text());',
    '',
    'const { results } = await response.json();',
    'for (const file of results) {',
    '  for (const page of file.pages) console.log(page.index, page.source, page.markdown);',
    '}',
  ].join('\n');

  return { curl, python, node };
}
