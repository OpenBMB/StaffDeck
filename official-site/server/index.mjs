import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { readFile, stat } from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import corpus from './data/product-corpus.json' with { type: 'json' };
import { SiteLlmError, routeQuestion, streamAnswer } from './lib/llm.mjs';
import { createRetriever } from './lib/retrieval.mjs';
import {
  createRateLimiter,
  createSessionToken,
  isAllowedOrigin,
  requestIp,
  sessionCookie,
  sessionFromRequest,
} from './lib/security.mjs';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const envPath = path.join(projectRoot, '.env.local');
if (existsSync(envPath) && typeof process.loadEnvFile === 'function') process.loadEnvFile(envPath);

const distRoot = path.join(projectRoot, 'dist');
const port = Number(process.env.PORT || 5_175);
const allowedOrigins = String(process.env.STAFFDECK_SITE_ALLOWED_ORIGINS || '')
  .split(',')
  .map((value) => value.trim())
  .filter(Boolean);
const sessionSecret = process.env.STAFFDECK_SITE_SESSION_SECRET || randomBytes(32).toString('hex');
const llmConfig = {
  baseUrl: process.env.STAFFDECK_SITE_LLM_BASE_URL || 'https://llm-center.modelbest.cn/llm/v1',
  model: process.env.STAFFDECK_SITE_LLM_MODEL || 'glm-5.2',
  apiKey: process.env.STAFFDECK_SITE_LLM_API_KEY || '',
};
const retrieve = createRetriever(corpus);
const consumeIp = createRateLimiter({ limit: 24, windowMs: 10 * 60_000 });
const consumeSession = createRateLimiter({ limit: 16, windowMs: 10 * 60_000 });
const activeSessions = new Set();

const MIME = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
};

function sendJson(response, status, payload, headers = {}) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers });
  response.end(JSON.stringify(payload));
}

async function readJson(request, maxBytes = 32_768) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw Object.assign(new Error('Request body is too large.'), { status: 413 });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw Object.assign(new Error('Request body must be valid JSON.'), { status: 400 });
  }
}

function validatePayload(body) {
  const message = typeof body?.message === 'string' ? body.message.trim() : '';
  if (!message || message.length > 2_000) {
    throw Object.assign(new Error('Message must contain between 1 and 2000 characters.'), { status: 400 });
  }
  const locale = body?.locale === 'en-US' ? 'en-US' : 'zh-CN';
  const history = Array.isArray(body?.history)
    ? body.history.slice(-8).flatMap((item) => {
        if (!item || !['user', 'assistant'].includes(item.role) || typeof item.content !== 'string') return [];
        return [{ role: item.role, content: item.content.slice(0, 2_000) }];
      })
    : [];
  return { message, locale, history };
}

function emit(response, event, data = {}) {
  response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
}

