import { ApiError } from '@/api/client';

const ERROR_MESSAGES: Record<string, string> = {
  AUDIT_CASE_ADMIN_REQUIRED: '仅管理员可以维护认证项目',
  AUDIT_CASE_ACCESS_DENIED: '你无权访问这个认证项目',
  AUDIT_CASE_NOT_FOUND: '项目不存在或你无权访问',
  AUDIT_CASE_READ_ONLY: '项目已归档，不能继续修改',
  INVALID_PROJECT_MEMBER: '所选成员不存在或不是内部账号',
  INVALID_KNOWLEDGE_VERSION: '所选知识库版本不可用',
  MATERIAL_ALREADY_EXISTS: '该文件已经上传',
  MATERIAL_CATEGORY_CONFLICT: '相同文件已存在于其他材料分类',
  UNSUPPORTED_DOCUMENT_FORMAT: '文件格式暂不支持，请将 .doc 转换为 .docx',
  AUDIT_MATERIAL_TOO_LARGE: '单个文件不能超过 50 MB',
};

export function auditCaseErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code && ERROR_MESSAGES[error.code]) {
    return ERROR_MESSAGES[error.code];
  }
  return error instanceof Error && error.message ? error.message : fallback;
}
