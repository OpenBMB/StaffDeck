import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { EnterpriseRoute } from '../enums/routes';

/**
 * Backward-compatible redirect for links created before team conversations
 * moved out of the management console. New entry points navigate directly to
 * the workspace chat route.
 */
export default function TeamChatPage() {
  const { teamId = '' } = useParams<{ teamId: string }>();
  const navigate = useNavigate();
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    if (!teamId) {
      setError('团队不存在');
      return undefined;
    }

    api
      .post<{ session_id: string }>(`/api/enterprise/teams/${teamId}/tl/session`, {
        tenant_id: TENANT_ID,
      })
      .then((result) => {
        if (cancelled) return;
        if (!result.session_id) throw new Error('未返回团队会话');
        navigate(`${EnterpriseRoute.Chat}/${result.session_id}`, { replace: true });
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : '打开项目领导对话失败');
      });

    return () => {
      cancelled = true;
    };
  }, [navigate, teamId]);

  return (
    <div className="grid min-h-[60vh] place-items-center px-[24px] text-center">
      <div className="rounded-[20px] bg-white px-[32px] py-[28px] shadow-[0_0_6px_rgba(0,0,0,0.05)]">
        <p className="text-[15px] font-medium text-[#18181a]">
          {error || '正在前往对话端…'}
        </p>
        <p className="mt-[8px] text-[12px] text-[#757f9c]">
          团队管理保留在管理端，团队协作统一在对话端进行。
        </p>
      </div>
    </div>
  );
}
