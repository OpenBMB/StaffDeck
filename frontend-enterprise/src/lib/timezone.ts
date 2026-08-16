import { getDateLocale } from '@/i18n';

const FALLBACK_TIME_ZONE = 'Asia/Shanghai';

export function getClientTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || FALLBACK_TIME_ZONE;
  } catch {
    return FALLBACK_TIME_ZONE;
  }
}

export function parseBackendDateTime(value?: string): Date {
  const text = String(value || '').trim();
  if (!text) return new Date('');
  // 纯日期字符串本就被当作 UTC 解析，无需补时区后缀
  if (!text.includes('T')) return new Date(text);
  // 后端时间戳为 naive UTC（无 Z 后缀），缺失时按 UTC 解析而非本地时间
  if (/([zZ]|[+-]\d{2}:\d{2})$/.test(text)) return new Date(text);
  return new Date(`${text}Z`);
}

export function formatClientDateTime(value?: string, emptyText = '-'): string {
  if (!value) return emptyText;
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return emptyText;
  return date.toLocaleString(getDateLocale(), {
    hour12: false,
    timeZone: getClientTimeZone(),
  });
}
