import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const sourceRoot = process.env.PARITY_SOURCE_ROOT;
const sidecarRoot = process.env.PARITY_PILOTDECK_ROOT ?? sourceRoot;
const scenario = JSON.parse(process.env.PARITY_SCENARIO_JSON);
const mockBaseUrl = process.env.PARITY_MOCK_BASE_URL;
const traceOut = process.env.PARITY_TRACE_OUT;
const mode = process.env.PARITY_MODE;
const runKey = process.env.PARITY_RUN_KEY ?? `${mode}-parity`;
const debug = (...values) => {
  if (process.env.PARITY_DEBUG === "1") console.error("[parity-gateway]", ...values);
};

if (!sourceRoot || !sidecarRoot || !mockBaseUrl || !traceOut) {
  throw new Error("PilotDeck gateway parity environment is incomplete.");
}

const importFrom = (root, relative) => import(pathToFileURL(path.join(root, relative)).href);
const { createLocalGateway } = await importFrom(sourceRoot, "dist/src/cli/createLocalGateway.js");
const { startPilotDeckServer } = await importFrom(sourceRoot, "dist/src/cli/pilotdeckServer.js");
const { GatewayWsClient } = await importFrom(sourceRoot, "dist/src/gateway/client/GatewayWsClient.js");
const { DEFAULT_MODEL_CAPABILITIES } = await importFrom(
  sourceRoot,
  "dist/src/model/protocol/capabilities.js",
);

