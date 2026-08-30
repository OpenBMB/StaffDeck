export type CapabilityScope = 'general' | 'sop_specific';

export type SkillCapabilityRefs = {
  general_skill_ids: string[];
  tool_ids: string[];
  knowledge_base_ids: string[];
  required_general_skill_ids?: string[];
  required_tool_ids?: string[];
  required_knowledge_base_ids?: string[];
};

export type SkillGraphNode = Record<string, unknown> & {
  capability_refs?: SkillCapabilityRefs;
  sub_sop_id?: string | null;
  /** 人工节点指定处理人（handoff / handoff_human 节点）。 */
  assignee_user_id?: string | null;
  /** 处理人投递渠道:null=默认;'web'=仅网页端;'feishu' 等绑定渠道=按该渠道转接。 */
  assignee_notify_channel?: string | null;
};

export type SkillCard = {
  skill_id: string;
  name: string;
  version: string;
  business_domain?: string;
  description: string;
  capability_scope?: CapabilityScope;
  step_timeout_seconds?: number | null;
  trigger_intents: string[];
  user_utterance_examples: string[];
  goal: string[];
  required_info: string[];
  nodes: SkillGraphNode[];
  edges: Array<Record<string, unknown>>;
  start_node_id: string;
  terminal_node_ids: string[];
  interruption_policy: Record<string, string>;
  response_rules: string[];
};

export type KnowledgeIngestJobRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id?: string;
  filename: string;
  status: string;
  stage: string;
  progress: number;
  error?: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  finished_at?: string;
  updated_at: string;
};

export type KnowledgeBaseRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string;
  capability_scope?: CapabilityScope;
  status: string;
  mode?: 'dedicated' | 'shared';
  published_version_id?: string | null;
  published_version?: string | null;
  bound_team_count?: number;
  version?: string;
  branch_sync_state?: string;
  branch_base_version?: string;
  branch_head_version?: string;
  metadata?: Record<string, unknown>;
  document_count: number;
  bucket_count: number;
  chunk_count: number;
  created_at: string;
  updated_at: string;
};

export type KnowledgeBaseVersionRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  version: string;
  name: string;
  description?: string;
  status: string;
  publication_state?: 'draft' | 'released' | 'rejected';
  parent_version_id?: string | null;
  source_team_id?: string | null;
  created_by_agent_id?: string | null;
  created_by_user_id?: string | null;
  change_reason?: string | null;
  published_at?: string | null;
  is_published_head?: boolean;
  is_head?: boolean;
  is_base?: boolean;
  capability_scope?: CapabilityScope;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type KnowledgeBaseConversionRead = {
  source_knowledge_base_id: string;
  source_version_id: string;
  new_knowledge_base: KnowledgeBaseRead;
  released_version: KnowledgeBaseVersionRead;
  binding_ids: string[];
  default_for_team_id?: string | null;
  source_archived: boolean;
  audit_event_id: string;
};

export type KnowledgeBaseAuditEventRead = {
  id: string;
  knowledge_base_id: string;
  team_id?: string | null;
  team_name?: string | null;
  knowledge_base_version_id?: string | null;
  knowledge_base_version?: string | null;
  actor_type: 'user' | 'agent' | 'system' | string;
  actor_id: string;
  actor_name: string;
  action: string;
  reason?: string | null;
  details: Record<string, unknown>;
  created_at: string;
};

export type KnowledgeBaseAuditPageRead = {
  items: KnowledgeBaseAuditEventRead[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
};

export type KnowledgeDocumentRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id?: string;
  filename: string;
  file_type: string;
  title?: string;
  status: string;
  bucket_count: number;
  chunk_count: number;
  metadata?: Record<string, unknown>;
  error?: string;
  created_at: string;
  updated_at: string;
};

