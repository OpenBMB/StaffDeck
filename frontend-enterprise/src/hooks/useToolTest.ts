import { useEffect, useRef, useState } from 'react';
import { api, TENANT_ID } from '../api/client';
import { getEnterpriseAuthSession } from '../auth';

type Receipt = { success?: boolean; data?: { detached?: boolean; task_id?: string }; error?: unknown };
type Task = { id: string; status: string; poll_attempts?: number; result?: unknown; error?: unknown };
const terminal = new Set(['completed', 'failed', 'cancelled', 'expired', 'outcome_unknown']);
function requestId() {
  // randomUUID is unavailable on non-HTTPS deployments such as port 10086.
  return globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}
function persist(key: string, value: unknown) {
  try {
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch { /* Private mode/quota must not wedge the call button. */ }
}

export function useToolTest(toolId: string, agentQuery: string, pollSeconds = 5) {
  const key = `staffdeck-tool-test:${TENANT_ID}:${getEnterpriseAuthSession()?.user.id || ''}:${toolId}`;
  const [result, setResult] = useState('');
  const [status, setStatus] = useState('等待调用');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const controller = useRef<AbortController>();
  const inFlight = useRef(false);
  const submission = useRef<{ id: string; arguments: Record<string, unknown> }>();
  const labels: Record<string, string> = {
    queued: '本地排队中', submitting: '正在提交', accepted: '外部已受理', working: '执行中',
    completed: '已完成', failed: '失败', cancelled: '已取消', expired: '已超时', outcome_unknown: '提交结果待核实',
  };

  function save(id: string | null) {
    persist(key, id ? { taskId: id } : null);
  }

  async function poll(id: string, version: number) {
    if (version !== generation.current) return;
    const requestController = new AbortController();
    controller.current = requestController;
    const timeout = setTimeout(() => requestController.abort(), 30000);
    try {
      const task = await api.getWithSignal<Task>(
        `/api/enterprise/external-business-tasks/${encodeURIComponent(id)}?tenant_id=${TENANT_ID}&tool_id=${encodeURIComponent(toolId)}`,
        requestController.signal,
      );
      if (version !== generation.current) return;
      setResult(JSON.stringify(task, null, 2));
      setError('');
      setStatus(labels[task.status] || task.status);
      if (terminal.has(task.status)) {
        // Unknown outcome must not enable a one-click re-submission.
        if (task.status !== 'outcome_unknown') {
          setTaskId(null);
          save(null);
        }
        setBusy(false);
      } else {
        timer.current = setTimeout(() => void poll(id, version), Math.max(1, pollSeconds) * 1000);
      }
    } catch (e) {
      if (version !== generation.current) return;
      setError(`状态查询失败，任务不会重新提交：${e instanceof Error ? e.message : String(e)}`);
      setStatus('状态查询中断');
      setBusy(false);
    } finally {
      clearTimeout(timeout);
    }
  }

  useEffect(() => {
    const version = ++generation.current;
    inFlight.current = false;
    submission.current = undefined;
    setResult(''); setStatus('等待调用'); setError(''); setBusy(false); setTaskId(null);
    try {
      const saved = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (typeof saved?.taskId === 'string') {
        setTaskId(saved.taskId);
        setBusy(true);
        void poll(saved.taskId, version);
      } else if (saved?.submission?.id && saved?.submission?.arguments) {
        submission.current = saved.submission;
        setStatus('提交结果待确认');
        setError('上次提交尚未确认。重试将使用相同请求标识和原参数，不会新建重复任务。');
      }
    } catch { persist(key, null); }
    return () => {
      ++generation.current;
      clearTimeout(timer.current);
      controller.current?.abort();
    };
  }, [key]);

  async function submit(args: Record<string, unknown>) {
    if (inFlight.current || taskId) return;
    inFlight.current = true;
    const version = generation.current;
    submission.current ||= { id: requestId(), arguments: args };
    persist(key, { submission: submission.current });
    setBusy(true); setError(''); setStatus('正在提交');
    const requestController = new AbortController();
    controller.current = requestController;
    const timeout = setTimeout(() => requestController.abort(), 30000);
    try {
      const receipt = await api.postWithSignal<Receipt>(
        `/api/enterprise/tools/${toolId}/test${agentQuery ? `?${agentQuery.slice(1)}` : ''}`,
        { tenant_id: TENANT_ID, arguments: submission.current.arguments, client_request_id: submission.current.id },
        requestController.signal,
      );
      if (version !== generation.current) return;
      setResult(JSON.stringify(receipt, null, 2));
      submission.current = undefined;
      const id = receipt.data?.detached ? receipt.data.task_id : undefined;
      if (id) {
        save(id); setTaskId(id); setStatus('本地排队中');
        clearTimeout(timeout);
        void poll(id, version);
      } else {
        save(null); setStatus(receipt.success === false ? '调用失败' : '已返回'); setBusy(false);
      }
    } catch (e) {
      if (version !== generation.current) return;
      setError(`提交结果未确认；重试会沿用原参数和请求标识：${e instanceof Error ? e.message : String(e)}`);
      setStatus('提交结果待确认'); setBusy(false);
    } finally {
      clearTimeout(timeout);
      if (version === generation.current) inFlight.current = false;
    }
  }

  function retryStatus() {
    if (!taskId || busy) return;
    setBusy(true); setError('');
    void poll(taskId, generation.current);
  }
  return { result, status, taskId, busy, error, submit, retryStatus,
    parametersLocked: busy || Boolean(taskId) || Boolean(submission.current) };
}
