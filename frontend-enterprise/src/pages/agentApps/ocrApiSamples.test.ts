import { describe, expect, it } from 'vitest';

import { buildOcrApiSamples } from './ocrApiSamples';

const ENDPOINT = 'https://staffdeck.example.com/api/v1/ocr';

describe('buildOcrApiSamples', () => {
  it('produces curl / python / node samples that hit the given endpoint', () => {
    const samples = buildOcrApiSamples(ENDPOINT);

    expect(samples.curl).toContain(`curl -X POST '${ENDPOINT}'`);
    expect(samples.curl).toContain("-H 'Authorization: Bearer sd_live_REPLACE_WITH_YOUR_KEY'");

    expect(samples.python).toContain('import requests');
    expect(samples.python).toContain(`"${ENDPOINT}"`);

    expect(samples.node).toContain(`const ENDPOINT = '${ENDPOINT}'`);
    expect(samples.node).toContain("'Bearer ' + API_KEY");
  });

  it('uploads files as multipart in every language', () => {
    const samples = buildOcrApiSamples(ENDPOINT);

    expect(samples.curl).toContain("-F 'files=@scan.pdf'");
    expect(samples.python).toContain('files=files');
    expect(samples.python).toContain('("files", ("scan.pdf"');
    expect(samples.node).toContain('new FormData()');
    expect(samples.node).toContain("form.append('files'");
    // multipart 由运行时自己带 boundary，示例里不能手写 Content-Type
    expect(samples.node).not.toContain('Content-Type');
  });

  it('shows how to read pages out of the response', () => {
    const samples = buildOcrApiSamples(ENDPOINT);

    expect(samples.python).toContain('payload["results"]');
    expect(samples.node).toContain('const { results } = await response.json();');
  });

  it('keeps the sample skeleton free of CJK so the i18n catalog stays clean', () => {
    const samples = buildOcrApiSamples(ENDPOINT);

    for (const code of [samples.curl, samples.python, samples.node]) {
      expect(/[\u3400-\u9fff]/.test(code)).toBe(false);
    }
  });
});
