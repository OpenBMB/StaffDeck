/** Keep parallel deployments on one origin inside their own routing and storage namespace. */
export const APP_BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');

export function appPath(path: string): string {
  if (!path.startsWith('/') || path.startsWith('//') || !APP_BASE) return path;
  if (path === APP_BASE || path.startsWith(`${APP_BASE}/`)) return path;
  return `${APP_BASE}${path}`;
}