async function handleChat(request, response) {
  if (!llmConfig.apiKey) return sendJson(response, 503, { error: 'Website assistant is not configured.' });
  if (!isAllowedOrigin(request, allowedOrigins)) return sendJson(response, 403, { error: 'Origin is not allowed.' });
  const session = sessionFromRequest(request, sessionSecret);
  if (!session || request.headers['x-site-csrf'] !== session.csrf) {
    return sendJson(response, 401, { error: 'Website chat session is invalid or expired.' });
  }

  const ipLimit = consumeIp(requestIp(request));
  const sessionLimit = consumeSession(session.sid);
  if (!ipLimit.allowed || !sessionLimit.allowed) {
    const retryAfter = Math.max(ipLimit.retryAfterSeconds, sessionLimit.retryAfterSeconds);
    return sendJson(response, 429, { error: 'Too many requests. Please try again later.' }, { 'Retry-After': retryAfter });
  }
  if (activeSessions.has(session.sid)) {
    return sendJson(response, 409, { error: 'A response is already being generated for this session.' });
  }

  let input;
  try {
    input = validatePayload(await readJson(request));
  } catch (error) {
    return sendJson(response, error.status || 400, { error: error.message });
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(new Error('Website assistant request timed out.')), 90_000);
  const onClose = () => {
    if (!response.writableEnded) controller.abort(new Error('Client disconnected.'));
  };
  response.on('close', onClose);
  activeSessions.add(session.sid);
  response.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  response.flushHeaders?.();

  try {
    emit(response, 'route.started');
    const route = await routeQuestion(llmConfig, { ...input, signal: controller.signal });
    emit(response, 'route.completed', route);

    let sources = [];
    if (route.mode === 'product_qa') {
      emit(response, 'sop.selected', { sop: 'staffdeck_product_qa' });
      emit(response, 'retrieval.started');
      sources = retrieve(`${route.retrievalQuery}\n${input.message}`, 4).map(({ score, ...source }) => source);
      emit(response, 'retrieval.completed', {
        sources: sources.map((source, index) => ({ index: index + 1, id: source.id, title: source.title })),
      });
    }

    emit(response, 'answer.started', { mode: route.mode });
    let answerLength = 0;
    for await (const delta of streamAnswer(llmConfig, { route, ...input, sources, signal: controller.signal })) {
      answerLength += delta.length;
      if (answerLength > 12_000) throw new SiteLlmError('The generated answer exceeded the website limit.');
      emit(response, 'answer.delta', { delta });
    }
    emit(response, 'answer.completed');
    emit(response, 'done');
  } catch (error) {
    const cancelled = controller.signal.aborted;
    emit(response, 'error', {
      code: cancelled ? 'REQUEST_CANCELLED' : error.code || 'SITE_CHAT_ERROR',
      message: cancelled ? 'The request was cancelled or timed out.' : error.message || 'Website assistant request failed.',
    });
  } finally {
    clearTimeout(timeout);
    response.off('close', onClose);
    activeSessions.delete(session.sid);
    response.end();
  }
}

async function serveStatic(request, response) {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const decoded = decodeURIComponent(pathname);
  const requested = decoded === '/' ? 'index.html' : decoded.replace(/^\/+/, '');
  let filePath = path.resolve(distRoot, requested);
  if (!filePath.startsWith(`${distRoot}${path.sep}`) && filePath !== path.join(distRoot, 'index.html')) {
    return sendJson(response, 400, { error: 'Invalid path.' });
  }
  try {
    if (!(await stat(filePath)).isFile()) throw new Error('not a file');
  } catch {
    filePath = path.join(distRoot, 'index.html');
  }
  try {
    const content = await readFile(filePath);
    const extension = path.extname(filePath).toLowerCase();
    response.writeHead(200, {
      'Content-Type': MIME[extension] || 'application/octet-stream',
      'Cache-Control': filePath.endsWith('index.html') ? 'no-cache' : 'public, max-age=31536000, immutable',
    });
    response.end(content);
  } catch {
    sendJson(response, 503, { error: 'Official site has not been built yet.' });
  }
}

const server = http.createServer(async (request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  if (request.method === 'GET' && pathname === '/api/site-chat/health') {
    return sendJson(response, 200, { status: 'ok', assistantConfigured: Boolean(llmConfig.apiKey) });
  }
  if (request.method === 'GET' && pathname === '/api/site-chat/session') {
    const { session, token } = createSessionToken(sessionSecret);
    const secure = request.headers['x-forwarded-proto'] === 'https';
    return sendJson(response, 200, { csrfToken: session.csrf, expiresAt: session.exp }, {
      'Set-Cookie': sessionCookie(token, { secure }),
    });
  }
  if (request.method === 'POST' && pathname === '/api/site-chat/stream') return handleChat(request, response);
  if (pathname.startsWith('/api/')) return sendJson(response, 404, { error: 'API route not found.' });
  if (!['GET', 'HEAD'].includes(request.method)) return sendJson(response, 405, { error: 'Method not allowed.' });
  return serveStatic(request, response);
});

server.listen(port, '127.0.0.1', () => {
  console.log(`StaffDeck official site listening on http://127.0.0.1:${port}`);
});
