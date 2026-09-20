import fs from "node:fs";
import path from "node:path";

const root = process.env.PARITY_SOURCE_ROOT;
const scenario = JSON.parse(process.env.PARITY_SCENARIO_JSON);
const mock = process.env.PARITY_MOCK_BASE_URL;
const output = process.env.PARITY_TRACE_OUT;
const runKey = process.env.PARITY_RUN_KEY ?? `${scenario.scenarioId}:pilotdeck-native`;
const sessionModule = path.join(root, "dist/src/agent/session/createAgentSession.js");
if (!fs.existsSync(sessionModule)) throw new Error(`PilotDeck baseline build is missing ${sessionModule}`);
const { createAgentSession } = await import(pathToUrl(sessionModule));
const { ToolRegistry } = await import(pathToUrl(path.join(root, "dist/src/tool/registry/ToolRegistry.js")));
const { createDefaultPermissionContext } = await import(pathToUrl(path.join(root, "dist/src/permission/protocol/types.js")));

let sequence = 0;
let modelAttempt = 0;
let eventId = 0;
const trace = [];
const push = (kind, extra = {}) => trace.push({ kind, scenarioId: scenario.scenarioId, q: scenario.q, sequence: sequence++, ...extra });
const post = async (suffix, body, signal) => (await fetch(`${mock}${suffix}`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ ...body, runKey }),
  signal,
})).json();
const modelFaultAt = (attempt) => (scenario.faults?.model ?? []).find((fault) => (fault.at ?? 1) === attempt);
const registry = new ToolRegistry();
const limits = scenario.limits ?? {};
const scenarioHasPermissionPolicy = ["allow", "deny", "ask"].some((behavior) =>
  Array.isArray(scenario.permission?.[behavior]) && scenario.permission[behavior].length > 0,
);
const allowedReadFiles = new Set(scenario.seedState?.allowedReadFiles ?? []);
let cancelled = false;
let deadlineExceeded = false;
let toolCancelTimer;
let session;

function toolIsAllowed(name, input) {
  const asked = scenario.permission?.ask?.includes(name) ?? false;
  if (name === "read_file") return allowedReadFiles.has(String(input?.path ?? ""));
  return !(scenario.permission?.deny?.includes(name) ?? false) && !(asked && scenario.permission?.answer === "deny");
}

function cancelRun(reason) {
  if (cancelled) return;
  cancelled = true;
  void post("/control/cancel", {}).catch(() => {}).finally(() => session?.abort(reason));
}

