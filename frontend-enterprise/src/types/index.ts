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
  steps: Array<Record<string, unknown>>;
  interruption_policy: Record<string, string>;
  response_rules: string[];
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
  updated_at: string;
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
  method: string;
  url: string;
  headers: Record<string, unknown>;
  auth: Record<string, unknown>;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  allowed_skills: string[];
  enabled: boolean;
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
  updated_at: string;
};

export type FeedbackMessageRead = {
  id: string;
  tenant_id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  created_at: string;
  feedback_rating?: 'up' | 'down' | null;
  feedback_updated_at?: string;
};

export type FeedbackSessionDetailRead = {
  session: Record<string, unknown>;
  messages: FeedbackMessageRead[];
  feedback: Array<Record<string, unknown>>;
};
