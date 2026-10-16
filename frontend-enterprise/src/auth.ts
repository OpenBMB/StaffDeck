export type EnterpriseAuthUser = {
  id: string;
  tenant_id: string;
  username: string;
  display_name?: string;
  department?: string;
  role: 'admin' | 'member';
  avatar_url?: string;
};

export type EnterpriseAuthSession = {
  token: string;
  user: EnterpriseAuthUser;
  /**
   * 服务端签发的凭证绝对过期时间（Unix 秒）：勾选「记住账号」时是「登录 + 30 天」。
   * 有值时过期即视为未登录（省掉一次注定 401 的 /api/auth/me）；缺省（历史记录、
   * 分享访客的内存态覆盖）按原行为处理。
   */
  expires_at?: number;
};

export const ENTERPRISE_AUTH_STORAGE_KEY = 'ultrarag_auth';

/** 登录页「记住账号」的勾选态：下次进登录页保持上次的选择。 */
export const ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY = 'staffdeck_remember_login';

/** 「记住账号」时一并记住的账号名，用于下次预填（不存密码）。 */
export const ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY = 'staffdeck_remembered_username';

/** 过期判定留 30s 时钟偏差余量：别在边界上拿着刚过期的凭证去发请求。 */
const TOKEN_EXPIRY_SKEW_MS = 30_000;

// 分享页等场景的「内存态身份覆盖」：只在本标签页有效、不落 localStorage，
// 避免访客身份把浏览器里已有的站内登录态冲掉（反之亦然）。
let authOverride: EnterpriseAuthSession | null = null;

export function setEnterpriseAuthOverride(session: EnterpriseAuthSession | null): void {
  authOverride = session;
}

export function getEnterpriseAuthSession(): EnterpriseAuthSession | null {
  return authOverride ?? readStoredSession(ENTERPRISE_AUTH_STORAGE_KEY);
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

/**
 * 用最新的用户资料刷新会话，保留 token 与免登窗口。
 * 头像/部门等资料变更后回写时必须走这里——直接拼 `{ token, user }` 会把
 * expires_at 丢掉，把「记住账号」换来的 30 天窗口在第一次刷新后抹平。
 */
export function mergeEnterpriseAuthSessionUser(
  session: EnterpriseAuthSession,
  user: EnterpriseAuthUser,
): EnterpriseAuthSession {
  return { ...session, user };
}

/** 凭证是否已过期（无 expires_at 的历史会话一律视为未过期，行为不变）。 */
export function isEnterpriseAuthSessionExpired(session: EnterpriseAuthSession): boolean {
  if (!session.expires_at) return false;
  return session.expires_at * 1000 <= Date.now() + TOKEN_EXPIRY_SKEW_MS;
}

/** 上次是否勾选了「记住账号」。 */
export function readRememberLoginPreference(): boolean {
  try {
    return window.localStorage.getItem(ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

export function writeRememberLoginPreference(remember: boolean): void {
  try {
    if (remember) {
      window.localStorage.setItem(ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY, '1');
    } else {
      window.localStorage.removeItem(ENTERPRISE_REMEMBER_LOGIN_STORAGE_KEY);
    }
  } catch {
    // 隐私模式等禁用存储的场景：只影响下次的勾选态预置，登录本身不受影响
  }
}

/** 「记住账号」时记住的账号名；没记住过则为空串。 */
export function readRememberedUsername(): string {
  try {
    return window.localStorage.getItem(ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

/** 传 null / 空串 = 不再记住：连同上次遗留的账号一起清掉。 */
export function writeRememberedUsername(username: string | null): void {
  try {
    const value = (username || '').trim();
    if (value) {
      window.localStorage.setItem(ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY, value);
    } else {
      window.localStorage.removeItem(ENTERPRISE_REMEMBERED_USERNAME_STORAGE_KEY);
    }
  } catch {
    // 同上：存储不可用时静默降级
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
    // 凭证已过期（如 30 天免登窗口走完）：抹掉痕迹直接当未登录，
    // 免得先渲染出壳子再被打回登录页。
    if (isEnterpriseAuthSessionExpired(parsed)) {
      window.localStorage.removeItem(key);
      return null;
    }
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
