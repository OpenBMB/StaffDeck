import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Link2Off, LoaderCircle } from 'lucide-react';

import { api } from '@/api/client';
import { setEnterpriseAuthOverride, type EnterpriseAuthSession } from '@/auth';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import { cn } from '@/lib/utils';
import { formatClientMonthDayTime } from '@/lib/timezone';

import { CHAT_HEADER_ACTIONS_CLASS, CHAT_MAIN_CLASS } from './chat/chatPageStyles';
import Composer from './chat/components/Composer';
import MessageList from './chat/components/MessageList';
import { useChatSession } from './chat/useChatSession';

const VISITOR_KEY_PREFIX = 'ultrarag_share_visitor:';

type SharePublicInfo = {
  token: string;
  agent_id: string;
  agent_name: string;
  agent_description?: string | null;
  model_label: string;
  expires_at?: string | null;
};

type VisitorSessionRead = {
  access_token: string;
  visitor_key: string;
  agent_id: string;
  model_config_id: string;
  expires_at?: string | null;
  user: {
    id: string;
    tenant_id: string;
    username: string;
    display_name?: string | null;
    role: string;
  };
};

function readVisitorKey(token: string): string {
  try {
    return window.localStorage.getItem(`${VISITOR_KEY_PREFIX}${token}`) || '';
  } catch {
    return '';
  }
}

function writeVisitorKey(token: string, visitorKey: string): void {
  try {
    window.localStorage.setItem(`${VISITOR_KEY_PREFIX}${token}`, visitorKey);
  } catch {
    // 存储不可用（隐私模式等）：退化为每次打开都是新访客，不影响对话本身
  }
}

/**
 * 数字员工分享页 —— 免登录。
 *
 * 访客身份只写进内存（`setEnterpriseAuthOverride`），不落 localStorage：
 * 否则会把同一个浏览器里已有的站内登录态冲掉。
 * 浏览器标识 `visitor_key` 存 localStorage，同一个浏览器重复打开会接续上下文。
 */
export default function ShareChatPage() {
  const { token = '' } = useParams<{ token: string }>();
  const [share, setShare] = useState<SharePublicInfo | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!token) {
      setError('分享链接无效');
      return;
    }
    let cancelled = false;
    setError('');
    setShare(null);
    setEnterpriseAuthOverride(null);

    void (async () => {
      try {
        const info = await api.get<SharePublicInfo>(
          `/api/public/agent-shares/${encodeURIComponent(token)}`,
        );
        const opened = await api.post<VisitorSessionRead>(
          `/api/public/agent-shares/${encodeURIComponent(token)}/session`,
          { visitor_key: readVisitorKey(token) || undefined },
        );
        if (cancelled) return;
        writeVisitorKey(token, opened.visitor_key);
        const session: EnterpriseAuthSession = {
          token: opened.access_token,
          user: {
            id: opened.user.id,
            tenant_id: opened.user.tenant_id,
            username: opened.user.username,
            display_name: opened.user.display_name || undefined,
            role: 'member',
          },
        };
        setEnterpriseAuthOverride(session);
        setShare(info);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : '分享链接打开失败');
      }
    })();

    return () => {
      cancelled = true;
      setEnterpriseAuthOverride(null);
    };
  }, [token]);

  if (error) {
    return (
      <ShareFallbackState>
        <span className="grid size-[46px] place-items-center rounded-[16px] bg-[#f2f3f7] text-[#8a92a3]">
          <Link2Off className="size-[20px]" />
        </span>
        <strong className="mt-[16px] text-[15px] font-semibold text-[#18181a]">分享链接不可用</strong>
        <p className="mt-[6px] max-w-[320px] text-center text-[12px] leading-[18px] text-[#757f9c]">{error}</p>
        <p className="mt-[14px] text-[11px] text-[#a0a7b6]">请联系发送链接的同事重新生成</p>
      </ShareFallbackState>
    );
  }

  if (!share) {
    return (
      <ShareFallbackState>
        <LoaderCircle className="size-[22px] animate-spin text-[#8a92a3]" />
        <p className="mt-[12px] text-[12px] text-[#757f9c]">正在进入对话…</p>
      </ShareFallbackState>
    );
  }

  return <ShareChatSurface key={token} share={share} />;
}

function ShareChatSurface({ share }: { share: SharePublicInfo }) {
  const chat = useChatSession({ anonymous: true, shareMode: true });
  const agent = chat.displayedAgent;

  return (
    <div className="flex h-screen min-h-0 flex-col sd1-canvas text-[#18181a]">
      <div className="flex h-[88px] shrink-0 items-center justify-between gap-[12px] border-b border-[#f4f4f4] pt-[32px] pl-[18px] pr-[24px]">
        <div className="flex min-w-0 items-center gap-[10px]">
          {agent && (
            <EmployeeAvatar
              agent={agent}
              width={34}
              height={34}
              fit="contain"
              objectPosition="center bottom"
              className="shrink-0 overflow-visible! rounded-none! border-0! bg-transparent! bg-none! shadow-none! after:hidden!"
            />
          )}
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-[14px] text-[#18181a]">{share.agent_name}</span>
            <span className="truncate text-[10px] text-[#757f9c]">
              {share.expires_at ? `分享链接将于 ${formatExpiry(share.expires_at)} 失效` : '长期有效'}
            </span>
          </div>
        </div>
        <div className={CHAT_HEADER_ACTIONS_CLASS}>
          <span className="rounded-full bg-[#f2f3f7] px-[9px] py-[4px] text-[10px] text-[#757f9c]">免登录体验</span>
        </div>
      </div>
      <main className={cn(CHAT_MAIN_CLASS, 'flex-1')}>
        <MessageList chat={chat} />
        <Composer chat={chat} hideModelSelector hideScheduledTask />
      </main>
    </div>
  );
}

function ShareFallbackState({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-screen min-h-0 flex-col items-center justify-center bg-[#fcfcfc] px-[24px]">
      {children}
    </div>
  );
}

function formatExpiry(value: string): string {
  // 后端 expires_at 是 naive UTC，直接用 new Date 会按本地时间解析（东八区早 8 小时）
  return formatClientMonthDayTime(value, value);
}
