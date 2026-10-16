import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent } from 'react';

import { animate, createTimeline, stagger, text, utils } from 'animejs';

import { api, TENANT_ID, ApiError } from '../api/client';
import {
  readRememberedUsername,
  readRememberLoginPreference,
  setEnterpriseAuthSession,
  writeRememberedUsername,
  writeRememberLoginPreference,
  type EnterpriseAuthSession,
} from '../auth';
import AppHeader from '../components/AppHeader';
import BrandLogo from '../components/BrandLogo';
import EmployeeAvatar from '../components/EmployeeAvatar';
import LoginCardStack from '../components/LoginCardStack';
import LoginSopFlow from '../components/LoginSopFlow';
import LoginWingCards from '../components/LoginWingCards';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import { EMPLOYEE_AVATAR_PRESETS } from '../employee';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

/**
 * 登录页的「在岗播报」内容：岗位、头像与职责全部取自产品里真实的数字员工，
 * 不是营销文案——第一眼就能看出这个平台到底在做什么。
 */
const STAFF_ON_DUTY = [
  { role: '市场', avatarPreset: 'marketing-spark', duty: '梳理市场目标、受众与内容策划清单' },
  { role: '数据分析', avatarPreset: 'data-insight', duty: '定义指标口径、分析周期与对比维度' },
  { role: '采购', avatarPreset: 'procurement-check', duty: '准备采购需求、供应商比较与审批材料' },
  { role: '项目管理', avatarPreset: 'project-board', duty: '拆解目标、里程碑、责任人与风险' },
  { role: '行政', avatarPreset: 'after-sales-seal', duty: '统筹会议室预订、办公用品与用章申请' },
  { role: '销售', avatarPreset: 'sales-handshake', duty: '澄清客户需求、准备沟通材料并推进商机' },
] as const;

/** 入场前先藏起来，避免首帧闪一下再跳到起点（useEffect 在首帧绘制后才跑）。 */
function hiddenFrom(offsetY: number): CSSProperties {
  return { opacity: 0, transform: `translateY(${offsetY}px)` };
}

function avatarProfile(preset: string) {
  const found = EMPLOYEE_AVATAR_PRESETS.find((item) => item.key === preset);
  return {
    avatarKind: 'preset' as const,
    avatarImage: '',
    avatarPreset: preset,
    avatarText: found?.text ?? '员',
    avatarTone: found?.tone ?? 'teal',
  };
}

/**
 * 未登录的落地 / 登录页。
 *
 * 版式：品牌眼眉 + 产品主标题（按字入场）→ 副标题 → 在岗播报 →
 * 「视差浮层卡片堆」登录框（前排表单卡 + 后排界面卡随光标 3D 转动）
 * → 底部「SOP 光路流水线」：一条光路穿过四个能力节点，光脉冲循环跑完全程。
 * 两翼（宽屏）放「工作快照」卡：左执行中任务、右已交付成果——
 * 中央是「你」，两侧是数字员工正在干的活。
 * 背景只放几团缓慢漂移的光斑和一层淡网格：留白不空，但不抢内容。
 */