for (const name of scenario.tools ?? []) {
  const concurrencySafe = ["lookup", "summarize"].includes(name);
  registry.register({
    name, description: name, kind: "custom", inputSchema: { type: "object" },
    // Exercise the permission decision seam whenever the scenario declares policy.
    isReadOnly: () => !scenarioHasPermissionPolicy && !["restricted", "loop", "read_file"].includes(name),
    isConcurrencySafe: () => concurrencySafe,
    checkPermissions: async (input) => {
      const allowed = toolIsAllowed(name, input);
      push("permission.decision", { toolName: name, allowed });
      return !allowed
        ? { type: "deny", message: "Deterministic permission denial.", reason: { type: "tool", toolName: name, message: "denied" } }
        : { type: "allow", reason: { type: "tool", toolName: name, message: "allowed" } };
    },
    execute: async (input, context) => {
      const toolCallId = context.currentToolCallId;
      push("tool.call", { name, arguments: input, toolCallId, concurrencySafe });
      if (limits.cancelAfterToolStartMs && !toolCancelTimer) {
        toolCancelTimer = setTimeout(() => cancelRun("parity_cancel"), limits.cancelAfterToolStartMs);
      }
      let result;
      try {
        result = await post("/tools/execute", {
          scenarioId: scenario.scenarioId,
          q: scenario.q,
          name,
          arguments: input,
          permissionAllowed: toolIsAllowed(name, input),
          delays: scenario.delays,
          toolDelays: scenario.toolDelays,
          faults: scenario.faults,
        }, context.abortSignal);
      } catch (error) {
        if (!context.abortSignal?.aborted) throw error;
        const cancelledResult = {
          type: "error",
          toolName: name,
          error: {
            code: "CANCELLED",
            message: "Deterministic tool cancellation.",
            retryable: false,
          },
        };
        push("tool.result", { result: cancelledResult, toolCallId, concurrencySafe });
        return { content: [], data: {} };
      }
      push("tool.result", { result, toolCallId, concurrencySafe });
      if (result.type === "error") throw Object.assign(new Error(result.error.message), { code: result.error.code });
      return { content: [{ type: "text", text: JSON.stringify(result.data) }], data: result.data };
    },
  });
}
const router = {
  decide: async () => ({ provider: "parity", model: "deterministic", scenarioType: "default", isSubagent: false, orchestrating: false, resolvedFrom: "fallback", mutations: {} }),
  materializeRequest: (_decision, request) => request,
  execute: async function* (_decision, request, context) {
    yield* this.stream(request, context?.abortSignal);
  },
  stream: async function* (request, signal) {
    modelAttempt += 1;
    const attempt = modelAttempt;
    push("model.request", { attempt, request });
    const fault = modelFaultAt(attempt);
    if (["retryable_error", "non_retryable_error", "malformed_response", "stream_interruption"].includes(fault?.action)) {
      const retryable = fault.action === "retryable_error";
      const code = retryable
        ? "provider_unavailable"
        : fault.action === "stream_interruption"
          ? "provider_stream_interrupted"
          : "invalid_model_response";
      const message = fault.action === "malformed_response"
        ? "Deterministic malformed provider response."
        : fault.action === "stream_interruption"
          ? "Deterministic provider stream interruption."
          : retryable
            ? "Deterministic temporary provider failure."
            : "Deterministic permanent provider failure.";
      const error = Object.assign(
        new Error(message),
        {
          code,
          retryable,
        },
      );
      push("fault.injected", { target: "model", action: fault.action, attempt });
      push("model.error", { code: error.code, message: error.message, retryable, attempt });
      throw error;
    }
    let result;
    try {
      result = await post("/v1/chat/completions", {
        scenarioId: scenario.scenarioId,
        q: scenario.q,
        messages: request.messages,
        delays: scenario.delays,
        faults: scenario.faults,
      }, signal);
    } catch (error) {
      if (signal?.aborted) return;
      throw error;
    }
    const message = result.choices[0].message;
    push("model.response", { attempt, response: message });
    yield { type: "message_start", role: "assistant" };
    for (const call of message.tool_calls ?? []) yield { type: "tool_call_end", toolCall: { id: call.id, name: call.function.name, input: JSON.parse(call.function.arguments) } };
    if (message.tool_calls?.length) yield { type: "message_end", finishReason: "tool_call" };
    else { yield { type: "text_delta", text: message.content ?? "" }; yield { type: "message_end", finishReason: "stop" }; }
  },
};
const permissionMode = scenario.permission?.mode ?? "default";
const permissionContext = createDefaultPermissionContext({
  cwd: root,
  mode: permissionMode,
  rules: {
    // The deterministic adapter is the scenario authority. Base runtime rules would
    // short-circuit checkPermissions before the resolved scenario answer is applied.
    allow: [],
    deny: [],
    ask: [],
  },
});
const scenarioMessages = (scenario.messages ?? []).map(canonicalMessage);
const lastMessage = scenarioMessages.at(-1);
const hasCurrentUserMessage = lastMessage?.role === "user";
const historyMessages = hasCurrentUserMessage ? scenarioMessages.slice(0, -1) : scenarioMessages;
const initialState = historyMessages.length ? {
  sessionId: "session-parity",
  messages: historyMessages,
  usage: {},
  permissionDenials: [],
  status: "idle",
  abortController: new AbortController(),
} : undefined;
const seedState = parseSeedState(scenario.seedState);
if (seedState) {
  push("checkpoint", { status: "seeded", seedState: serializeSeedState(seedState) });
}
session = createAgentSession({
  sessionId: "session-parity",
  config: { provider: "parity", model: "deterministic", cwd: root, systemPrompt: "Return the deterministic answer.", permissionMode, permissionContext, metadata: { scenarioId: scenario.scenarioId } },
  dependencies: { router, tools: { registry }, now: () => new Date("2026-01-01T00:00:00.000Z"), uuid: () => `parity-id-${++eventId}` },
  initialState,
  seedState,
});
const currentContent = hasCurrentUserMessage ? lastMessage.content : [{ type: "text", text: scenario.q }];
const input = { type: "blocks", content: currentContent };
const cancelTimer = limits.cancelAfterMs ? setTimeout(() => cancelRun("parity_cancel"), limits.cancelAfterMs) : undefined;
const deadlineTimer = limits.deadlineMs ? setTimeout(() => {
  deadlineExceeded = true;
  cancelRun("deadline_exceeded");
}, limits.deadlineMs) : undefined;
try {
  for await (const event of session.submit(input, { turnId: "turn-parity", maxTurns: limits.maxTurns, permissionMode, permissionRules: permissionContext.rules })) {
    if (event.type === "turn_completed") push("terminal", { outcome: outcome(event.result.type, { cancelled, deadlineExceeded }), code: deadlineExceeded ? "DEADLINE_EXCEEDED" : event.result.errors?.[0]?.code, stopReason: event.result.stopReason, structuredResult: event.result.structuredOutput, output: textOf(event.result.finalMessage) });
  }
} catch (error) {
  push("terminal", { outcome: "failed", code: error.code ?? "ADAPTER_ERROR", error: String(error.message ?? error) });
} finally {
  if (cancelTimer) clearTimeout(cancelTimer);
  if (deadlineTimer) clearTimeout(deadlineTimer);
  if (toolCancelTimer) clearTimeout(toolCancelTimer);
}
fs.writeFileSync(output, trace.map((item) => JSON.stringify(item)).join("\n") + "\n");
process.exit(trace.some((item) => item.kind === "terminal") ? 0 : 2);

