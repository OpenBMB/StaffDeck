export type OidcCallbackResult = {
  token: string | null;
  error: string | null;
  cleanUrl: string;
};

/**
 * 解析登录页 URL 中的 OIDC 回调结果并计算清理后的 URL。
 *
 * 后端在 SSO 成功后 302 到 `/login#oidc_token=<jwt>`（令牌放 fragment，
 * 不进服务器日志）；失败时 302 到 `/login?oidc_error=<消息>`。
 * 令牌/错误被消费后应通过 history.replaceState 落到 cleanUrl，避免刷新重放。
 */
export function consumeOidcCallback(href: string): OidcCallbackResult {
  const url = new URL(href, window.location.origin);

  const hashParams = new URLSearchParams(url.hash.replace(/^#/, ''));
  const token = hashParams.get('oidc_token') || null;
  hashParams.delete('oidc_token');
  const nextHash = hashParams.toString();

  const searchParams = new URLSearchParams(url.search);
  const error = searchParams.get('oidc_error') || null;
  searchParams.delete('oidc_error');
  const nextSearch = searchParams.toString();

  const cleanUrl = `${url.pathname}${nextSearch ? `?${nextSearch}` : ''}${nextHash ? `#${nextHash}` : ''}`;
  return { token, error, cleanUrl };
}
