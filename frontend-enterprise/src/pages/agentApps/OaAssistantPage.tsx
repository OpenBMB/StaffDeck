import { useEffect, useMemo, useRef, useState } from 'react';
import { Bookmark, ListChecks, MousePointerClick, Puzzle, ShieldCheck } from 'lucide-react';

import AppHeader from '@/components/AppHeader';
import { API_BASE_URL, api } from '@/api/client';
import type { EnterpriseAuthUser } from '@/auth';

import AgentAppBackButton from '../agentApps/AgentAppBackButton';

/**
 * OA 浏览器代填（书签面板）的安装页。
 *
 * 这条链路不在服务端开浏览器：书签把 `panel.js` 注入到用户当前的 OA 页面，
 * 面板在 OA 自己的 origin 里渲染表单并操作页面 —— 服务器部署也能用，用户零安装。
 */
export default function OaAssistantPage({
  currentUser,
  onLogout,
}: {
  currentUser?: EnterpriseAuthUser;
  onLogout?: () => void;
}) {
  const linkRef = useRef<HTMLAnchorElement>(null);

  // 面板脚本要走用户浏览器可达的绝对地址：OA 页面里没有 StaffDeck 的相对路径上下文。
  const panelUrl = useMemo(
    () => new URL(`${API_BASE_URL}/api/enterprise/oa-assistant/panel.js`, window.location.origin).href,
    [],
  );

  // 当前支持的报单场景：从后端配置下发（oa_assistant.json 的 scenarios），
  // 配置改了页面自动跟着变，不用在这里维护第二份清单。
  const [scenarios, setScenarios] = useState<{ id: string; name: string; summary: string; scene: string }[]>([]);
  const [scenariosError, setScenariosError] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .get<{ items: { id: string; name: string; summary: string; scene: string }[] }>(
        '/api/enterprise/oa-assistant/scenarios',
      )
      .then((data) => {
        if (alive) setScenarios(data.items || []);
      })
      .catch(() => {
        if (alive) setScenariosError(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  // 书签本体：小 loader。每次点击都先卸载旧面板、带 cache-buster 重新加载，
  // 保证面板代码/config 更新后不用手动刷新 OA 页面。
  const bookmarklet = useMemo(
    () =>
      'javascript:(function(){' +
      `var u=${JSON.stringify(panelUrl)};` +
      'if(window.__SD_OA_PANEL__&&window.__SD_OA_PANEL__.destroy){window.__SD_OA_PANEL__.destroy();}' +
      'var s=document.createElement("script");' +
      's.src=u+((u.indexOf("?")<0)?"?":"&")+"t="+Date.now();' +
      's.onerror=function(){alert("AI报销代填脚本加载失败：可能被 OA 页面 CSP 拦截，或网络不可达。");};' +
      'document.documentElement.appendChild(s);' +
      '})();',
    [panelUrl],
  );

  // React 会对 javascript: 形式的 href 发警告，直接落到 DOM 属性上，拖动安装才可靠。
  useEffect(() => {
    if (linkRef.current) linkRef.current.setAttribute('href', bookmarklet);
  }, [bookmarklet]);

  return (
    <div className="box-border min-h-full px-[48px] pb-[43px] pt-[20px] max-[900px]:px-[16px]">
      <AppHeader
        className="mb-[16px]"
        onLogout={onLogout}
        userName={currentUser?.username}
        title="Agent 广场 · AI 单据助手"
      />

      <div className="flex flex-col gap-[14px]">
        <div className="flex justify-end">
          <AgentAppBackButton />
        </div>

        {/* 安装 */}
        <section className="rounded-[20px] bg-white p-[20px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          <div className="flex items-center gap-[8px]">
            <Bookmark className="size-[15px] text-[#757f9c]" />
            <h2 className="text-[14px] font-medium text-[#18181a]">第一步：把按钮拖到书签栏</h2>
          </div>
          <p className="mt-[6px] text-[12px] leading-[1.8] text-[#858b9c]">
            书签栏默认隐藏，按 <kbd className="rounded border border-[#e3e7f1] bg-[#fafbfd] px-[5px] py-[1px] text-[11px]">Ctrl/⌘ + Shift + B</kbd> 显示。
            把下面这个按钮<strong className="text-[#464c5e]">拖到书签栏</strong>即可（无需安装任何软件）。
          </p>

          <div className="mt-[14px] flex flex-wrap items-center gap-[10px]">
            <a
              ref={linkRef}
              draggable
              onClick={(e) => e.preventDefault()}
              className="inline-flex h-[36px] cursor-grab items-center gap-[7px] rounded-[10px] border border-[#d6dbe8] bg-[#fafbfd] px-[14px] text-[13px] text-[#1f2333] no-underline hover:bg-[#f2f4f9] active:cursor-grabbing"
              title="拖我到书签栏"
            >
              <Puzzle className="size-[14px] text-[#757f9c]" />
              AI 单据助手
            </a>
          </div>

          <div className="mt-[12px] flex items-center gap-[8px] rounded-[10px] border border-[#bfded2] bg-[#eef7f3] px-[12px] py-[9px] text-[12.5px] font-medium text-[#0f6b4f]">
            <ShieldCheck className="size-[15px] shrink-0" />
            插件不保存个人数据，只用于任务执行。
          </div>
        </section>

        {/* 使用 */}
        <section className="rounded-[20px] bg-white p-[20px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          <div className="flex items-center gap-[8px]">
            <MousePointerClick className="size-[15px] text-[#757f9c]" />
            <h2 className="text-[14px] font-medium text-[#18181a]">第二步：在 OA 报销页面点这个书签</h2>
          </div>
          <ol className="mt-[10px] flex list-decimal flex-col gap-[6px] pl-[20px] text-[12.5px] leading-[1.9] text-[#464c5e]">
            <li>像平时一样打开 OA 报销表单页（已登录状态）。</li>
            <li>点书签栏里的「AI 单据助手」→ 右上角弹出面板。</li>
            <li>在面板里确认场景 / 事由 / 金额，点「代填」。</li>
            <li>面板会逐个字段操作当前 OA 页面，并显示每一步结果；失败的字段可以单独重试。</li>
            <li>默认<strong>不会自动点保存</strong>，先看填得对不对；确认无误后可勾选「填完后自动点保存」。</li>
          </ol>
          <p className="mt-[10px] rounded-[10px] border border-[#eef0f5] bg-[#fafbfd] p-[10px] text-[11.5px] leading-[1.8] text-[#858b9c]">
            说明：本方案不需要服务端浏览器，也不需要用户安装扩展。代价是<strong className="text-[#5b6274]">每次执行都要手动点一下书签</strong>（浏览器不允许网页替用户点书签），
            且若 OA 页面配置了严格 CSP，注入脚本可能被拦截（面板会给出提示）。
          </p>
        </section>

        {/* 支持的报单场景（后端配置下发） */}
        <section className="rounded-[20px] bg-white p-[20px] shadow-[0_-4px_16px_0_rgba(0,0,0,0.05)]">
          <div className="flex items-center gap-[8px]">
            <ListChecks className="size-[15px] text-[#757f9c]" />
            <h2 className="text-[14px] font-medium text-[#18181a]">当前支持的报单场景</h2>
            {scenarios.length > 0 && (
              <span className="font-mono text-[11px] text-[#a3aaba]">共 {scenarios.length} 个</span>
            )}
          </div>
          <p className="mt-[6px] text-[12px] leading-[1.8] text-[#858b9c]">
            打开对应场景的 OA 表单页再点书签，面板会自动切到该场景；也可以手动在面板顶部切换。
          </p>

          {scenariosError && (
            <p className="mt-[10px] text-[12px] text-[#c15a3a]">场景清单加载失败，请刷新重试。</p>
          )}
          {!scenariosError && scenarios.length === 0 && (
            <p className="mt-[10px] text-[12px] text-[#a3aaba]">正在加载场景清单…</p>
          )}

          <ul className="mt-[10px] flex flex-col gap-[8px]">
            {scenarios.map((item) => (
              <li
                key={item.id}
                className="rounded-[12px] border border-[#eef0f5] bg-[#fafbfd] px-[12px] py-[10px]"
              >
                <div className="flex flex-wrap items-center gap-[8px]">
                  <span className="text-[13px] font-medium text-[#1f2333]">{item.name}</span>
                  {item.scene && (
                    <span className="rounded-[6px] border border-[#dfe4ec] bg-white px-[6px] py-[1px] font-mono text-[10.5px] text-[#757f9c]">
                      报销场景：{item.scene}
                    </span>
                  )}
                </div>
                {item.summary && (
                  <p className="mt-[4px] text-[12px] leading-[1.8] text-[#5b6274]">{item.summary}</p>
                )}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