function pathToUrl(file) { return new URL(`file://${file}`).href; }
function parseSeedState(value) {
  if (value === undefined || value === null) return undefined;
  if (!isPlainRecord(value)) throw new Error("Invalid parity seedState: expected an object.");
  const seed = {};
  if (value.allowedReadFiles !== undefined) {
    if (!Array.isArray(value.allowedReadFiles) || value.allowedReadFiles.some((filePath) => typeof filePath !== "string")) {
      throw new Error("Invalid parity seedState.allowedReadFiles.");
    }
    seed.allowedReadFiles = [...value.allowedReadFiles];
  }
  if (value.readFileState !== undefined) seed.readFileState = parseSeedEntries(value.readFileState, "readFileState", (entry) =>
    typeof entry.mtimeMs === "number" && ["text", "image", "pdf", "notebook"].includes(entry.kind));
  if (value.writeSnapshots !== undefined) seed.writeSnapshots = parseSeedEntries(value.writeSnapshots, "writeSnapshots", (entry) =>
    typeof entry.absolutePath === "string" && typeof entry.mtimeMs === "number" && typeof entry.contentHash === "string");
  return seed;
}
function parseSeedEntries(value, name, isValidEntry) {
  if (!isPlainRecord(value)) throw new Error(`Invalid parity seedState.${name}.`);
  const result = new Map();
  for (const [filePath, entry] of Object.entries(value)) {
    if (!isPlainRecord(entry) || !isValidEntry(entry)) throw new Error(`Invalid parity seedState.${name} entry: ${filePath}.`);
    result.set(filePath, { ...entry });
  }
  return result;
}
function isPlainRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}
function serializeSeedState(seedState) {
  return {
    ...(seedState.allowedReadFiles ? { allowedReadFiles: [...seedState.allowedReadFiles] } : {}),
    ...(seedState.readFileState ? { readFileState: Object.fromEntries(seedState.readFileState) } : {}),
    ...(seedState.writeSnapshots ? { writeSnapshots: Object.fromEntries(seedState.writeSnapshots) } : {}),
  };
}
function imageBlock(block) { const match = /^data:([^;]+);base64,(.+)$/.exec(block.image_url?.url ?? ""); return match ? { type: "image", source: "base64", mimeType: match[1], data: match[2] } : { type: "text", text: "[invalid image]" }; }
function canonicalMessage(message) {
  const content = Array.isArray(message.content)
    ? message.content.map((block) => block.type === "image_url" ? imageBlock(block) : block)
    : [{ type: "text", text: String(message.content ?? "") }];
  return { role: message.role, content };
}
function outcome(type, state) { if (state.deadlineExceeded) return "failed"; if (state.cancelled) return "cancelled"; return type === "success" ? "completed" : type === "aborted" ? "cancelled" : "failed"; }
function textOf(message) { return (message?.content ?? []).filter((item) => item.type === "text").map((item) => item.text).join(""); }
