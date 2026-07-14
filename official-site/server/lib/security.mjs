import { createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

const COOKIE_NAME = 'staffdeck_site_session';

function encode(value) {
  return Buffer.from(value).toString('base64url');
}
function signature(payload, secret) {
  return createHmac('sha256', secret).update(payload).digest('base64url');
}

export function createSessionToken(secret, ttlSeconds = 7_200, now = Date.now()) {
  const session = {
    sid: randomBytes(18).toString('base64url'),
    csrf: randomBytes(18).toString('base64url'),
    exp: now + ttlSeconds * 1_000,
  };
  const payload = encode(JSON.stringify(session));
  return { session, token: `${payload}.${signature(payload, secret)}` };
}

export function verifySessionToken(token, secret, now = Date.now()) {
  if (!token || typeof token !== 'string') return null;
  const [payload, providedSignature, extra] = token.split('.');
  if (!payload || !providedSignature || extra) return null;
  const expected = signature(payload, secret);
  const left = Buffer.from(providedSignature);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !timingSafeEqual(left, right)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8'));
    if (!session.sid || !session.csrf || !Number.isFinite(session.exp) || session.exp <= now) return null;
    return session;
  } catch {
    return null;
  }
}

export function parseCookies(header = '') {
  return Object.fromEntries(
    header
      .split(';')
      .map((entry) => entry.trim())
      .filter(Boolean)
      .map((entry) => {
        const index = entry.indexOf('=');
        if (index < 0) return [entry, ''];
        return [entry.slice(0, index), decodeURIComponent(entry.slice(index + 1))];
      }),
  );
}

export function sessionCookie(token, { secure = false, maxAge = 7_200 } = {}) {
  return `${COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure ? '; Secure' : ''}`;
}

export function sessionFromRequest(request, secret) {
  const token = parseCookies(request.headers.cookie || '')[COOKIE_NAME];
  return verifySessionToken(token, secret);
}

export function isAllowedOrigin(request, configuredOrigins = []) {
  const origin = request.headers.origin;
  if (!origin) return false;
  if (configuredOrigins.includes(origin)) return true;
  const host = request.headers['x-forwarded-host'] || request.headers.host;
  const protocol = request.headers['x-forwarded-proto'] || 'http';
  return Boolean(host && origin === `${protocol}://${host}`);
}

export function requestIp(request) {
  const forwarded = request.headers['x-forwarded-for'];
  if (typeof forwarded === 'string' && forwarded) return forwarded.split(',')[0].trim();
  return request.socket.remoteAddress || 'unknown';
}

export function createRateLimiter({ limit, windowMs }) {
  const entries = new Map();
  return function consume(key, now = Date.now()) {
    const previous = entries.get(key);
    const current = !previous || previous.resetAt <= now
      ? { count: 0, resetAt: now + windowMs }
      : previous;
    current.count += 1;
    entries.set(key, current);
    if (entries.size > 5_000) {
      for (const [entryKey, entry] of entries) {
        if (entry.resetAt <= now) entries.delete(entryKey);
      }
    }
    return {
      allowed: current.count <= limit,
      remaining: Math.max(0, limit - current.count),
      retryAfterSeconds: Math.max(1, Math.ceil((current.resetAt - now) / 1_000)),
    };
  };
}
