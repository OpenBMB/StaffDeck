export type SkillCard = {
  skill_id: string;
  name: string;
  version: string;
  business_domain?: string;
  description: string;
  trigger_intents: string[];
  user_utterance_examples: string[];
  goal: string[];
  required_info: string[];
  nodes: Array<Record<string, unknown>>;
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
  status: string;
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
  metadata: Record<string, unknown>;
  resources: AgentResourceBindingRead[];
  created_at: string;
  updated_at: string;
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
  homepage?: string;
  skill_markdown: string;
  skill_files: Array<{
    path: string;
    content: string;
    size?: number;
    mime_type?: string;
  }>;
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
  base_url?: string;
  api_key_masked: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
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
  bucket: string;
  tool_type: 'http' | 'mcp' | string;
  method: string;
  url: string;
  headers: Record<string, unknown>;
  auth: Record<string, unknown>;
  mcp_config: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  allowed_skills: string[];
  mcp_server_id?: string | null;
  enabled: boolean;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type MCPTransport = 'stdio' | 'streamable_http' | 'sse' | 'builtin';

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
  enabled: boolean;
  last_synced_at?: string | null;
  tool_count: number;
  created_at: string;
  updated_at: string;
};

export type MCPDiscoveredTool = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  imported: boolean;
  tool_id?: string | null;
  enabled?: boolean | null;
};

export type MCPDiscoverResponse = {
  success: boolean;
  tools: MCPDiscoveredTool[];
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
  python_summary?: string | null;
  error?: string | null;
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

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  metadata?: {
    attachments?: ChatAttachmentRead[];
    knowledge_citations?: KnowledgeCitation[];
    knowledge_query?: Record<string, unknown>;
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
  created_at: string;
  updated_at: string;
};

export type EnterpriseSessionDetailRead = {
  session: EnterpriseChatSessionRead;
  messages: FeedbackMessageRead[];
  events: Array<{
    id: string;
    event_type: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
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
};

export type TurnTraceRead = {
  turn_id: string;
  user_message_id?: string | null;
  started_at: string;
  completed_at?: string | null;
  lines: TraceLineRead[];
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
