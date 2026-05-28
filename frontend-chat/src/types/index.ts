export type ChatSession = {
  id: string;
  tenant_id: string;
  user_id?: string;
  title?: string;
  active_skill_id?: string;
  active_step_id?: string;
  status: string;
  summary?: string;
  last_agent_question?: string;
  updated_at: string;
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  created_at: string;
  feedback_rating?: 'up' | 'down' | null;
  turnId?: string;
  isStreaming?: boolean;
  isError?: boolean;
};

export type ChatTurnResponse = {
  reply: string;
  session_id: string;
  router_decision?: Record<string, unknown>;
  step_result?: Record<string, unknown>;
  tool_result?: Record<string, unknown>;
  session_state: Record<string, unknown>;
};

export type TraceLineRead = {
  id: string;
  kind: 'thinking' | 'decision' | 'skill' | 'tool';
  text: string;
  detail?: string | null;
  state: 'running' | 'completed' | 'failed';
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
  updated_at: string;
};