export type KnowledgeBucketRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id: string;
  bucket_key: string;
  title: string;
  summary: string;
  token_estimate: number;
  chunk_count: number;
  status: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type KnowledgeChunkRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id: string;
  bucket_id: string;
  chunk_index: number;
  content: string;
  summary?: string;
  source_ref?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type KnowledgeDiscoveryRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  document_id: string;
  bucket_id?: string;
  suggestion_type: 'skill' | 'tool' | 'warning';
  title: string;
  status: string;
  payload: Record<string, unknown>;
  source_refs: Array<Record<string, unknown>>;
  reason?: string;
  created_at: string;
  updated_at: string;
};

export type KnowledgeConceptRead = {
  id: string;
  tenant_id: string;
  knowledge_base_id: string;
  knowledge_base_version_id?: string;
  document_id?: string;
  concept_id: string;
  concept_type: string;
  title: string;
  description?: string;
  content_md: string;
  frontmatter: Record<string, unknown>;
  links: Array<Record<string, unknown>>;
  citations: Array<Record<string, unknown>>;
  source_refs: Array<Record<string, unknown>>;
  status: string;
  created_at: string;
  updated_at: string;
};

export type KnowledgeSearchEvidence = {
  chunk_id: string;
  document_id: string;
  bucket_id: string;
  source_path?: string;
  section_path?: string;
  summary?: string;
  excerpt: string;
  confidence_reason?: string;
};

export type KnowledgeSearchResponse = {
  selected_buckets: KnowledgeBucketRead[];
  chunks: KnowledgeChunkRead[];
  trace: Array<Record<string, unknown>>;
  route_trace: Array<Record<string, unknown>>;
  selected_documents: Array<Record<string, unknown>>;
  expanded_sections: Array<Record<string, unknown>>;
  selected_concepts: Array<Record<string, unknown>>;
  okf_citations: Array<Record<string, unknown>>;
  evidence_pack: KnowledgeSearchEvidence[];
};

export type AgentResourceType = 'skill' | 'general_skill' | 'knowledge_base' | 'tool';

export type AgentResourceBindingRead = {
  id: string;
  tenant_id: string;
  agent_id: string;
  resource_type: AgentResourceType;
  resource_id: string;
  status: 'active' | 'inactive' | string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentProfileRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string;
  persona_prompt?: string;
  is_overall: boolean;
  status: 'active' | 'archived' | string;
  harness_max_actions?: number;
  metadata: Record<string, unknown>;
  resources: AgentResourceBindingRead[];
  created_at: string;
  updated_at: string;
};

export type AgentModelBindingRead = {
  role: string;
  model_config_id: string;
  effective: boolean;
};

export type ToolSuggestion = {
  name: string;
  display_name?: string;
  description?: string;
  bucket: string;
  tool_type?: 'http' | 'mcp' | string;
  method: string;
  url: string;
  mcp_config?: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  sample_arguments?: Record<string, unknown>;
  source_excerpt?: string;
  probe_result?: ToolProbeResponse;
  reason: string;
  resolution_status?: 'existing' | 'new_candidate' | 'incomplete';
  matched_tool_id?: string;
  matched_tool_name?: string;
  matched_tool_display_name?: string;
  missing_reason?: string;
};

export type ToolProbeResponse = {
  success: boolean;
  status_code?: number;
  data_preview?: unknown;
  inferred_output_schema: Record<string, unknown>;
  error?: {
    code: string;
    message: string;
  };
};