let sequence = 0;
const trace = [];
let modelAttempt = 0;
let markModelStarted;
const modelStarted = new Promise((resolve) => {
  markModelStarted = resolve;
});
let markToolStarted;
const toolStarted = new Promise((resolve) => {
  markToolStarted = resolve;
});
let scenarioTurnIndex = 0;
let scenarioTurnModelAttempt = 0;
const push = (kind, extra = {}) => {
  trace.push({ kind, scenarioId: scenario.scenarioId, q: scenario.q, sequence: sequence++, ...extra });
};
const modelView = (request) => ({
  systemPrompt: request.systemPrompt,
  messages: request.messages,
  tools: request.tools,
  metadata: request.metadata,
});
const faultAt = (target, attempt, stage) => (scenario.faults?.[target] ?? []).find(
  (fault) => (fault.at ?? 1) === attempt && (!stage || !fault.stage || fault.stage === stage),
);
const post = async (pathname, body, signal) => {
  const response = await fetch(`${mockBaseUrl}${pathname}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  return response.json();
};

class MockModelRuntime {
  async *stream(request, options = {}) {
    modelAttempt += 1;
    scenarioTurnModelAttempt += 1;
    push("model.request", { attempt: modelAttempt, modelView: modelView(request), request });
    markModelStarted();
    const fault = faultAt("model", modelAttempt);
    if (fault?.action === "retryable_error" || fault?.action === "non_retryable_error") {
      const retryable = fault.action === "retryable_error";
      const error = Object.assign(new Error(retryable ? "Deterministic temporary provider failure." : "Deterministic permanent provider failure."), {
        code: retryable ? "provider_unavailable" : "invalid_model_response",
        retryable,
      });
      push("fault.injected", { target: "model", action: fault.action, attempt: modelAttempt });
      push("model.error", { code: error.code, message: error.message, retryable, attempt: modelAttempt });
      throw error;
    }
    if (fault?.action === "stream_interruption") {
      push("fault.injected", { target: "model", action: fault.action, attempt: modelAttempt });
      yield { type: "request_started", provider: request.provider, model: request.model };
      yield { type: "message_start", role: "assistant" };
      yield { type: "text_delta", text: "partial" };
      throw Object.assign(new Error("Deterministic stream interruption."), { code: "stream_interrupted", retryable: false });
    }
    const response = await post("/v1/chat/completions", {
      scenarioId: scenario.scenarioId,
      q: scenario.q,
      messages: request.messages,
      delays: scenario.delays,
      toolDelays: scenario.toolDelays,
      faults: scenario.faults,
      planModePolicy: scenario.planModePolicy === true,
      turnIndex: scenarioTurnIndex,
      turnModelAttempt: scenarioTurnModelAttempt,
      runKey,
    }, options.signal);
    if (fault?.action === "malformed_response") {
      push("fault.injected", { target: "model", action: fault.action, attempt: modelAttempt });
      throw Object.assign(new Error("Deterministic malformed model response."), { code: "invalid_model_response", retryable: false });
    }
    const message = response.choices[0].message;
    push("model.response", { attempt: modelAttempt, modelView: message, response: message });
    yield { type: "request_started", provider: request.provider, model: request.model };
    yield { type: "message_start", role: "assistant" };
    for (const call of message.tool_calls ?? []) {
      yield {
        type: "tool_call_end",
        toolCall: {
          id: call.id,
          name: call.function.name,
          input: JSON.parse(call.function.arguments),
        },
      };
    }
    if (message.tool_calls?.length) {
      yield { type: "message_end", finishReason: "tool_call" };
    } else {
      yield { type: "text_delta", text: message.content ?? "" };
      yield { type: "message_end", finishReason: "stop" };
    }
  }

  async complete() {
    return { role: "assistant", content: [{ type: "text", text: "Parity session" }], finishReason: "stop" };
  }

  getCapabilities() {
    return { ...DEFAULT_MODEL_CAPABILITIES, supportsToolUse: true };
  }

  getMultimodal() {
    return { input: ["text", "image"] };
  }

  getProviderProtocol() {
    return "openai";
  }

  getProviderBaseUrl() {
    return mockBaseUrl;
  }
}

function createTools() {
  return (scenario.tools ?? []).filter((name) =>
    name !== "ask_user_question"
    && name !== "read_file"
    && name !== "enter_plan_mode"
    && name !== "exit_plan_mode",
  ).map((name) => {
    const concurrencySafe = ["lookup", "summarize"].includes(name);
    return {
    name,
    description: name,
    kind: "custom",
    inputSchema: { type: "object" },
    isReadOnly: () => !["restricted", "loop", "write_file", "parity_write_probe"].includes(name),
    isConcurrencySafe: () => concurrencySafe,
    requiresUserInteraction: () => name === "ask_user_question",
    checkPermissions: async (_input, context) => {
      push("policy.context", { toolName: name, permissionMode: context.permissionMode, runMode: context.runMode });
      push("permission.request", { toolName: name, mode: context.permissionMode, canPrompt: scenario.permission?.canPrompt ?? false });
      const deniedByRule = scenario.permission?.deny?.includes(name) ?? false;
      const deniedByAnswer = scenario.permission?.ask?.includes(name) && scenario.permission?.answer === "deny";
      const denied = deniedByRule || deniedByAnswer;
      if (scenario.permission?.ask?.includes(name)) {
        push("permission.answer", { toolName: name, allowed: !denied, code: denied ? "PERMISSION_DENIED" : undefined });
      }
      push("permission.decision", { toolName: name, allowed: !denied });
      return denied
        ? {
            type: "deny",
            message: "Deterministic permission denial.",
            reason: { type: "tool", toolName: name, message: "denied" },
          }
        : {
            type: "allow",
            reason: { type: "tool", toolName: name, message: "allowed" },
          };
    },
    execute: async (input, context) => {
      const toolCallId = context.currentToolCallId;
      push("tool.call", { name, arguments: input, toolCallId, concurrencySafe });
      push("tool.start", { name, toolCallId, concurrencySafe });
      markToolStarted();
      const forwardCancellation = () => {
        void post("/control/cancel", { runKey }).catch(() => undefined);
      };
      context.abortSignal?.addEventListener("abort", forwardCancellation, { once: true });
      let result;
      try {
        result = await post("/tools/execute", {
          scenarioId: scenario.scenarioId,
          q: scenario.q,
          name,
          arguments: input,
          permissionAllowed: true,
          delays: scenario.delays,
          toolDelays: scenario.toolDelays,
          faults: scenario.faults,
          planModePolicy: scenario.planModePolicy === true,
          runKey,
        }, context.abortSignal);
      } catch (error) {
        if (!context.abortSignal?.aborted) throw error;
        // Native and stdio can observe the same cancelled mock call at
        // different points around fetch abort. Preserve the canonical tool
        // cancellation before handing it back to the normal scheduler.
        result = {
          type: "error",
          toolName: name,
          error: {
            code: "CANCELLED",
            message: "Deterministic tool cancellation.",
            retryable: false,
          },
        };
      } finally {
        context.abortSignal?.removeEventListener("abort", forwardCancellation);
      }
      push("tool.finish", { name, toolCallId, concurrencySafe, success: result.type === "success", error: result.error, sideEffectCount: result.data?.sideEffectCount });
      push("tool.result", { result, toolCallId, concurrencySafe, sideEffectCount: result.data?.sideEffectCount });
      if (result.type === "error") {
        throw Object.assign(new Error(result.error.message), { code: result.error.code });
      }
      return { content: [{ type: "text", text: JSON.stringify(result.data) }], data: result.data };
    },
  };
  });
}

function createParityReadFileTool() {
  return {
    name: "read_file",
    description: "Reads a deterministic parity file.",
    kind: "filesystem",
    inputSchema: { type: "object", required: ["file_path"], properties: { file_path: { type: "string" } } },
    isReadOnly: () => true,
    isConcurrencySafe: () => false,
    checkPermissions: async (input, context) => {
      const allowed = (context.allowedReadFiles ?? []).some((file) => String(file).endsWith(String(input.file_path ?? "")));
      push("permission.decision", { toolName: "read_file", allowed });
      return allowed ? { type: "allow", reason: { type: "tool", toolName: "read_file" } } : { type: "deny", message: "File is not in allowedReadFiles." };
    },
    execute: async (input) => ({ content: [{ type: "text", text: "deterministic file content" }], data: { path: input.file_path, content: "deterministic file content" } }),
  };
}


const configuredRuntimeRoot = process.env.PARITY_RUNTIME_ROOT;
const runtimeRoot = configuredRuntimeRoot
  ? path.resolve(configuredRuntimeRoot)
  : await mkdtemp(path.join(tmpdir(), `pilotdeck-full-${mode}-`));
await mkdir(runtimeRoot, { recursive: true });
const pilotHome = path.join(runtimeRoot, "home");
await mkdir(pilotHome, { recursive: true });
process.env.PILOT_HOME = pilotHome;
const projectRoot = pilotHome;
const configuredContextTokens = scenario.limits?.maxContextTokens ?? 65536;
const configuredOutputTokens = scenario.limits?.maxOutputTokens ?? 8192;
await writeFile(path.join(pilotHome, "pilotdeck.yaml"), `schemaVersion: 1\nagent:\n  model: parity/deterministic\n  maxContextTokens: ${configuredContextTokens}\n  maxOutputTokens: ${configuredOutputTokens}\nmodel:\n  providers:\n    parity:\n      protocol: openai\n      url: ${mockBaseUrl}\n      apiKey: parity-test\n      models:\n        deterministic:\n          capabilities:\n            supportsToolUse: true\n            maxContextTokens: ${configuredContextTokens}\n            maxOutputTokens: ${configuredOutputTokens}\ntelemetry:\n  enabled: false\n`, "utf8");
await writeFile(path.join(projectRoot, "parity-input.txt"), "deterministic file content\n", "utf8");
await mkdir(path.join(projectRoot, ".pilotdeck", "plans"), { recursive: true });
await writeFile(path.join(projectRoot, ".pilotdeck", "plans", "parity-plan.md"), "# Parity plan\n\nExecute the deterministic plan.\n", "utf8");

const productionSidecarEvidence = {
  selectedTransport: mode === "sidecar" ? "stdio" : "native",
  handshakeCompleted: false,
  moduleCalls: [],
};
const gatewayEnv = {
  ...process.env,
  PILOTDECK_AGENT_LOOP_TRANSPORT: mode === "sidecar" ? "stdio" : "native",
  ...(mode === "sidecar" ? {
    PILOTDECK_AGENT_LOOP_SIDECAR_PATH: path.join(sidecarRoot, "dist/src/cli/pilotdeck-agent-loop-sidecar.js"),
  } : {}),
};
const local = createLocalGateway({
  projectRoot,
  pilotHome,
  env: gatewayEnv,
  permissionMode: scenario.permission?.mode ?? "default",
  extraTools: [
    ...createTools(),
  ],
  __testModelFactory: () => new MockModelRuntime(),
  autoElicitation: scenario.permission?.answer === "allow",
  agentLoopTransportObserver: {
    observe(event) {
      if (mode !== "sidecar") return;
      if (event.type === "handshake_completed") productionSidecarEvidence.handshakeCompleted = true;
      if (event.type === "module_call_received") {
        productionSidecarEvidence.moduleCalls.push({ module: event.module, operation: event.operation });
      }
    },
  },
});
const server = await startPilotDeckServer({
  gateway: local.gateway,
  host: "127.0.0.1",
  port: 0,
  staticAssetsPath: path.join(sourceRoot, "ui", "dist"),
});
local.bindServer(server);

if (process.env.PARITY_SERVE_ONLY === "1") {
  console.log(JSON.stringify({ url: server.url, wsUrl: server.wsUrl, token: server.token }));
  await new Promise((resolve) => {
    process.once("SIGINT", resolve);
    process.once("SIGTERM", resolve);
  });
  await server.close();
  await local.dispose();
  await rm(runtimeRoot, { recursive: true, force: true });
  process.exit(0);
}

const client = new GatewayWsClient({ url: server.wsUrl, token: server.token, clientName: "test" });
const controlClient = new GatewayWsClient({ url: server.wsUrl, token: server.token, clientName: "test-control" });
try {
  await client.connect();
  await controlClient.connect();
  const sessionKey = `full-${scenario.scenarioId}`;
  if (scenario.scenarioId === "checkpoint_resume") {
    for await (const _event of client.stream("submit_turn", {
      sessionKey,
      channelKey: "test",
      message: "Previous deterministic result",
      mode: "default",
      canPrompt: false,
    })) {
      // Populate the real transcript before the resumed turn.
    }
  }
  const attachments = [];
  if ((scenario.messages ?? []).some((message) => Array.isArray(message.content) && message.content.some((item) => item.type === "image_url"))) {
    const block = scenario.messages?.at(-1)?.content?.find((item) => item.type === "image_url");
    const match = /^data:([^;]+);base64,(.+)$/.exec(block?.image_url?.url ?? "");
    if (!match) throw new Error("Image scenario is missing a data URL.");
    const imagePath = path.join(projectRoot, "parity-image.png");
    await writeFile(imagePath, Buffer.from(match[2], "base64"));
    attachments.push({ type: "image", name: "parity-image.png", path: imagePath, mimeType: match[1] });
  }
  const limits = scenario.limits ?? {};
  const scenarioTurns = Array.isArray(scenario.turns) && scenario.turns.length > 0
    ? scenario.turns
    : [{ message: scenario.q }];
  let visibleOutput = "";
  let terminal;
  let observedPermissionMode = scenario.permission?.mode ?? "default";
  for (const [turnIndex, turn] of scenarioTurns.entries()) {
    scenarioTurnIndex = turnIndex;
    scenarioTurnModelAttempt = 0;
    push("policy.turn", { permissionMode: observedPermissionMode, runMode: "agent" });
    const streamInput = {
      sessionKey,
      channelKey: "test",
      message: scenario.scenarioId === "auto_compact"
        ? `${typeof turn?.message === "string" ? turn.message : scenario.q}\n${"LONG_CONTEXT ".repeat(1000)}`
        : typeof turn?.message === "string" ? turn.message : scenario.q,
      attachments: turnIndex === 0 ? attachments : [],
      canPrompt: scenario.permission?.canPrompt ?? false,
      maxTurns: limits.maxTurns,
      timeoutMs: limits.deadlineMs,
      ...(turn?.allowPlanModeTools ? { allowPlanModeTools: true } : {}),
      ...(turn?.omitClientMode ? {} : { mode: scenario.permission?.mode ?? "default" }),
    };
    const stream = client.stream("submit_turn", streamInput);
    const cancelAfterMs = limits.cancelAfterToolStartMs ?? limits.cancelAfterMs;
    const cancelAnchor = limits.cancelAfterToolStartMs ? toolStarted : modelStarted;
    const cancelTask = cancelAfterMs
      ? cancelAnchor.then(() => new Promise((resolve) => setTimeout(resolve, cancelAfterMs))).then(async () => {
          push("cancel.requested", { sessionKey });
          await post("/control/cancel", { runKey });
          return controlClient.request("abort_turn", { sessionKey, reason: "parity_cancel" }).then(
            () => push("cancel.acknowledged", { sessionKey }),
            (error) => push("cancel.error", { message: error?.message ?? String(error) }),
          );
        })
      : undefined;
    for await (const event of stream) {
    // Built-in tools (notably read_file) emit their lifecycle only through
    // the Gateway stream. Project those events into the same canonical trace
    // shape used by parity-owned tools; custom tools already trace internally.
    if (event.type === "tool_call_started" && event.name === "read_file") {
      push("tool.call", {
        name: event.name,
        toolCallId: event.toolCallId,
        arguments: event.argsPreview ? { preview: event.argsPreview } : undefined,
      });
      push("tool.start", { name: event.name, toolCallId: event.toolCallId });
    }
    if (event.type === "tool_call_finished" && (event.toolName === "read_file" || event.toolCallId)) {
      if (event.toolName === "read_file") {
        const denied = event.errorCode === "permission_denied" || event.errorCode === "permission_required";
        push("permission.decision", { toolName: event.toolName, allowed: event.ok && !denied });
        push("tool.finish", {
          name: event.toolName,
          toolCallId: event.toolCallId,
          success: event.ok,
          error: event.errorCode ? { code: event.errorCode, message: event.resultPreview } : undefined,
        });
        push("tool.result", {
          toolCallId: event.toolCallId,
          result: event.ok
            ? { type: "success", data: { preview: event.resultPreview } }
            : { type: "error", error: { code: event.errorCode, message: event.resultPreview } },
        });
      }
      if (scenario.planModePolicy === true && event.errorCode === "plan_mode_violation") {
        push("tool.finish", {
          name: event.toolName,
          toolCallId: event.toolCallId,
          success: false,
          error: { code: event.errorCode, message: event.resultPreview },
        });
        push("tool.result", {
          toolCallId: event.toolCallId,
          result: { type: "error", error: { code: event.errorCode, message: event.resultPreview } },
        });
      }
    }
    if (event.type === "permission_request") {
      push("permission.request", { requestId: event.requestId, toolName: event.toolName, payload: event.payload });
    }
    if (event.type === "elicitation_request" && event.toolName === "exit_plan_mode") {
      const question = event.questions[0]?.question;
      if (typeof question !== "string") {
        throw new Error("exit_plan_mode elicitation did not include a question.");
      }
      const response = await client.request("elicitation_respond", {
        sessionKey,
        requestId: event.requestId,
        answer: { type: "answered", answers: { [question]: "execute_plan" } },
      });
      if (!response || response.delivered !== true) {
        throw new Error("Deterministic exit_plan_mode approval was not delivered.");
      }
    }
    if (event.type === "assistant_text_delta") {
      visibleOutput += event.text;
      push("user.output", { text: event.text });
    }
    if (event.type === "plan_mode_changed") observedPermissionMode = event.mode;
    if (event.type === "turn_completed") terminal = event;
    if (event.type === "error") push("gateway.error", { code: event.code, message: event.message });
    }
    if (cancelTask) await cancelTask;
  }
  const mockState = await post("/control/state", { runKey });
  if (mode === "sidecar") {
    if (!productionSidecarEvidence.handshakeCompleted) {
      throw new Error("BLOCKED: production sidecar handshake was not observed.");
    }
    if (productionSidecarEvidence.moduleCalls.length === 0) {
      throw new Error("BLOCKED: production sidecar performed no host module calls.");
    }
    await writeFile(`${traceOut}.proof.json`, `${JSON.stringify(productionSidecarEvidence)}\n`, "utf8");
  }
  const sideEffectCounts = mockState.sideEffects ?? {};
  push("side_effect.state", {
    counts: sideEffectCounts,
    sideEffectCount: Object.values(sideEffectCounts).reduce((total, value) => total + Number(value || 0), 0),
  });
  debug("gateway stream closed", terminal?.finishReason ?? "without terminal");
  const finishReason = terminal?.finishReason ?? "unknown";
  push("terminal", {
    outcome: finishReason === "completed" ? "completed" : finishReason.includes("abort") ? "cancelled" : "failed",
    code: finishReason === "max_turns" ? "agent_max_turns_reached" : undefined,
    stopReason: finishReason,
    output: visibleOutput,
  });
  await writeFile(traceOut, `${trace.map((item) => JSON.stringify(item)).join("\n")}\n`, "utf8");
} finally {
  debug("closing deployment");
  client.close();
  controlClient.close();
  await server.close();
  await local.dispose();
  await rm(runtimeRoot, { recursive: true, force: true });
}
