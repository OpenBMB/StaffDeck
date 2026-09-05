import { api, TENANT_ID } from '@/api/client';
import {
  listRuleSets,
  listRuleSetVersions,
  type RuleSetRead,
  type RuleSetVersionRead,
} from '../rules/ruleLibraryApi';

export type RuleBindingRead = {
  id: string;
  tenant_id: string;
  audit_case_id: string;
  rule_set_id: string;
  rule_set_version_id: string;
  selection_source: string;
  status: string;
  priority: number;
  bound_by_user_id: string;
  supersedes_binding_id: string | null;
};

export type RuleMigrationPreviewRead = {
  added_rule_keys: string[];
  removed_rule_keys: string[];
  changed_rule_keys: string[];
  unchanged_rule_keys: string[];
  impacted_information_domains: string[];
  impacted_workflow_nodes: string[];
};

export type PublishedRuleVersionOption = {
  ruleSet: Pick<RuleSetRead, 'id' | 'key' | 'name'>;
  version: RuleSetVersionRead;
};

const tenantQuery = `tenant_id=${encodeURIComponent(TENANT_ID)}`;
const segment = (value: string) => encodeURIComponent(value);

export function loadCurrentRuleBindings(caseId: string): Promise<RuleBindingRead[]> {
  return api.get<RuleBindingRead[]>(
    `/api/audit-cases/${segment(caseId)}/rule-bindings?${tenantQuery}`,
  );
}

export async function loadPublishedRuleVersionOptions(): Promise<PublishedRuleVersionOption[]> {
  const ruleSets = await listRuleSets();
  const options = await Promise.all(
    ruleSets.map(async (ruleSet) => {
      const versions = await listRuleSetVersions(ruleSet.id);
      return versions
        .filter((version) => version.status === 'published')
        .map((version) => ({
          ruleSet: {
            id: ruleSet.id,
            key: ruleSet.key,
            name: ruleSet.name,
          },
          version,
        }));
    }),
  );
  return options.flat();
}

export function replaceCurrentRuleBindings(
  caseId: string,
  versionIds: string[],
  selectionSource: 'recommended' | 'manual' = 'manual',
): Promise<RuleBindingRead[]> {
  return api.put<RuleBindingRead[]>(
    `/api/audit-cases/${segment(caseId)}/rule-bindings?${tenantQuery}`,
    { version_ids: versionIds, selection_source: selectionSource },
  );
}

export function previewRuleBindingMigration(
  caseId: string,
  versionIds: string[],
): Promise<RuleMigrationPreviewRead> {
  return api.post<RuleMigrationPreviewRead>(
    `/api/audit-cases/${segment(caseId)}/rule-bindings/migration-preview?${tenantQuery}`,
    { version_ids: versionIds },
  );
}

export function migrateRuleBindings(
  caseId: string,
  versionIds: string[],
  reason: string,
): Promise<RuleBindingRead[]> {
  return api.post<RuleBindingRead[]>(
    `/api/audit-cases/${segment(caseId)}/rule-bindings/migrate?${tenantQuery}`,
    { version_ids: versionIds, reason },
  );
}
