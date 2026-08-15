import { useEffect, useMemo, useState } from 'react';
import { LockKeyhole, RefreshCw, UsersRound } from 'lucide-react';

import { api, TENANT_ID } from '@/api/client';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import { Button } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import type { AgentProfileRead, TeamRead } from '@/types';

import { useChatSession } from '../useChatSession';
import ChatDialogs from './ChatDialogs';
import Composer from './Composer';
import MessageList from './MessageList';

function TeamRoom({
  sessionId,
  team,
  agents,
}: {
  sessionId: string;
  team: TeamRead;
  agents: AgentProfileRead[];
}) {
  const chat = useChatSession({ sessionId, embedded: true });
  const agentById = useMemo(
    () => new Map(agents.map((agent) => [agent.id, agent])),
    [agents],
  );

  return (
    <>
      <div className="flex min-h-0 flex-1 flex-col bg-[#fcfcfc]">
        <div className="flex min-h-[64px] shrink-0 items-center justify-between gap-[16px] border-b border-[#eef1f6] px-[20px] py-[12px]">
          <div className="flex min-w-0 items-center gap-[12px]">
            <div className="grid size-[36px] shrink-0 place-items-center rounded-[11px] bg-[#18181a] text-white">
              <UsersRound className="size-[17px]" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-[8px]">
                <h3 className="truncate text-[14px] font-medium text-[#18181a]">{team.name} · 团队群聊</h3>
                <span className="inline-flex shrink-0 items-center gap-[4px] rounded-full bg-[#eefaf3] px-[8px] py-[3px] text-[10px] font-medium text-[#1e7a4c]">
                  <LockKeyhole className="size-[10px]" />
                  仅本团队
                </span>
              </div>
              <p className="mt-[2px] text-[11px] text-[#858b9c]">
                对话由项目领导统一承接；任务执行、竞标与验收记录保留在下方工作区
              </p>
            </div>
          </div>
          <div className="flex shrink-0 -space-x-[8px]" aria-label={`${team.members.length} 位团队成员`}>
            {team.members.slice(0, 5).map((member) => (
              <div key={member.id} className="rounded-[10px] border-2 border-white bg-white" title={member.agent_name || member.agent_id}>
                <EmployeeAvatar agent={agentById.get(member.agent_id)} size={28} radius={8} />
              </div>
            ))}
            {team.members.length > 5 && (
              <span className="grid size-[32px] place-items-center rounded-[10px] border-2 border-white bg-[#eef0f4] text-[10px] font-medium text-[#646b7c]">
                +{team.members.length - 5}
              </span>
            )}
          </div>
        </div>

        <MessageList
          chat={chat}
          emptyState={(
            <div className="mx-auto grid min-h-[220px] w-full max-w-[560px] place-items-center px-[24px] text-center">
              <div>
                <div className="mx-auto grid size-[44px] place-items-center rounded-[14px] bg-[#f1f2f5] text-[#646b7c]">
                  <UsersRound className="size-[19px]" />
                </div>
                <p className="mt-[14px] text-[14px] font-medium text-[#18181a]">从团队目标开始讨论</p>
                <p className="mx-auto mt-[6px] max-w-[400px] text-[12px] leading-[19px] text-[#858b9c]">
                  这里是团队的唯一群聊。项目领导会在后台注入团队上下文，并把需要执行的工作拆进任务看板。
                </p>
              </div>
            </div>
          )}
        />
        <Composer chat={chat} />
      </div>
      <ChatDialogs chat={chat} />
    </>
  );
}

export default function TeamGroupChatPanel({
  team,
  agents,
}: {
  team: TeamRead;
  agents: AgentProfileRead[];
}) {
  const [sessionId, setSessionId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const leaderId = team.members.find((member) => member.role === 'leader')?.agent_id || '';

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    api
      .post<{ session_id: string }>(`/api/enterprise/teams/${team.id}/tl/session`, {
        tenant_id: TENANT_ID,
      })
      .then((result) => {
        if (cancelled) return;
        if (!result.session_id) throw new Error('未返回团队群聊');
        setSessionId(result.session_id);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        const message = reason instanceof Error ? reason.message : '加载团队群聊失败';
        setError(message);
        notify.error(message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [leaderId, team.id]);

  return (
    <section
      id="team-chat"
      aria-label="团队群聊"
      className="mt-[20px] overflow-hidden rounded-[20px] bg-white shadow-[0_0_6px_rgba(0,0,0,0.05)]"
    >
      <div className="flex items-center justify-between gap-[12px] border-b border-[#eef1f6] px-[20px] py-[16px]">
        <div>
          <h2 className="text-[16px] font-medium text-[#18181a]">团队群聊</h2>
          <p className="mt-[3px] text-[12px] text-[#858b9c]">每个团队固定一个群聊，不占用员工单独会话列表</p>
        </div>
      </div>
      <div className="flex h-[min(640px,72vh)] min-h-[480px] flex-col">
        {loading && (
          <div className="grid flex-1 place-items-center text-[13px] text-[#858b9c]">正在进入团队群聊…</div>
        )}
        {!loading && error && (
          <div className="grid flex-1 place-items-center px-[24px] text-center">
            <div>
              <p className="text-[13px] text-[#c0342b]">{error}</p>
              <Button
                type="button"
                variant="outline"
                onClick={() => window.location.reload()}
                className="mt-[12px] h-[34px] rounded-[10px]"
              >
                <RefreshCw className="mr-[6px] size-[14px]" />
                重新加载
              </Button>
            </div>
          </div>
        )}
        {!loading && !error && sessionId && <TeamRoom sessionId={sessionId} team={team} agents={agents} />}
      </div>
    </section>
  );
}
