/**
 * `@earendil-works/pi-ai` stub, including its `/providers/all` and `/api/*.lazy`
 * subpaths. The package is Node-only (no `require`/`browser` conditions, Node
 * builtins plus five cloud SDKs in its transport layer) and `llm-pi-ai` imports it
 * statically at module scope, so the row cannot mount without it.
 *
 * Every symbol `llm-pi-ai` imports by name is present: a missing CommonJS symbol
 * would surface as `undefined` at call time instead of a link error. The three catalog readers
 * return empty collections rather than throwing — the row reads them while it
 * activates, and "this deployment ships no pi-ai provider" is the truth here.
 * Everything on a request path is loud.
 */
import { notImplementedFail } from '../notImplementedFail.ts'

const MODULE = '@earendil-works/pi-ai'

/** Provider factory (unavailable). */
export const createProvider = notImplementedFail(MODULE, 'createProvider')

/** Model-list factory (unavailable). */
export const createModels = notImplementedFail(MODULE, 'createModels')

/** Thinking-level catalog (unavailable). */
export const getSupportedThinkingLevels = notImplementedFail(MODULE, 'getSupportedThinkingLevels')

/** Context-overflow predicate (unavailable). */
export const isContextOverflow = notImplementedFail(MODULE, 'isContextOverflow')

/** Builtin provider ids of pi-ai 0.84.2, in catalog order. */
const BUILTIN_PROVIDER_IDS: readonly string[] = [
  'amazon-bedrock', 'ant-ling', 'anthropic', 'azure-openai-responses', 'baseten', 'cerebras',
  'cloudflare-ai-gateway', 'cloudflare-workers-ai', 'deepseek', 'fireworks', 'github-copilot',
  'google', 'google-vertex', 'groq', 'huggingface', 'kimi-coding', 'minimax', 'minimax-cn',
  'mistral', 'moonshotai', 'moonshotai-cn', 'nvidia', 'openai', 'openai-codex', 'opencode',
  'opencode-go', 'openrouter', 'qwen-token-plan', 'qwen-token-plan-cn',
  'qwen-token-plan-individual', 'together',
  'vercel-ai-gateway', 'xai', 'xiaomi', 'xiaomi-token-plan-ams', 'xiaomi-token-plan-cn',
  'xiaomi-token-plan-sgp', 'zai', 'zai-coding-cn',
]

/**
 * Installed catalog providers, read while `llm-pi-ai` activates. Each carries the
 * api-key auth marker the adapter filters on, and no models: the provider
 * directory therefore matches the served deployment while every request path
 * lands on a loud symbol above.
 * @returns one entry per builtin provider.
 */
export function builtinProviders(): unknown[] {
  return BUILTIN_PROVIDER_IDS.map(id => ({
    id,
    name: id,
    auth: { apiKey: { type: 'api-key' } },
    models: [],
  }))
}

/**
 * Provider route ids of the installed catalog. `llm-pi-ai` registers the whole
 * catalog as configurable the moment it mounts and rejects an empty
 * registration, so these are pi-ai's real ids rather than an empty list.
 * @returns the builtin provider ids.
 */
export function getBuiltinProviders(): string[] {
  return [...BUILTIN_PROVIDER_IDS]
}

/**
 * Models of one installed catalog provider.
 * @returns no models.
 */
export function getBuiltinModels(): unknown[] {
  return []
}

/** Anthropic messages API binding (unavailable). */
export const anthropicMessagesApi = notImplementedFail(MODULE, 'anthropicMessagesApi')

/** OpenAI completions API binding (unavailable). */
export const openAICompletionsApi = notImplementedFail(MODULE, 'openAICompletionsApi')

/** OpenAI responses API binding (unavailable). */
export const openAIResponsesApi = notImplementedFail(MODULE, 'openAIResponsesApi')

/** CommonJS interop marker: the worker loader hands `default` to default imports. */
export const __esModule = true

/** CommonJS default export: the members `require()` hands a caller of this module. */
export default {
  createProvider, createModels, getSupportedThinkingLevels, isContextOverflow, builtinProviders,
  getBuiltinModels, getBuiltinProviders, anthropicMessagesApi, openAICompletionsApi,
  openAIResponsesApi,
}
