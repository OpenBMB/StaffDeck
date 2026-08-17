import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, TENANT_ID } from '../api/client';
import { EnterpriseRoute } from '../enums/routes';

/** Backward-compatible entry that opens the team's single persistent group chat. */
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
        if (!result.session_id) throw new Error('未返回团队群聊');
        navigate(`${EnterpriseRoute.Chat}/${result.session_id}`, { replace: true });
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '打开团队群聊失败');
      });
    return () => {
      cancelled = true;
    };
  }, [navigate, teamId]);

  return (
    <div className="grid min-h-[60vh] place-items-center px-[24px] text-center">
      <p className="text-[14px] text-[#646b7c]">{error || '正在进入团队群聊…'}</p>
    </div>
  );
}
