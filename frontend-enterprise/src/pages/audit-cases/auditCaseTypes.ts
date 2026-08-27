import type {
  AuditCaseManagementOptions,
  AuditCaseManagementPage,
  AuditCaseManagementRead,
  AuditCaseRead,
} from '@/types';

export type AuditCaseListParams = {
  query?: string;
  status?: string;
  management_system?: string;
  report_type?: string;
  offset?: number;
  limit?: number;
};

export type AuditCaseCreateRequest = {
  tenant_id: string;
  organization_name: string;
  report_type: string;
  management_systems: string[];
  knowledge_base_version_ids: string[];
  member_user_ids: string[];
};

export type AuditCaseUpdateRequest = Partial<
  Pick<AuditCaseCreateRequest, 'organization_name' | 'report_type' | 'management_systems' | 'knowledge_base_version_ids'>
>;

export type AuditCaseMemberOption = {
  id: string;
  username: string;
  display_name?: string;
  role: 'admin' | 'member' | string;
  source?: string;
};

export type AuditCaseListState = {
  rows: AuditCaseManagementRead[];
  total: number;
  loading: boolean;
  error: string;
};

export type AuditCaseApi = {
  list: (params: AuditCaseListParams) => Promise<AuditCaseManagementPage>;
  options: () => Promise<AuditCaseManagementOptions>;
  create: (request: AuditCaseCreateRequest) => Promise<AuditCaseRead>;
};
