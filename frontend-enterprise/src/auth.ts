export type EnterpriseAuthUser = {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  role: 'admin' | 'member';
  avatar_url?: string;
};

export type EnterpriseAuthSession = {
  token: string;
  user: EnterpriseAuthUser;
};

export const ENTERPRISE_AUTH_STORAGE_KEY = 'ultrarag_auth';

export function getEnterpriseAuthSession(): EnterpriseAuthSession | null {
  return readStoredSession(ENTERPRISE_AUTH_STORAGE_KEY);
}

export function setEnterpriseAuthSession(session: EnterpriseAuthSession): void {
  try {
    window.localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, JSON.stringify(session));
  } catch {
    // 存储超限等异常(极端情况):降级为不带头像字段的最小会话再试一次
    try {
      const minimal: EnterpriseAuthSession = {
        ...session,
        user: { ...session.user, avatar_url: undefined },
      };
      window.localStorage.setItem(ENTERPRISE_AUTH_STORAGE_KEY, JSON.stringify(minimal));
    } catch {
      // 抛出真实原因,避免被登录流程误报为账号/密码错误
      throw new Error('浏览器存储空间不足，请清理站点数据后重试');
    }
  }
}

export function clearEnterpriseAuthSession(): void {
  window.localStorage.removeItem(ENTERPRISE_AUTH_STORAGE_KEY);
}

function readStoredSession(key: string): EnterpriseAuthSession | null {
  const raw = window.localStorage.getItem(key);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as EnterpriseAuthSession;
    if (!parsed.token || !parsed.user?.id) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function isEnterpriseAdmin(user?: EnterpriseAuthUser | null): boolean {
  return user?.role === 'admin';
}

export function isGalleryEmployee(agent?: { metadata?: Record<string, unknown> } | null): boolean {
  return agent?.metadata?.published_to_gallery === true;
}

export function isEmployeeOwnedBy(
  agent: { metadata?: Record<string, unknown> },
  user?: EnterpriseAuthUser | null,
): boolean {
  if (!user) return false;
  const metadata = agent.metadata || {};
  const ownerUserId = metadata.owner_user_id;
  return ownerUserId === user.id;
}
