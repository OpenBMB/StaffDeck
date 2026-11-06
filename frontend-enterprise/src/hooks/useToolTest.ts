import { useEffect, useRef, useState } from 'react';
import { api, TENANT_ID } from '../api/client';

const terminal = new Set(['completed', 'failed', 'cancelled', 'expired', 'outcome_unknown', 'tracking_blocked']);
type Receipt = { success?: boolean; data?: { accepted?: boolean; task_id?: string } };
type Task = { id: string; status: string; result?: unknown; error?: unknown };

/** Poll the same internal task ID. A failed GET must never become another POST. */
export function useToolTest(toolId: string) {
  const [result, setResult] = useState<unknown>(null);
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [queryError, setQueryError] = useState('');
  const [taskId, setTaskId] = useState<string | null>(null);
  const lock = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    generation.current += 1;
    lock.current = false;
    setResult(null);
    setStatus('');
    setBusy(false);
    setTaskId(null);
    setQueryError('');
    return () => { generation.current += 1; };
  }, [toolId]);

  useEffect(() => {
    if (!taskId) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const current = generation.current;
    const active = () => !disposed && current === generation.current;
    async function poll() {
      try {
        const query = new URLSearchParams({ tenant_id: TENANT_ID, tool_id: toolId });
        const task = await api.get<Task>(`/api/enterprise/external-business-tasks/${encodeURIComponent(taskId!)}?${query}`);
        if (!active()) return;
        if (task.id !== taskId) throw new Error('任务状态与查询回执不一致');
        setResult(task);
        setStatus(task.status);
        setQueryError('');
        if (terminal.has(task.status)) {
          lock.current = task.status === 'tracking_blocked';
          setBusy(task.status === 'tracking_blocked');
          setTaskId(null);
          return;
        }
      } catch (error) {
        if (!active()) return;
        setQueryError(error instanceof Error ? error.message : '任务状态查询失败');
      }
      if (active()) timer = setTimeout(() => void poll(), 2000);
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [taskId, toolId]);

  async function submit(path: string, argumentsJson: Record<string, unknown>) {
    if (lock.current) return;
    lock.current = true;
    const current = generation.current;
    setBusy(true);
    setStatus('submitting');
    setQueryError('');
    try {
      const response = await api.post<Receipt>(path, { tenant_id: TENANT_ID, arguments: argumentsJson });
      if (current !== generation.current) return;
      setResult(response);
      if (response.success && response.data?.accepted && response.data.task_id) {
        setTaskId(response.data.task_id);
        setStatus('queued');
      } else {
        lock.current = false;
        setBusy(false);
        setStatus(response.success === false ? 'failed' : 'completed');
      }
    } catch (error) {
      if (current === generation.current) {
        lock.current = false;
        setBusy(false);
        setStatus('failed');
      }
      throw error;
    }
  }

  return { result, status, busy, queryError, submit };
}
