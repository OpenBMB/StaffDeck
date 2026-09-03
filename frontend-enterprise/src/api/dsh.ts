import { api } from './client';

export type DshEngineChoice = 'default' | 'legacy' | 'dsh';

export type DshStatus = {
  dsh_enabled: boolean;
  security_profile: string;
  default_engine: DshEngineChoice;
  staff_allowlist: string[];
  fallback_to_legacy: boolean;
  dsh_root: string;
  dsh_home: string;
  runtime_ok: boolean;
  runtime_error?: { code: string; message: string } | null;
  mcp_url?: string | null;
  live_activations: number;
  modules_total: number;
  modules_enabled: number;
  registry_generation: number;
  started_at?: string | null;
  restart_count?: number;
  config_pending?: boolean;
  engine_version?: string | null;
};

export type DshModule = {
  module_id: string;
  name: string;
  summary?: string;
  version: string;
  kind: 'A' | 'C' | 'T' | 'K';
  contract_version: string;
  slot: string;
  enabled: boolean;
  source: string;
  provides: string[];
  requires: string[];
  hooks: string[];
  policy_actions: string[];
  guarded: boolean;
};

export type DshTreeSub = {
  id: string;
  name: string;
  description: string;
  kind: 'A' | 'C' | 'T' | 'K';
  slots: string[];
  legacy: string[];
  modules: DshModule[];
  enabled: number;
  total: number;
};

export type DshTreeBig = {
  id: string;
  name: string;
  root: string;
  description: string;
  order: number;
  pep: boolean;
  edges: Array<{ to: string; label: string }>;
  subs: DshTreeSub[];
  enabled: number;
  total: number;
};

export type DshGrant = {
  operation: string;
  resource_type: string;
  resource_id: string;
  name: string;
  scope: 'general' | 'sop_specific';
  sop_id?: string | null;
  node_id?: string | null;
  slot_name?: string | null;
  required: boolean;
};

export type DshSnapshot = {
  snapshot_id: string;
  staff_id: string;
  persona_preview?: string | null;
  model_route: Record<string, string>;
  session_policy: Record<string, unknown>;
  grants: DshGrant[];
  sops: Array<{
    skill_id: string;
    version: string;
    name: string;
    resolved_slots: Array<{ slot: string; operation: string; resource_type: string; resource_id: string; required: boolean; node_id?: string | null }>;
    sub_sop_ids: string[];
  }>;
  hooks: Record<string, string[]>;
  channels: string[];
  team_id?: string | null;
  proxy_tools: string[];
};

export type DshLedgerRow = {
  id: string;
  session_id: string;
  task_id: string;
  run_id: string;
  tool_name: string;
  status: string;
  side_effect_key?: string | null;
  arguments: Record<string, unknown>;
  error?: { code?: string; message?: string } | null;
  started_at: string;
  finished_at?: string | null;
  engine?: string | null;
};

export type DshStaffEngine = {
  agent_id: string;
  engine: DshEngineChoice;
  effective_engine: 'legacy' | 'dsh';
};

export type DshEvent = {
  id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type DshAssembly = {
  engine: 'dsh' | 'legacy' | string;
  security_profile: string;
  disabled_modules: string[];
  extra_modules: string[];
  updated_at?: string | null;
  updated_by?: string | null;
};

export type DshAssemblyState = {
  saved: DshAssembly;
  applied: DshAssembly | null;
  pending: boolean;
  started_at: string | null;
  restart_count: number;
  last_restart_error: string | null;
  config_path: string;
};

export type DshAssemblyUpdate = {
  engine?: 'dsh' | 'legacy';
  security_profile?: string;
  disabled_modules?: string[];
  extra_modules?: string[];
};

export type DshSessionSummary = {
  session_id: string;
  title: string | null;
  agent_id: string | null;
  agent_name: string | null;
  channel: string | null;
  status: string;
  updated_at: string;
};

export type DshLogEntry = {
  id: string;
  ts: string;
  type: string;
  source: 'event' | 'ledger';
  event_type?: string;
  invocation_id?: string;
  data: Record<string, unknown>;
  engine?: string | null;
  turn_id?: string | null;
};

export type DshLog = {
  session: DshSessionSummary;
  entries: DshLogEntry[];
};

const q = (tenantId: string, extra?: Record<string, string | number | undefined>) => {
  const params = new URLSearchParams({ tenant_id: tenantId });
  Object.entries(extra || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== '') params.set(k, String(v));
  });
  return `?${params.toString()}`;
};

export const dshApi = {
  status: (tenantId: string) => api.get<DshStatus>(`/api/enterprise/dsh/status${q(tenantId)}`),
  modules: (tenantId: string) => api.get<DshModule[]>(`/api/enterprise/dsh/modules${q(tenantId)}`),
  modulesTree: (tenantId: string) => api.get<DshTreeBig[]>(`/api/enterprise/dsh/modules/tree${q(tenantId)}`),
  snapshot: (tenantId: string, agentId?: string) => api.get<DshSnapshot>(`/api/enterprise/dsh/snapshot${q(tenantId, { agent_id: agentId })}`),
  ledgerUnknown: (tenantId: string) => api.get<DshLedgerRow[]>(`/api/enterprise/dsh/ledger/unknown${q(tenantId)}`),
  ledgerRecent: (tenantId: string, sessionId?: string, limit = 50) => api.get<DshLedgerRow[]>(`/api/enterprise/dsh/ledger/recent${q(tenantId, { session_id: sessionId, limit })}`),
  reconcile: (tenantId: string, invocationId: string, status: 'completed' | 'failed') =>
    api.post<DshLedgerRow>(`/api/enterprise/dsh/ledger/${encodeURIComponent(invocationId)}/reconcile`, { tenant_id: tenantId, status }),
  staffEngine: (tenantId: string, agentId: string) => api.get<DshStaffEngine>(`/api/enterprise/dsh/staff/${encodeURIComponent(agentId)}/engine${q(tenantId)}`),
  setStaffEngine: (tenantId: string, agentId: string, engine: DshEngineChoice) =>
    api.put<DshStaffEngine>(`/api/enterprise/dsh/staff/${encodeURIComponent(agentId)}/engine`, { tenant_id: tenantId, engine }),
  events: (tenantId: string, sessionId: string, limit = 200) => api.get<DshEvent[]>(`/api/enterprise/dsh/events/recent${q(tenantId, { session_id: sessionId, limit })}`),
  config: (tenantId: string) => api.get<DshAssemblyState>(`/api/enterprise/dsh/config${q(tenantId)}`),
  setConfig: (tenantId: string, patch: DshAssemblyUpdate) => api.put<DshAssemblyState>('/api/enterprise/dsh/config', { tenant_id: tenantId, ...patch }),
  restart: (tenantId: string) => api.post<{ restart_count: number; restarted_at: string; state: DshAssemblyState; runtime_error?: { code: string; message: string } }>('/api/enterprise/dsh/restart', { tenant_id: tenantId }),
  sessionsRecent: (tenantId: string, limit = 40) => api.get<DshSessionSummary[]>(`/api/enterprise/dsh/sessions/recent${q(tenantId, { limit })}`),
  log: (tenantId: string, sessionId: string, limit = 400) => api.get<DshLog>(`/api/enterprise/dsh/log${q(tenantId, { session_id: sessionId, limit })}`),
};