export type SkillRead = {
  id: string;
  tenant_id: string;
  skill_id: string;
  name: string;
  version: string;
  business_domain?: string;
  description?: string;
  content: SkillCard;
  status: 'draft' | 'published' | 'archived';
  call_count: number;
  positive_feedback_count: number;
  negative_feedback_count: number;
  positive_rate: number;
  negative_rate: number;
  total_call_count: number;
  total_positive_feedback_count: number;
  total_negative_feedback_count: number;
  total_positive_rate: number;
  total_negative_rate: number;
  recent_versions: string[];
  recent_call_count: number;
  recent_positive_feedback_count: number;
  recent_negative_feedback_count: number;
  recent_positive_rate: number;
  recent_negative_rate: number;
  agent_id?: string;
  branch_status?: string;
  branch_sync_state?: string;
  branch_base_version?: string;
  branch_head_version?: string;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type SkillVersionRead = SkillRead & {
  created_at: string;
};

export type GeneralSkillRead = {
  id: string;
  tenant_id: string;
  slug: string;
  name: string;
  description?: string;
  capability_scope?: CapabilityScope;
  homepage?: string;
  skill_markdown: string;
  skill_files: Array<{
    path: string;
    content: string;
    size?: number;
    mime_type?: string;
  }>;
  skill_directories?: string[];
  metadata: Record<string, unknown>;
  status: 'draft' | 'published' | 'archived';
  permissions: Record<string, unknown>;
  runtime_config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type GeneralSkillRunResponse = {
  skill_slug: string;
  execution_trace: Array<Record<string, unknown>>;
  generated_code: string;
  stdout: string;
  stderr: string;
  structured_result: Record<string, unknown>;
  reply: string;
};

export type ModelConfigRead = {
  id: string;
  tenant_id: string;
  name: string;
  provider: string;
  api_protocol: 'openai_chat_completions' | 'openai_responses' | 'anthropic_messages' | 'gemini_generate_content';
  base_url?: string;
  api_key_masked: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
  extra_body: Record<string, unknown>;
  protocol_options: Record<string, unknown>;
  legacy_unmapped_options: Record<string, unknown>;
  trust_status: 'legacy_trusted' | 'unverified' | 'verified';
  verification_attempt_status: 'idle' | 'verifying' | 'succeeded' | 'failed';
  config_revision: number;
  security_revision: number;
  is_default: boolean;
  enabled: boolean;
  updated_at: string;
};

export type PersonaRead = {
  tenant_id: string;
  system_prompt: string;
  updated_at: string;
};

export type UIConfigRead = {
  tenant_id: string;
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: number;
  agent_loop_max_actions: number;
  context_token_budget: number;
  context_compaction_trigger_ratio: number;
  context_recent_round_limit: number;
  context_long_summary_token_budget: number;
  context_medium_summary_token_budget: number;
  context_allowed_roles: Array<'user' | 'assistant'>;
  context_long_summary_prefix: string;
  context_medium_summary_prefix: string;
  sandbox_enabled: boolean;
  harness_storage_path: string;
  effective_harness_storage_path: string;
  restart_scheduled?: boolean;
  sandbox_network_mode: 'all' | 'allowlist' | 'deny';
  sandbox_allowed_domains: string[];
  sandbox_backend?: string | null;
  sandbox_setup_required?: boolean;
  sandbox_setup_instructions?: string | null;
  sandbox_status?: 'ready' | 'unavailable' | 'degraded' | 'disabled';
  sandbox_status_code?: string | null;
  sandbox_status_message?: string | null;
  sandbox_status_remediation?: string | null;
  updated_at: string;
};

export type MemoryRead = {
  id: string;
  tenant_id: string;
  user_id: string;
  username?: string;
  session_id?: string;
  kind: string;
  content: string;
  importance: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ToolRead = {
  id: string;
  tenant_id: string;
  name: string;
  display_name?: string;
  description?: string;
  capability_scope?: CapabilityScope;
  bucket: string;
  tool_type: 'http' | 'a2a' | 'mcp' | string;
  method: string;
  url: string;
  headers: Record<string, unknown>;
  auth: Record<string, unknown>;
  mcp_config: Record<string, unknown>;
  execution_policy?: {
    timeout_seconds: number;
  } | null;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  allowed_skills: string[];
  mcp_server_id?: string | null;
  enabled: boolean;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type A2ATaskEventRead = {
  sequence: number;
  event_type: string;
  data: Record<string, unknown>;
  created_at: string;
};

export type A2ATaskRunRead = {
  id: string;
  direction: string;
  remote_task_id?: string | null;
  context_id?: string | null;
  codex_session_id?: string | null;
  status: string;
  endpoint_url: string;
  protocol_version: string;
  cancel_requested: boolean;
  recovery_attempts: number;
  artifacts: Record<string, unknown>[];
  error: Record<string, unknown>;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  updated_at: string;
  events: A2ATaskEventRead[];
};

export type CodexA2AAdapterRead = {
  enabled: boolean;
  endpoint_url: string;
  agent_card_url: string;
  command: string;
  workspace_root: string;
  timeout_seconds: number;
  token_configured: boolean;
};

export type MCPTransport = 'stdio' | 'streamable_http' | 'sse' | 'builtin';
export type MCPAppsMode = 'disabled' | 'auto';

export type MCPServerConnection = {
  transport: MCPTransport;
  url?: string | null;
  headers: Record<string, string>;
  command?: string | null;
  args: string[];
  env: Record<string, string>;
  cwd?: string | null;
};

export type MCPServerRead = {
  id: string;
  tenant_id: string;
  name: string;
  display_name?: string;
  description?: string;
  bucket: string;
  connection: MCPServerConnection;
  apps_mode: MCPAppsMode;
  apps_negotiated: boolean;
  negotiated_capabilities: Record<string, unknown>;
  capability_scope?: CapabilityScope;
  enabled: boolean;
  last_synced_at?: string | null;
  tool_count: number;
  created_at: string;
  updated_at: string;
};

export type MCPDiscoveredTool = {
  name: string;
  title: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  annotations: Record<string, unknown>;
  meta: Record<string, unknown>;
  app?: { resource_uri: string; visibility: string[] } | null;
  imported: boolean;
  tool_id?: string | null;
  enabled?: boolean | null;
  capability_scope?: CapabilityScope | null;
};

export type MCPDiscoverResponse = {
  success: boolean;
  tools: MCPDiscoveredTool[];
  server_capabilities: Record<string, unknown>;
  server_info: Record<string, unknown>;
  error?: { code: string; message: string } | null;
};

export type MCPSyncResponse = {
  success: boolean;
  imported: string[];
  updated: string[];
  removed: string[];
  error?: { code: string; message: string } | null;
};

export type ScheduledTaskRead = {
  id: string;
  tenant_id: string;
  agent_id: string;
  created_by_user_id: string;
  title: string;
  prompt: string;
  description?: string;
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly' | string;
  schedule: Record<string, unknown>;
  timezone: string;
  rrule?: string;
  status: 'active' | 'paused' | 'completed' | 'archived' | string;
  concurrency_policy: string;
  misfire_policy: string;
  max_runs?: number;
  end_at?: string;
  next_run_at?: string;
  last_run_at?: string;
  last_status?: string;
  run_count: number;
  source_session_id?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ScheduledTaskRunRead = {
  id: string;
  tenant_id: string;
  scheduled_task_id: string;
  task_title?: string;
  task_status?: string;
  agent_id: string;
  user_id: string;
  session_id?: string;
  scheduled_for: string;
  status: string;
  started_at?: string;
  finished_at?: string;
  result_summary?: string;
  error?: string;
  trace: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ChatTurnResponse = {
  reply: string;
  session_id: string;
  router_decision?: Record<string, unknown>;
  step_result?: Record<string, unknown>;
  tool_result?: Record<string, unknown>;
  session_state: Record<string, unknown>;
};

// ---------------------------------------------------------------------------
// Chat conversation types
// ---------------------------------------------------------------------------

export type ChatSession = {
  id: string;
  tenant_id: string;
  user_id?: string;
  agent_id?: string;
  title?: string;
  active_skill_id?: string;
  active_step_id?: string;
  status: string;
  summary?: string;
  last_agent_question?: string;
  is_scheduled?: boolean;
  /** 团队会话归属（后端逐步放开，可能缺省）。 */
  team_id?: string | null;
  team_name?: string | null;
  updated_at: string;
};

export type ChatAttachmentKind = 'text' | 'pdf' | 'image' | 'binary';

export type ChatAttachmentRead = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  kind: ChatAttachmentKind;
  text?: string | null;
  preview?: string | null;
  data_url?: string | null;
  sandbox_path?: string | null;
  sha256?: string | null;
  python_summary?: string | null;
  error?: string | null;
};

export type ChatSlashCommand = {
  kind: 'sop' | 'skill' | 'tool';
  target: string;
  label: string;
  description: string;
  command: string;
};

export type KnowledgeCitation = {
  id: string;
  label?: string;
  kind?: 'evidence' | 'concept' | 'okf' | string;
  title?: string;
  source_path?: string;
  section_path?: string;
  content?: string;
  excerpt?: string;
  summary?: string;
  confidence_reason?: string;
  document_id?: string;
  bucket_id?: string;
  chunk_id?: string;
  concept_id?: string;
  concept_type?: string;
};

export type HarnessWorkspaceArtifact = {
  type: 'workspace_file';
  task_frame_id: string;
  path: string;
  sandbox_path?: string | null;
  sha256?: string | null;
  size?: number | null;
  display_name?: string | null;
  description?: string | null;
  content_type?: string | null;
  operation?: string | null;
  source?: string | null;
};

export type HarnessArtifact =
  | HarnessWorkspaceArtifact
  | {
      type: 'human_handoff' | string;
      handoff_id?: string | null;
      [key: string]: unknown;
    };

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  metadata?: {
    attachments?: ChatAttachmentRead[];
    knowledge_citations?: KnowledgeCitation[];
    knowledge_query?: Record<string, unknown>;
    harness_artifacts?: HarnessArtifact[];
    [key: string]: unknown;
  };
  created_at: string;
  feedback_rating?: 'up' | 'down' | null;
  turn_id?: string | null;
  turnId?: string;
  serverMessageId?: string;
  isStreaming?: boolean;
  isError?: boolean;
};

export type ChatSessionEventRead = {
  id: string;
  created_at: string;
  run_id?: string;
  seq?: number;
  event: string;
  data: Record<string, unknown>;
};

export type HumanHandoffRead = {
  id: string;
  tenant_id: string;
  session_id: string;
  agent_id?: string | null;
  requester_user_id?: string | null;
  assignee_user_id?: string | null;
  trigger_skill_id?: string | null;
  trigger_step_id?: string | null;
  context_summary?: string | null;
  pending_question?: string | null;
  status: string;
  human_reply?: string | null;
  resume_payload?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  answered_at?: string | null;
};

export type ScheduledTaskDraftRead = {
  should_create: boolean;
  tenant_id: string;
  agent_id: string;
  title: string;
  prompt: string;
  description?: string;
  schedule_type: 'once' | 'daily' | 'weekly' | 'monthly' | string;
  schedule: Record<string, unknown>;
  timezone: string;
  rrule?: string;
  confidence: number;
  reason?: string;
  source_session_id?: string;
};

export type EnterpriseChatSessionRead = {
  id: string;
  tenant_id: string;
  user_id?: string;
  agent_id?: string;
  title?: string;
  active_skill_id?: string;
  active_step_id?: string;
  status: string;
  summary?: string;
  last_agent_question?: string;
  channel?: string | null;
  session_username?: string;
  session_display_name?: string;
  created_at: string;
  updated_at: string;
};

export type EnterpriseSessionDetailRead = {
  session: EnterpriseChatSessionRead;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
  events: Array<{
    id: string;
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
  traces: TurnTraceRead[];
  tool_invocations?: Array<{
    id: string;
    task_id: string;
    run_id: string;
    call_id: string;
    tool_name: string;
    status: string;
    arguments: Record<string, unknown>;
    result: Record<string, unknown>;
    replayed_from_invocation_id?: string | null;
    started_at: string;
    finished_at?: string | null;
  }>;
};

export type AgentWorkRecordEventRead = {
  id: string;
  kind: 'chat' | 'task' | 'sop' | 'tool' | 'knowledge' | 'skill';
  phase: 'reply' | 'last_run' | 'next_run' | 'assigned';
  timestamp: string;
  label: string;
};

export type AgentWorkRecordRead = {
  agent_id: string;
  timezone: string;
  generated_at: string;
  reply_stats: {
    total: number;
    today: number;
    by_day: Record<string, number>;
  };
  events: AgentWorkRecordEventRead[];
};

export type TraceLineRead = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool' | 'code' | 'knowledge';
  text: string;
  detail?: string | null;
  code?: string | null;
  language?: string | null;
  output?: string | null;
  outputLanguage?: string | null;
  outputTitle?: string | null;
  state: 'running' | 'completed' | 'failed';
  collapsible?: boolean | null;
  duration_ms?: number | null;
  model_duration_ms?: number | null;
  model_call_count?: number | null;
  model_names?: string[] | null;
  depth?: number | null;
};

export type TurnTraceRead = {
  turn_id: string;
  user_message_id?: string | null;
  started_at: string;
  completed_at?: string | null;
  lines: TraceLineRead[];
  duration_ms?: number | null;
  model_duration_ms?: number | null;
  model_call_count?: number | null;
  model_names?: string[] | null;
};

export type TraceSummary = {
  session_id: string;
  user_id?: string;
  active_skill_id?: string;
  active_step_id?: string;
  last_decision?: Record<string, unknown>;
  last_message?: string;
  last_message_time?: string;
  tool_call_count: number;
  status: string;
  updated_at: string;
};

export type FeedbackSessionRead = {
  session_id: string;
  tenant_id: string;
  agent_id?: string;
  user_id?: string;
  username?: string;
  display_name?: string;
  title?: string;
  summary?: string;
  status: string;
  feedback_count: number;
  latest_feedback_at: string;
  latest_message_id: string;
  latest_message: string;
  analysis_status?: string;
  analysis_bucket?: string;
  analysis_bucket_label?: string;
  analysis_summary?: string;
  primary_bucket?: string;
  primary_bucket_label?: string;
  bucket_counts?: Record<string, number>;
  updated_at: string;
};

export type FeedbackAnalysisRead = {
  status?: string;
  bucket?: string;
  bucket_label?: string;
  reason?: string;
  summary?: string;
  confidence?: number;
  metadata?: Record<string, unknown>;
  analyzed_at?: string | null;
};

export type FeedbackMessageRead = {
  id: string;
  tenant_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  created_at: string;
  feedback_id?: string;
  feedback_rating?: 'up' | 'down' | null;
  feedback_updated_at?: string;
  feedback_analysis?: FeedbackAnalysisRead;
};

export type FeedbackSessionDetailRead = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
};

export type FeedbackSummaryRead = {
  total_feedback: number;
  down_count: number;
  up_count: number;
  bucket_counts: Array<{ bucket: string; label: string; count: number }>;
  status_counts: Record<string, number>;
  summary: string;
  top_summaries: Array<Record<string, unknown>>;
};

export type ChannelBindingAgentRead = {
  agent_id: string;
  name: string;
  is_default: boolean;
  sort_order: number;
};

export type ChannelBindingRead = {
  id: string;
  tenant_id: string;
  agent_id: string;
  channel: string;
  /** 用户可编辑的接入显示名；为空时回退展示渠道类型名。 */
  name?: string | null;
  status: string;
  connected: boolean;
  ilink_bot_id?: string | null;
  baseurl?: string | null;
  bot_id?: string | null;
  corp_id?: string | null;
  app_id?: string | null;
  client_id?: string | null;
  bot_open_id?: string | null;
  bot_name?: string | null;
  provider_tenant_key?: string | null;
  config_revision?: number;
  session_expired?: boolean;
  bound_at?: string | null;
  created_by_user_id?: string | null;
  created_by_name?: string | null;
  config_json?: Record<string, unknown>;
  agents: ChannelBindingAgentRead[];
  /** 团队绑定（与 agent 挂载互斥，后端逐步放开，可能缺省）。 */
  team_id?: string | null;
  team_name?: string | null;
  auto_route?: boolean;
  /** 渠道默认人工处理人（SOP 节点未指定 assignee 时回退到此值）。 */
  default_handoff_assignee_user_id?: string | null;
  default_handoff_assignee_name?: string | null;
  /** 处理人投递渠道:null=默认;'web'=仅网页端;'feishu' 等绑定渠道=按该渠道转接。 */
  default_handoff_assignee_channel?: string | null;
  identity_scope_key?: string | null;
  /** 当前请求者对该绑定的管理角色:admin/owner/collaborator;无关系时为 null */
  my_role?: string | null;
  created_at: string;
  updated_at: string;
};

export type ChannelBindingManagerRead = {
  user_id: string;
  name?: string | null;
  granted_at: string;
  granted_by_user_id?: string | null;
  granted_by_name?: string | null;
};

export type ChannelDeliveryRead = {
  id: string;
  kind: string;
  text: string;
  status: string;
  attempts: number;
  last_error?: string;
  created_at: string;
  delivered_at?: string;
  target_json: Record<string, unknown>;
};

export type ChannelConversationRead = {
  session_id: string;
  external_conv_id: string;
  display_name: string;
  is_group: boolean;
  agent_id: string;
  agent_name: string;
  message_count: number;
  last_message_preview: string;
  updated_at: string;
};

export type ChannelConversationAttachment = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  kind: 'text' | 'pdf' | 'image' | 'binary';
};

export type ChannelConversationMessageRead = {
  id: string;
  role: string;
  content: string;
  created_at: string;
  attachments?: ChannelConversationAttachment[] | null;
};

export type ChannelBindCodeRead = {
  code: string;
  expires_at: string;
};

export type PagedResponse<T> = {
  items: T[];
  total: number;
  offset: number;
  limit: number;
};

export type ChannelDeliveryDay = {
  date: string;
  count: number;
  items: ChannelDeliveryRead[];
};

export type ChannelDeliveryDayPage = {
  days: ChannelDeliveryDay[];
  total_days: number;
  offset: number;
  limit: number;
};

export type ChannelIdentityBindingRead = {
  channel: string;
  external_user_id: string;
  display_name: string;
  bound_at: string;
  external_account_scope?: string | null;
};

export type ChannelCredentialFieldRead = {
  key: string;
  label: string;
  placeholder?: string;
  secret?: boolean;
  optional?: boolean;
};

export type ChannelMetaRead = {
  channel: string;
  name: string;
  setup: 'qrcode' | 'credentials' | string;
  credential_fields?: ChannelCredentialFieldRead[];
  capabilities: string[];
};

// ---------------------------------------------------------------------------
// Multi-agent teams (增量 1)
// ---------------------------------------------------------------------------

export type TeamMemberRead = {
  id: string;
  team_id: string;
  agent_id: string;
  role: 'leader' | 'member' | string;
  agent_name?: string | null;
  created_at: string;
};

export type TeamRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string | null;
  owner_user_id: string;
  default_knowledge_base_id?: string | null;
  config: Record<string, unknown>;
  status: 'active' | 'archived' | string;
  members: TeamMemberRead[];
  created_at: string;
  updated_at: string;
};

export type KnowledgePermission = 'reader' | 'editor' | 'publisher';

export type TeamKnowledgeGrantRead = {
  agent_id: string;
  permission: KnowledgePermission;
  status: string;
};

export type TeamKnowledgeBindingRead = {
  id: string;
  team_id: string;
  knowledge_base_id: string;
  knowledge_base_name: string;
  status: string;
  revision: number;
  is_default: boolean;
  published_version_id?: string | null;
  published_version?: string | null;
  grants: TeamKnowledgeGrantRead[];
  created_at: string;
  updated_at: string;
};

export type TeamKnowledgeGrantInput = {
  agent_id: string;
  permission: KnowledgePermission | null;
};

export type TeamConversationKind = 'tl_chat' | 'member_task' | 'member_bid' | 'tl_review';

export type TeamConversationRead = {
  session_id: string;
  kind: TeamConversationKind;
  agent_id?: string | null;
  agent_name?: string | null;
  task_id?: string | null;
  task_status?: string | null;
  needs_input?: boolean;
  pending_question?: string | null;
  title: string;
  preview: string;
  created_at: string;
  updated_at: string;
};

export type TeamConversationsResponse = {
  team_id: string;
  team_name: string;
  tl?: {
    agent_id: string;
    agent_name?: string | null;
    session_id?: string | null;
  } | null;
  conversations: TeamConversationRead[];
};

export type TeamConversationMessageRead = {
  id: string;
  role: string;
  content: string;
  metadata?: ChatMessage['metadata'];
  turn_id?: string | null;
  created_at: string;
};

export type TeamConversationStreamRead = {
  status: 'idle' | 'running' | 'completed' | 'failed';
  content: string;
  phase?: string | null;
  updated_at?: string | null;
};

export type TeamTaskEventRead = {
  id: string;
  task_id: string;
  team_id: string;
  actor_type: string;
  actor_id?: string | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type TeamTaskBidRead = {
  id: string;
  task_id: string;
  agent_id: string;
  agent_name?: string | null;
  round: number;
  kind: 'statement' | 'rebuttal' | string;
  content: string;
  score?: number | null;
  score_rationale?: string | null;
  created_at: string;
};

export type TeamTaskRead = {
  id: string;
  team_id: string;
  tenant_id: string;
  team_run_id?: string | null;
  source_turn_id?: string | null;
  parent_task_id?: string | null;
  title: string;
  description?: string | null;
  priority: string;
  status:
    | 'bidding'
    | 'blocked'
    | 'pending'
    | 'in_progress'
    | 'review'
    | 'done'
    | 'rework'
    | 'escalated'
    | string;
  created_by_user_id?: string | null;
  created_by_tl: boolean;
  assignee_agent_id?: string | null;
  session_id?: string | null;
  depends_on_task_ids?: string[];
  activation_condition?: Record<string, unknown>;
  report: Record<string, unknown>;
  review: Record<string, unknown>;
  version?: number;
  events?: TeamTaskEventRead[];
  bids?: TeamTaskBidRead[];
  created_at: string;
  updated_at: string;
};

export type TeamTLChatResponse = {
  reply: string;
  session_id: string;
  created_tasks: TeamTaskRead[];
};

export type TeamReviewVerdict = 'approve' | 'rework' | 'escalate';

// ---------------------------------------------------------------------------
// Team blackboard (增量 2)
// ---------------------------------------------------------------------------

export type TeamBlackboardEntryRead = {
  id: string;
  team_id: string;
  tenant_id: string;
  content: string;
  tags: string[];
  source_type: 'member' | 'leader' | 'human' | string;
  source_agent_id?: string | null;
  source_task_id?: string | null;
  citation: Record<string, unknown>;
  status: string;
  pinned: boolean;
  created_at: string;
  updated_at: string;
};

// ---------------------------------------------------------------------------
// Team threads / events / settings (增量 4)
// ---------------------------------------------------------------------------

export type TeamThreadRead = {
  team_id: string;
  team_name: string;
  kind: 'tl_chat' | 'task' | string;
  session_id: string;
  task_id?: string | null;
  title: string;
  task_status?: string | null;
  updated_at: string;
};

export type TeamEventRead = {
  id: string;
  task_id?: string | null;
  task_title?: string | null;
  actor_type: string;
  actor_id?: string | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};
