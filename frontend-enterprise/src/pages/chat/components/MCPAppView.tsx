import { useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/api/client';
import StaffdeckIcon from '@/components/StaffdeckIcon';

import type { MCPAppViewDescriptor } from '../chatTypes';

const APP_PROTOCOL_VERSION = '2026-01-26';

type AppResource = {
  server_id: string;
  uri: string;
  mime_type: string;
  text: string;
  meta: {
    ui?: {
      csp?: Record<string, string[]>;
      permissions?: string[];
    };
  };
};

type AppCallResponse = {
  success: boolean;
  result?: unknown;
  requires_confirmation?: boolean;
  error?: { code?: string; message?: string } | null;
};

type JsonRpcRequest = {
  jsonrpc?: string;
  id?: string | number;
  method?: string;
  params?: Record<string, unknown>;
};

type AppMessage = JsonRpcRequest & {
  method?: string;
};

export default function MCPAppView({ descriptor }: { descriptor: MCPAppViewDescriptor }) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [resource, setResource] = useState<AppResource | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    const query = new URLSearchParams({
      tenant_id: descriptor.tenant_id || '',
      uri: descriptor.resource_uri,
    });
    if (descriptor.agent_id) query.set('agent_id', descriptor.agent_id);
    api
      .get<AppResource>(`/api/enterprise/mcp-servers/${descriptor.server_id}/app-resource?${query}`)
      .then((next) => {
        if (active) setResource(next);
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : 'MCP App 资源加载失败');
      });
    return () => {
      active = false;
    };
  }, [descriptor.agent_id, descriptor.resource_uri, descriptor.server_id, descriptor.tenant_id]);

  const srcDoc = useMemo(
    () => resource ? injectContentSecurityPolicy(resource.text, resource.meta.ui?.csp || {}) : '',
    [resource],
  );

  useEffect(() => {
    if (!resource) return undefined;
    const onMessage = (event: MessageEvent<unknown>) => {
      if (event.source !== iframeRef.current?.contentWindow || !isAppMessage(event.data)) return;
      const request = event.data;
      if (request.method === 'ui/initialize' || request.method === 'ui/initialize/request') {
        postRpcResult(request.id, {
          protocolVersion: APP_PROTOCOL_VERSION,
          hostInfo: { name: 'StaffDeck', version: '1' },
          hostCapabilities: { tools: { call: true }, textFallback: true },
        });
        return;
      }
      if (request.method === 'tools/call' || request.method === 'ui/tools/call') {
        void callTool(request);
      }
    };

    const callTool = async (request: JsonRpcRequest) => {
      const params = request.params || {};
      const toolName = typeof params.name === 'string' ? params.name : descriptor.tool_name;
      const argumentsValue = isRecord(params.arguments) ? params.arguments : {};
      const payload = {
        tenant_id: descriptor.tenant_id || '',
        tool_name: toolName,
        arguments: argumentsValue,
        agent_id: descriptor.agent_id || null,
        session_id: descriptor.session_id || null,
        active_skill_id: descriptor.active_skill_id || null,
        confirm_side_effect: false,
      };
      try {
        let response = await api.post<AppCallResponse>(
          `/api/enterprise/mcp-servers/${descriptor.server_id}/app-call`,
          payload,
        );
        if (response.requires_confirmation) {
          const confirmed = window.confirm(`MCP App 请求执行可能产生副作用的工具“${toolName}”，是否继续？`);
          if (!confirmed) {
            postRpcError(request.id, -32001, '用户取消了工具调用。');
            return;
          }
          response = await api.post<AppCallResponse>(
            `/api/enterprise/mcp-servers/${descriptor.server_id}/app-call`,
            { ...payload, confirm_side_effect: true },
          );
        }
        if (!response.success) {
          postRpcError(request.id, -32000, response.error?.message || 'MCP App 工具调用失败。');
          return;
        }
        postRpcResult(request.id, response.result ?? null);
      } catch (reason) {
        postRpcError(request.id, -32000, reason instanceof Error ? reason.message : 'MCP App 工具调用失败。');
      }
    };

    const postRpcResult = (id: JsonRpcRequest['id'], result: unknown) => {
      iframeRef.current?.contentWindow?.postMessage({ jsonrpc: '2.0', id, result }, '*');
    };
    const postRpcError = (id: JsonRpcRequest['id'], code: number, message: string) => {
      iframeRef.current?.contentWindow?.postMessage({ jsonrpc: '2.0', id, error: { code, message } }, '*');
    };

    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [descriptor, resource]);

  const notifyInitialResult = () => {
    const params = {
      content: descriptor.initial_result,
      _meta: descriptor.initial_meta || {},
    };
    for (const method of ['ui/notifications/tool-result', 'ui/notifications/tool-result-ready']) {
      iframeRef.current?.contentWindow?.postMessage({ jsonrpc: '2.0', method, params }, '*');
    }
  };

  if (error) {
    return (
      <div className="mt-2 flex items-start gap-2 rounded-lg border border-[#eceef1] bg-[#fafbfc] px-3 py-2 text-xs text-[#858b9c]">
        <StaffdeckIcon name="warning" size={14} />
        <span>MCP App 无法展示，已保留上方文本结果。{error}</span>
      </div>
    );
  }
  if (!resource) {
    return <div className="mt-2 text-xs text-[#858b9c]">正在加载 MCP App…</div>;
  }
  return (
    <section className="mt-2 overflow-hidden rounded-xl border border-[#dfe5e2] bg-white" aria-label="MCP App">
      <div className="flex items-center justify-between border-b border-[#eceef1] bg-[#fafbfc] px-3 py-2 text-xs text-[#5f6675]">
        <span className="font-medium">MCP App · {descriptor.tool_name}</span>
        <span>隔离视图</span>
      </div>
      <iframe
        ref={iframeRef}
        title={`MCP App ${descriptor.tool_name}`}
        className="h-[360px] w-full border-0 bg-white"
        sandbox="allow-scripts"
        allow={(resource.meta.ui?.permissions || []).join('; ')}
        srcDoc={srcDoc}
        onLoad={notifyInitialResult}
      />
    </section>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function isAppMessage(value: unknown): value is AppMessage {
  return isRecord(value) && typeof value.method === 'string';
}

function injectContentSecurityPolicy(html: string, csp: Record<string, string[]>): string {
  const resourceDomains = csp.resourceDomains || [];
  const connectDomains = csp.connectDomains || [];
  const frameDomains = csp.frameDomains || [];
  const policy = [
    "default-src 'none'",
    `script-src 'unsafe-inline' ${resourceDomains.join(' ')}`.trim(),
    `style-src 'unsafe-inline' ${resourceDomains.join(' ')}`.trim(),
    `img-src data: blob: ${resourceDomains.join(' ')}`.trim(),
    `font-src ${resourceDomains.join(' ')}`.trim(),
    `connect-src ${connectDomains.join(' ')}`.trim(),
    `frame-src ${frameDomains.join(' ')}`.trim(),
  ].join('; ');
  const meta = `<meta http-equiv="Content-Security-Policy" content="${escapeHtmlAttribute(policy)}">`;
  if (/<head[\s>]/i.test(html)) return html.replace(/<head([^>]*)>/i, `<head$1>${meta}`);
  return `<!doctype html><html><head>${meta}</head><body>${html}</body></html>`;
}

function escapeHtmlAttribute(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
}
