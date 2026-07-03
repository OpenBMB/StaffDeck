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
  updated_at: string;
};

export type AgentResourceType = 'skill' | 'general_skill' | 'knowledge_base' | string;

export type AgentResourceBindingRead = {
  id: string;
  agent_id: string;
  resource_type: AgentResourceType;
  resource_id: string;
  resource_version_id?: string | null;
  tenant_id?: string;
  display_name?: string | null;
  status: string;
  metadata?: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type AgentProfileRead = {
  id: string;
  tenant_id: string;
  name: string;
  description?: string;
  persona_prompt?: string;
  is_overall: boolean;
  status: string;
  metadata: Record<string, unknown>;
  resources?: AgentResourceBindingRead[];
  created_at?: string;
  updated_at?: string;
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
  turnId?: string;
  serverMessageId?: string;
  isStreaming?: boolean;
  isError?: boolean;
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
  excerpt?: string;
  summary?: string;
  confidence_reason?: string;
  document_id?: string;
  bucket_id?: string;
  chunk_id?: string;
  concept_id?: string;
  concept_type?: string;
};

export type ChatTurnResponse = {
  reply: string;
  session_id: string;
  router_decision?: Record<string, unknown>;
  step_result?: Record<string, unknown>;
  tool_result?: Record<string, unknown>;
  session_state: Record<string, unknown>;
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

export type UIConfigRead = {
  tenant_id: string;
  show_thinking_trace: boolean;
  show_skill_trace: boolean;
  show_tool_trace: boolean;
  reflection_max_rounds: number;
  agent_loop_max_actions: number;
  updated_at: string;
};

export type ModelConfigRead = {
  id: string;
  tenant_id: string;
  name: string;
  provider: string;
  base_url?: string | null;
  api_key_masked: string;
  model: string;
  temperature: number;
  max_output_tokens: number;
  is_default: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
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

export type ScheduledTaskRead = {
  id: string;
  tenant_id: string;
  agent_id: string;
  title: string;
  prompt: string;
  description?: string;
  schedule_type: string;
  schedule: Record<string, unknown>;
  timezone: string;
  rrule?: string;
  status: string;
  next_run_at?: string;
};