export default function LoginPage({ onLogin }: LoginPageProps) {
  const [username, setUsername] = useState(() => readRememberedUsername());
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  // 「记住账号」：勾选后服务端签发 30 天长效凭证，本机 30 天内免登
  const [remember, setRemember] = useState(() => readRememberLoginPreference());
  const [usernameError, setUsernameError] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [loading, setLoading] = useState(false);

  const pageRef = useRef<HTMLDivElement>(null);
  const brandRef = useRef<HTMLSpanElement>(null);
  const titleRef = useRef<HTMLSpanElement>(null);
  const subtitleRef = useRef<HTMLParagraphElement>(null);
  const tickerRef = useRef<HTMLDivElement>(null);

  async function login() {
    const trimmedUsername = username.trim();
    const trimmedPassword = password.trim();
    setUsernameError(trimmedUsername ? '' : '请输入账号');
    setPasswordError(trimmedPassword ? '' : '请输入密码');
    if (!trimmedUsername || !trimmedPassword) return;

    setLoading(true);
    try {
      const session = await api.post<EnterpriseAuthSession>('/api/auth/login', {
        tenant_id: TENANT_ID,
        username: trimmedUsername,
        password: trimmedPassword,
        remember,
      });
      // 勾选态与账号名先落盘再进应用：登录成功就算数，不依赖组件是否还被挂着
      writeRememberLoginPreference(remember);
      writeRememberedUsername(remember ? trimmedUsername : null);
      setEnterpriseAuthSession(session);
      onLogin(session);
    } catch (error) {
      const messageText = error instanceof Error ? error.message : '';
      const fallback = '登录失败，请稍后重试';
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        setUsernameError('账号或密码不正确');
        setPasswordError('请检查后重新输入');
      } else {
        setUsernameError('账号输入错误');
        setPasswordError(messageText || fallback);
      }
    } finally {
      setLoading(false);
    }
  }

  function onFieldKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') void login();
  }

  useEffect(() => {
    const page = pageRef.current;
    const brand = brandRef.current;
    const title = titleRef.current;
    const subtitle = subtitleRef.current;
    const ticker = tickerRef.current;
    if (!page || !brand || !title || !subtitle || !ticker) return;

    const tickerRows = Array.from(ticker.querySelectorAll<HTMLElement>('[data-ticker-row]'));
    const auroraBlobs = Array.from(page.querySelectorAll<HTMLElement>('[data-aurora]'));
    const liveDots = Array.from(page.querySelectorAll<HTMLElement>('[data-live-dot]'));
    const heroBlocks = [brand, subtitle, ticker];

    // 主标题按字入场。拆字依赖 ResizeObserver，jsdom 等环境没有它，
    // 那就退化成整行出现，不为了动画把页面渲染搞挂。
    const splitter = typeof ResizeObserver === 'undefined'
      ? null
      : text.splitText(title, { chars: true, words: false, lines: false });
    const titleParts: HTMLElement[] = splitter?.chars?.length
      ? (splitter.chars as HTMLElement[])
      : [title];

    const disposers: Array<() => void> = [];
    if (splitter) disposers.push(() => splitter.revert());
    const disposeAll = () => {
      for (let i = disposers.length - 1; i >= 0; i -= 1) disposers[i]();
    };

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      utils.set([...heroBlocks, title], { opacity: 1, y: 0, scale: 1 });
      utils.set(tickerRows[0], { y: '0%', opacity: 1 });
      return disposeAll;
    }

    // 背景光斑：十几秒一轮的缓慢漂移，让大面积留白「活」着
    auroraBlobs.forEach((blob, index) => {
      const drift = animate(blob, {
        x: [0, index % 2 ? -70 : 80],
        y: [0, index % 2 ? 50 : -60],
        scale: [1, index % 2 ? 1.18 : 0.9],
        duration: 11000 + index * 2600,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
      });
      disposers.push(() => drift.revert());
    });

    // 在岗小绿点：呼吸
    liveDots.forEach((dot, index) => {
      const pulse = animate(dot, {
        scale: [1, 1.6],
        opacity: [1, 0.25],
        duration: 1000,
        ease: 'inOutSine',
        loop: true,
        alternate: true,
        delay: index * 130,
      });
      disposers.push(() => pulse.revert());
    });

    // 主入场：眼眉 → 标题逐字 → 副标题 → 在岗播报（登录卡堆入场由 LoginCardStack 自理）
    const intro = createTimeline({ defaults: { duration: 720, ease: 'outQuint' } });
    intro
      .set(title, { opacity: 1 }, 0)
      .add(brand, { opacity: [0, 1], y: [14, 0] }, 0)
      .add(brand, { letterSpacing: ['0.62em', '0.2em'], duration: 1400, ease: 'outExpo' }, 0)
      .add(
        titleParts,
        { opacity: [0, 1], y: ['0.7em', '0em'], duration: 900, delay: stagger(30), ease: 'outExpo' },
        140,
      )
      .add(subtitle, { opacity: [0, 1], y: [16, 0] }, '-=540')
      .add(ticker, { opacity: [0, 1], y: [14, 0] }, '-=400');
    disposers.push(() => intro.revert());

    // 在岗播报：6 条职责循环滚过
    if (tickerRows.length > 1) {
      const loop = createTimeline({ loop: true });
      tickerRows.forEach((row, index) => {
        loop
          .add(
            row,
            { y: ['110%', '0%'], opacity: [0, 1], duration: 560, ease: 'outQuint' },
            index === 0 ? 0 : '+=140',
          )
          .add(row, { y: ['0%', '-110%'], opacity: [1, 0], duration: 470, ease: 'inQuad' }, '+=1500');
      });
      disposers.push(() => loop.revert());
    }

    return disposeAll;
  }, []);

  const inputBaseClass =
    'flex h-[44px] w-full items-center gap-[8px] rounded-[10px] border bg-white px-[16px] transition-colors';
  return (
    <div ref={pageRef} className="relative flex min-h-screen flex-col overflow-x-clip bg-[#fbfbfa]">
      {/* 背景层：光斑 + 淡网格，纯装饰 */}
      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        <div
          data-aurora
          className="absolute -left-[14%] top-[4%] h-[420px] w-[540px] rounded-full bg-[radial-gradient(circle,rgba(15,118,110,0.18),transparent_68%)] blur-[70px]"
        />
        <div
          data-aurora
          className="absolute -right-[12%] top-[22%] h-[460px] w-[520px] rounded-full bg-[radial-gradient(circle,rgba(168,93,50,0.14),transparent_68%)] blur-[80px]"
        />
        <div
          data-aurora
          className="absolute left-[30%] top-[58%] h-[380px] w-[520px] rounded-full bg-[radial-gradient(circle,rgba(111,123,66,0.13),transparent_70%)] blur-[80px]"
        />
        <div className="absolute inset-0 bg-[radial-gradient(rgba(24,24,26,0.05)_1px,transparent_1px)] [background-size:26px_26px] [mask-image:radial-gradient(ellipse_at_50%_36%,black,transparent_72%)]" />
      </div>

      <AppHeader
        className="relative z-10 h-[60px] shrink-0 px-[32px] pt-[10px]"
        left={<BrandLogo markSize={28} />}
        right={null}
      />

      <main className="relative z-10 flex flex-1 flex-col items-center px-[32px] pb-[10px]">
        {/* 两翼工作快照：左侧执行中任务卡 + 右侧已交付成果卡（窄屏自动隐藏） */}
        <LoginWingCards />

        <div className="flex w-full max-w-[1120px] flex-col items-center pt-[46px] text-center">
          {/* 两行做层级化排版：公司名作眼眉（小号、宽字距、弱色）承接品牌，
              产品名作主标题。拆成两个 span 是为了让读屏把公司名与产品名分开读，
              中间的空白文本节点不能省。 */}
          <h1 className="flex flex-col items-center text-center">
            <span
              ref={brandRef}
              style={hiddenFrom(14)}
              className="mr-[-4px] text-[20px] font-medium leading-[30px] tracking-[4px] text-[#757f9c]"
            >
              地中海度假集团
            </span>
            {' '}
            <span
              ref={titleRef}
              style={{ opacity: 0 }}
              className="mt-[10px] text-[40px] font-semibold leading-[52px] tracking-[0.8px] text-[#18181a]"
            >
              AI 数字员工平台
            </span>
          </h1>

          <p
            ref={subtitleRef}
            style={hiddenFrom(16)}
            className="mt-[18px] max-w-[560px] text-[16px] leading-[26px] text-[#5b6274]"
          >
            把经验、流程与判断标准，沉淀成持续在岗的数字员工
          </p>

          {/* 在岗播报：一次只露一条，逐条滚过（置于登录框上方） */}
          <div
            ref={tickerRef}
            style={hiddenFrom(14)}
            className="relative mt-[26px] h-[52px] w-full max-w-[520px] overflow-hidden"
          >
            {STAFF_ON_DUTY.map((item) => (
              <div
                key={item.role}
                data-ticker-row
                style={{ opacity: 0, transform: 'translateY(110%)' }}
                className="absolute inset-0 flex items-center gap-[10px] rounded-[12px] border border-black/[0.05] bg-white/85 px-[14px] text-left shadow-[0_6px_20px_rgba(17,17,17,0.05)] backdrop-blur"
              >
                <EmployeeAvatar profile={avatarProfile(item.avatarPreset)} size={34} radius={10} />
                <span className="flex min-w-0 flex-1 items-baseline gap-[8px]">
                  <span className="shrink-0 text-[13px] font-medium text-[#18181a]">
                    {item.role}
                  </span>
                  <span className="truncate text-[12.5px] text-[#757f9c]">{item.duty}</span>
                </span>
                <span className="flex shrink-0 items-center gap-[6px] text-[12px] text-[#138a55]">
                  <i data-live-dot className="size-[6px] rounded-full bg-[#138a55]" />
                  在岗
                </span>
              </div>
            ))}
          </div>

          {/* 登录框：视差浮层卡片堆，前排为真实表单 */}
          <LoginCardStack>
            <form
              className="flex flex-col gap-[12px]"
              onSubmit={(event) => {
                event.preventDefault();
                void login();
              }}
            >
              <div
                className={`${inputBaseClass} ${usernameError ? 'border-[#f54a45]' : username ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
              >
                <input
                  value={username}
                  autoComplete="username"
                  placeholder="请输入OA邮箱"
                  aria-label="账号"
                  onChange={(event) => {
                    setUsername(event.target.value);
                    if (usernameError) setUsernameError('');
                  }}
                  onKeyDown={onFieldKeyDown}
                  className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181a] outline-none placeholder:text-[#757f9c]"
                />
                {username && (
                  <button
                    type="button"
                    aria-label="清空账号"
                    onClick={() => {
                      setUsername('');
                      setUsernameError('');
                    }}
                    className="grid size-[18px] shrink-0 place-items-center text-[#667085] outline-none transition-colors hover:text-[#464c5e]"
                  >
                    <IconFieldClear className="size-[18px]" />
                  </button>
                )}
              </div>
              {usernameError && (
                <p className="-mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                  {usernameError}
                </p>
              )}

              <div
                className={`${inputBaseClass} ${passwordError ? 'border-[#f54a45]' : password ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
              >
                <input
                  value={password}
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="请输入密码"
                  aria-label="密码"
                  onChange={(event) => {
                    setPassword(event.target.value);
                    if (passwordError) setPasswordError('');
                  }}
                  onKeyDown={onFieldKeyDown}
                  className="min-w-0 flex-1 border-0 bg-transparent text-[14px] text-[#18181a] outline-none placeholder:text-[#757f9c]"
                />
                <button
                  type="button"
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  onClick={() => setShowPassword((prev) => !prev)}
                  className="grid size-[18px] shrink-0 place-items-center text-[#677185] outline-none transition-colors hover:text-[#464c5e]"
                >
                  {showPassword ? (
                    <IconFieldEyeOn className="size-[18px]" />
                  ) : (
                    <IconFieldEye className="size-[18px]" />
                  )}
                </button>
              </div>
              {passwordError && (
                <p className="-mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                  {passwordError}
                </p>
              )}

              {/* 「记住账号」：勾选后 30 天内免登（凭证有效期由服务端签发）。
                  复选框自绘，不用浏览器原生控件——原生勾选框在这套版式里太糙。 */}
              <div className="mt-[2px] flex items-center justify-between gap-[10px]">
                <label className="flex cursor-pointer select-none items-center gap-[8px]">
                  <input
                    type="checkbox"
                    checked={remember}
                    onChange={(event) => setRemember(event.target.checked)}
                    aria-label="记住账号"
                    className="peer sr-only"
                  />
                  <span
                    aria-hidden
                    className="grid size-[16px] shrink-0 place-items-center rounded-[5px] border border-[#dfe3ec] bg-white text-white transition-colors peer-checked:border-[#18181a] peer-checked:bg-[#18181a] peer-focus-visible:ring-2 peer-focus-visible:ring-[#18181a]/25 peer-focus-visible:ring-offset-1"
                  >
                    {remember && (
                      <svg viewBox="0 0 12 12" className="size-[10px]">
                        <path
                          d="M1.6 6.3 4.3 9 10.4 3"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.9"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    )}
                  </span>
                  <span className="text-[13px] leading-none text-[#5b6274]">记住账号</span>
                </label>
                <span className="shrink-0 text-[12px] leading-none text-[#757f9c]">
                  30 天内免登录
                </span>
              </div>

              <button
                type="submit"
                disabled={loading}
                className="mt-[6px] flex h-[42px] w-full items-center justify-center rounded-[10px] bg-[#18181a] text-[15px] font-normal text-white transition-colors hover:bg-[#18181a]/90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? '登录中…' : '登录'}
              </button>
            </form>
          </LoginCardStack>
        </div>

        {/* 底部：SOP 光路流水线（替代原产品截图预览） */}
        <LoginSopFlow />
      </main>
    </div>
  );
}
