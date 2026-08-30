import { ApiError } from '@/api/client';

const STABLE_ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]+$/;

const API_ERROR_MESSAGES: Record<string, string> = {
  KNOWLEDGE_BINDING_REVISION_CONFLICT: '权限配置已被其他管理员更新，请刷新后重新确认。',
  KNOWLEDGE_CONTEXT_MISMATCH: '当前团队、知识库或会话范围不匹配，请刷新后重试。',
  KNOWLEDGE_DEFAULT_NOT_CONFIGURED: '团队尚未设置默认写入知识库，请先选择目标知识库。',
  KNOWLEDGE_GRANT_REQUIRED: '当前员工没有执行此知识库操作所需的权限。',
  KNOWLEDGE_MODE_INVALID: '当前知识库类型不支持此操作。',
  KNOWLEDGE_PUBLISH_CONFLICT: '正式版本已变化，请基于最新版本重新操作。',
  KNOWLEDGE_VERSION_NOT_READY: '知识版本尚未处理完成，暂不能发布。',
  MODEL_API_KEY_REQUIRED: '请填写模型 API Key',
  MODEL_CONFIG_DISABLED: '请先启用该模型，再设为默认',
  MODEL_CONFIG_VERIFICATION_REQUIRED: '请先完成模型测试，再启用或设为默认',
  MODEL_DEFAULT_CONFLICT: '默认模型状态已变化，请刷新后重试',
  MODEL_EXTRA_BODY_UNSUPPORTED: '当前 API 协议不支持额外请求参数',
  MODEL_MAX_OUTPUT_TOKENS_INVALID: 'Max Tokens 必须大于 0',
  MODEL_PROTOCOL_OPTIONS_CONFLICT: '模型协议参数与额外请求参数冲突，请分别配置',
  MODEL_PROTOCOL_OPTIONS_INVALID: '模型协议选项无效，请检查 API 协议与协议参数',
  MODEL_PROTOCOL_UNSUPPORTED: '当前 API 协议不受支持',
  MODEL_TEMPERATURE_INVALID: 'Temperature 超出当前协议允许范围',
  MODEL_VERIFICATION_STALE: '模型测试状态已变化，请重新测试',
};

function stableErrorCode(value: unknown): string | undefined {
  return typeof value === 'string' && STABLE_ERROR_CODE_PATTERN.test(value)
    ? value
    : undefined;
}

function errorMessage(error: unknown): string {
  if (typeof error === 'string') return error;
  return error instanceof Error ? error.message : '';
}

export function apiErrorCode(error: unknown): string | undefined {
  if (error instanceof ApiError && error.code) return stableErrorCode(error.code);
  return stableErrorCode(errorMessage(error));
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  const code = apiErrorCode(error);
  if (code && API_ERROR_MESSAGES[code]) return API_ERROR_MESSAGES[code];

  const message = errorMessage(error);
  if (code && message === code) return `操作失败（错误码：${code}）`;
  return message || fallback;
}
