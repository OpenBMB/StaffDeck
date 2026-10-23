/**
 * 处理人下拉框取值规则：
 * - 空串表示未配置/未指定；
 * - 纯 user_id 表示网页端投递。
 *
 * 渠道功能已整体移除，本模块不再编码 `user_id::channel`。历史 SOP 里残留的
 * 复合值在解析时按 `::` 截断取 user_id，`channel` 恒为 'web'，避免旧草稿在
 * 编辑器里显示成不存在的处理人。
 */
export function formatHandoffAssigneeValue(userId?: string | null): string {
  return userId ? userId.trim() : '';
}

export function parseHandoffAssigneeValue(value: string): {
  userId: string;
  channel: string | null;
} {
  const userId = value.split('::')[0].trim();
  return { userId, channel: userId ? 'web' : null };
}
