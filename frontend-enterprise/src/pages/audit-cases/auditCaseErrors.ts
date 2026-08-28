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
  PDF_TEXT_LAYER_MISSING: 'PDF 未检测到可搜索文字；请启用并准备离线 RapidDoc OCR 后重试，或替换为可搜索 PDF',
  EMPTY_EXTRACTED_TEXT: '未提取到可用文本；请启用并准备离线 RapidDoc OCR 后重试，或替换文件',
  OCR_MODEL_MISSING: '未找到离线 OCR 模型，请先准备 RapidDoc 模型后再重试；原始文件仍已保留',
  OCR_DEPENDENCY_MISSING: '离线 OCR 依赖尚未安装或未启用，请先准备 RapidDoc 运行环境后再重试',
  OCR_TIMEOUT: '离线 OCR 处理超时，请检查机器资源后重试；原始文件仍已保留',
  DOCUMENT_EXTRACTION_FAILED: '文档提取失败，请检查文件是否损坏或重试；原始文件仍已保留',
};

export function auditCaseErrorCodeMessage(code: string, fallback: string): string {
  return ERROR_MESSAGES[code] || fallback;
}

export function auditCaseErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.code) {
    return auditCaseErrorCodeMessage(error.code, fallback);
  }
  return error instanceof Error && error.message ? error.message : fallback;
}
