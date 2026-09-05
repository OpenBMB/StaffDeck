import { api, TENANT_ID } from '@/api/client';

export type RuleSetRead = {
  id: string;
  tenant_id: string;
  key: string;
  name: string;
  description: string;
  management_systems: string[];
  audit_types: string[];
  business_domain: string;
  status: string;
};

export type RuleSetCreate = Omit<RuleSetRead, 'id' | 'tenant_id' | 'status'>;

export type RuleSetVersionRead = {
  id: string;
  tenant_id: string;
  rule_set_id: string;
  version: number;
  status: 'draft' | 'published' | string;
  content_sha256: string;
  published_by_user_id: string | null;
  published_at: string | null;
};

export type RuleExecutionLevel = 'mandatory' | 'warning' | 'guidance';
export type RuleExecutionMethod = 'deterministic' | 'model_assisted';

export type RuleDefinitionDraft = {
  rule_key: string;
  name: string;
  description: string;
  workflow_nodes: string[];
  information_domains: string[];
  document_types: string[];
  field_keys: string[];
  execution_level: RuleExecutionLevel;
  execution_method: RuleExecutionMethod;
  condition: Record<string, unknown>;
  input_requirements: Array<Record<string, unknown>>;
  evidence_requirements: Array<Record<string, unknown>>;
  source_refs: Array<Record<string, unknown>>;
  sequence: number;
  enabled: boolean;
};

export type RuleDefinitionRead = RuleDefinitionDraft & {
  id: string;
  tenant_id: string;
  rule_set_version_id: string;
};

export type RuleValidationResult = {
  errors: string[];
};

const tenantQuery = `tenant_id=${encodeURIComponent(TENANT_ID)}`;
const segment = (value: string) => encodeURIComponent(value);

export function listRuleSets(): Promise<RuleSetRead[]> {
  return api.get<RuleSetRead[]>(`/api/rule-sets?${tenantQuery}`);
}

export function createRuleSet(request: RuleSetCreate): Promise<RuleSetRead> {
  return api.post<RuleSetRead>('/api/rule-sets', {
    tenant_id: TENANT_ID,
    ...request,
  });
}

export function listRuleSetVersions(ruleSetId: string): Promise<RuleSetVersionRead[]> {
  return api.get<RuleSetVersionRead[]>(
    `/api/rule-sets/${segment(ruleSetId)}/versions?${tenantQuery}`,
  );
}

export function createRuleSetVersion(
  ruleSetId: string,
  rules: RuleDefinitionDraft[],
): Promise<RuleSetVersionRead> {
  return api.post<RuleSetVersionRead>(
    `/api/rule-sets/${segment(ruleSetId)}/versions?${tenantQuery}`,
    { rules },
  );
}

export function listRuleDefinitions(
  ruleSetId: string,
  versionId: string,
): Promise<RuleDefinitionRead[]> {
  return api.get<RuleDefinitionRead[]>(
    `/api/rule-sets/${segment(ruleSetId)}/versions/${segment(versionId)}/rules?${tenantQuery}`,
  );
}

export function replaceRuleDefinitions(
  ruleSetId: string,
  versionId: string,
  rules: RuleDefinitionDraft[],
): Promise<RuleSetVersionRead> {
  return api.put<RuleSetVersionRead>(
    `/api/rule-sets/${segment(ruleSetId)}/versions/${segment(versionId)}/rules?${tenantQuery}`,
    { rules },
  );
}

export function validateRuleSetVersion(
  ruleSetId: string,
  versionId: string,
): Promise<RuleValidationResult> {
  return api.post<RuleValidationResult>(
    `/api/rule-sets/${segment(ruleSetId)}/versions/${segment(versionId)}/validate?${tenantQuery}`,
  );
}

export function publishRuleSetVersion(
  ruleSetId: string,
  versionId: string,
): Promise<RuleSetVersionRead> {
  return api.post<RuleSetVersionRead>(
    `/api/rule-sets/${segment(ruleSetId)}/versions/${segment(versionId)}/publish?${tenantQuery}`,
  );
}
