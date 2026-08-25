const HANDOFF_ASSIGNEE_SEPARATOR = '::';

/**
 * 处理人下拉框取值规则:
 * - 空串表示未配置/未指定;
 * - 纯 user_id 表示网页端投递;
 * - `${user_id}::${channel}` 表示按成员已绑定的渠道身份投递(如飞书)。
 */
export function formatHandoffAssigneeValue(
  userId?: string | null,
  channel?: string | null,
): string {
  if (!userId) return '';
  return channel && channel !== 'web'
    ? `${userId}${HANDOFF_ASSIGNEE_SEPARATOR}${channel}`
    : userId;
}

export function parseHandoffAssigneeValue(value: string): {
  userId: string;
  channel: string | null;
} {
  const trimmed = value.trim();
  if (!trimmed) return { userId: '', channel: null };
  const separatorIndex = trimmed.indexOf(HANDOFF_ASSIGNEE_SEPARATOR);
  if (separatorIndex < 0) {
    return { userId: trimmed, channel: 'web' };
  }
  const userId = trimmed.slice(0, separatorIndex).trim();
  const channel = trimmed.slice(separatorIndex + HANDOFF_ASSIGNEE_SEPARATOR.length).trim();
  if (!userId || !channel) {
    return { userId: trimmed, channel: 'web' };
  }
  return { userId, channel };
}
