// 飞书身份 scope 由后端 feishu_identity_scope 生成:
// app:{appId长度}:{appId}:tenant:{tenantKey长度}:{tenantKey}
// 长度前缀保证 appId/tenantKey 含冒号时仍可无歧义解析。
export function feishuAppIdFromIdentityScope(scope: string): string | null {
  if (!scope.startsWith('app:')) return null;
  const lengthEnd = scope.indexOf(':', 'app:'.length);
  if (lengthEnd < 0) return null;
  const appIdLength = Number(scope.slice('app:'.length, lengthEnd));
  if (!Number.isInteger(appIdLength) || appIdLength <= 0) return null;
  const appIdStart = lengthEnd + 1;
  const appId = scope.slice(appIdStart, appIdStart + appIdLength);
  if (!appId) return null;
  if (!scope.slice(appIdStart + appIdLength).startsWith(':tenant:')) return null;
  return appId;
}
