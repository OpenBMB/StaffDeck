// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { consumeOidcCallback } from './oidcCallback';

describe('consumeOidcCallback', () => {
  it('extracts oidc_token from the hash fragment and cleans the URL', () => {
    const result = consumeOidcCallback('/login#oidc_token=abc.def.ghi');
    expect(result.token).toBe('abc.def.ghi');
    expect(result.error).toBeNull();
    expect(result.cleanUrl).toBe('/login');
  });

  it('keeps unrelated hash parameters', () => {
    const result = consumeOidcCallback('/login#oidc_token=tok&theme=dark');
    expect(result.token).toBe('tok');
    expect(result.cleanUrl).toBe('/login#theme=dark');
  });

  it('extracts oidc_error from the query string and cleans the URL', () => {
    const result = consumeOidcCallback('/login?oidc_error=%E5%A4%B1%E8%B4%A5');
    expect(result.token).toBeNull();
    expect(result.error).toBe('失败');
    expect(result.cleanUrl).toBe('/login');
  });

  it('keeps unrelated query parameters', () => {
    const result = consumeOidcCallback('/login?redirect=%2Fworkspace&oidc_error=bad');
    expect(result.error).toBe('bad');
    expect(result.cleanUrl).toBe('/login?redirect=%2Fworkspace');
  });

  it('returns empty result for a plain login URL', () => {
    const result = consumeOidcCallback('/login');
    expect(result.token).toBeNull();
    expect(result.error).toBeNull();
    expect(result.cleanUrl).toBe('/login');
  });
});
