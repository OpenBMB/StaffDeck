import { useState } from 'react';
import { LoaderCircle, LockKeyhole, UserRound } from 'lucide-react';

import { api, TENANT_ID } from '../api/client';
import { setEnterpriseAuthSession, type EnterpriseAuthSession } from '../auth';
import BrandLogo from '../components/BrandLogo';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import loginPoseLeft from '../assets/staffdeck/login-pose-left-team-v5.webp';
import loginPoseRight from '../assets/staffdeck/login-pose-right-team-v5.webp';
import loginPoseTopLeft from '../assets/staffdeck/login-pose-top-left-v5.webp';
import loginPoseTopRight from '../assets/staffdeck/login-pose-top-right-v5.webp';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

export default function LoginPage({ onLogin }: LoginPageProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [usernameError, setUsernameError] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [loading, setLoading] = useState(false);

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
      });
      setEnterpriseAuthSession(session);
      onLogin(session);
    } catch (error) {
      const messageText = error instanceof Error ? error.message : '登录失败';
      setUsernameError('账号输入错误');
      setPasswordError(messageText || '密码输入错误');
    } finally {
      setLoading(false);
    }
  }

  const usernameInvalid = Boolean(usernameError);
  const passwordInvalid = Boolean(passwordError);

  return (
    <div className="relative h-[100svh] overflow-hidden bg-[#e4eaf4]">
      <main className="staffdeck-login-main relative z-10 flex h-full min-h-0 items-center justify-center overflow-y-auto px-4 pb-6 pt-[76px]">
        <div className="staffdeck-login-stage relative w-full max-w-[400px]">
          <div className="staffdeck-login-poses" aria-hidden="true">
            <img
              src={loginPoseTopLeft}
              alt=""
              draggable={false}
              className="staffdeck-login-pose staffdeck-login-pose-top-left"
            />
            <img
              src={loginPoseTopRight}
              alt=""
              draggable={false}
              className="staffdeck-login-pose staffdeck-login-pose-top-right"
            />
            <img
              src={loginPoseLeft}
              alt=""
              draggable={false}
              className="staffdeck-login-pose staffdeck-login-pose-left"
            />
            <img
              src={loginPoseRight}
              alt=""
              draggable={false}
              className="staffdeck-login-pose staffdeck-login-pose-right"
            />
          </div>

          <section
            aria-labelledby="login-title"
            className="staffdeck-login-card relative z-20 w-full rounded-[8px] border border-[#dbe2e8] bg-white px-6 py-7 shadow-[0_24px_64px_rgba(28,45,56,0.18)] motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2 motion-safe:duration-500 sm:px-8 sm:py-8"
          >
          <BrandLogo
            markSize={40}
            className="gap-[10px] p-0 [&_strong]:text-[22px] [&_strong]:font-semibold"
          />

          <div className="staffdeck-login-heading mt-8">
            <h1
              id="login-title"
              className="text-[30px] font-semibold leading-[38px] text-[#18181a]"
            >
              欢迎回来
            </h1>
            <p className="mt-2 text-[14px] leading-[22px] text-[#646b7c]">
              登录 StaffDeck，开始管理你的数字员工
            </p>
          </div>

          <form
            className="staffdeck-login-form mt-7 flex flex-col gap-5"
            onSubmit={(event) => {
              event.preventDefault();
              void login();
            }}
          >
            <div>
              <label
                htmlFor="login-username"
                className="mb-2 block text-[13px] font-semibold leading-5 text-[#313745]"
              >
                账号
              </label>
              <div
                className={`relative flex h-12 items-center rounded-[8px] border bg-white transition-[border-color,box-shadow] ${
                  usernameInvalid
                    ? 'border-[#dc2626] shadow-[0_0_0_3px_rgba(220,38,38,0.10)] focus-within:shadow-[0_0_0_3px_rgba(220,38,38,0.22)]'
                    : 'border-[#dbe2e8] focus-within:border-[#315efe] focus-within:shadow-[0_0_0_3px_rgba(49,94,254,0.16)]'
                }`}
              >
                <UserRound aria-hidden="true" className="absolute left-3.5 size-[18px] text-[#747d8d]" />
                <input
                  id="login-username"
                  value={username}
                  autoComplete="username"
                  placeholder="请输入账号"
                  aria-label="账号"
                  aria-invalid={usernameInvalid}
                  aria-describedby={usernameInvalid ? 'login-username-error' : undefined}
                  onChange={(event) => {
                    setUsername(event.target.value);
                    if (usernameError) setUsernameError('');
                  }}
                  className="h-full min-w-0 flex-1 border-0 bg-transparent pl-11 pr-12 text-[14px] text-[#18181a] outline-none placeholder:text-[#646b7c]"
                />
                {username && (
                  <button
                    type="button"
                    aria-label="清空账号"
                    onClick={() => {
                      setUsername('');
                      setUsernameError('');
                    }}
                    className="absolute right-0 grid size-11 place-items-center rounded-[8px] text-[#747d8d] outline-none hover:text-[#313745] focus-visible:ring-2 focus-visible:ring-[#315efe] focus-visible:ring-offset-1"
                  >
                    <IconFieldClear className="size-[18px]" />
                  </button>
                )}
              </div>
              {usernameInvalid && (
                <p
                  id="login-username-error"
                  aria-live="polite"
                  className="mt-1.5 text-[12px] leading-[18px] text-[#dc2626]"
                >
                  {usernameError}
                </p>
              )}
            </div>

            <div>
              <label
                htmlFor="login-password"
                className="mb-2 block text-[13px] font-semibold leading-5 text-[#313745]"
              >
                密码
              </label>
              <div
                className={`relative flex h-12 items-center rounded-[8px] border bg-white transition-[border-color,box-shadow] ${
                  passwordInvalid
                    ? 'border-[#dc2626] shadow-[0_0_0_3px_rgba(220,38,38,0.10)] focus-within:shadow-[0_0_0_3px_rgba(220,38,38,0.22)]'
                    : 'border-[#dbe2e8] focus-within:border-[#315efe] focus-within:shadow-[0_0_0_3px_rgba(49,94,254,0.16)]'
                }`}
              >
                <LockKeyhole
                  aria-hidden="true"
                  className="absolute left-3.5 size-[18px] text-[#747d8d]"
                />
                <input
                  id="login-password"
                  value={password}
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="请输入密码"
                  aria-label="密码"
                  aria-invalid={passwordInvalid}
                  aria-describedby={passwordInvalid ? 'login-password-error' : undefined}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    if (passwordError) setPasswordError('');
                  }}
                  className="h-full min-w-0 flex-1 border-0 bg-transparent pl-11 pr-12 text-[14px] text-[#18181a] outline-none placeholder:text-[#646b7c]"
                />
                <button
                  type="button"
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  onClick={() => setShowPassword((prev) => !prev)}
                  className="absolute right-0 grid size-11 place-items-center rounded-[8px] text-[#747d8d] outline-none hover:text-[#313745] focus-visible:ring-2 focus-visible:ring-[#315efe] focus-visible:ring-offset-1"
                >
                  {showPassword ? (
                    <IconFieldEyeOn className="size-[18px]" />
                  ) : (
                    <IconFieldEye className="size-[18px]" />
                  )}
                </button>
              </div>
              {passwordInvalid && (
                <p
                  id="login-password-error"
                  aria-live="polite"
                  className="mt-1.5 text-[12px] leading-[18px] text-[#dc2626]"
                >
                  {passwordError}
                </p>
              )}
            </div>

            <button
              type="submit"
              disabled={loading}
              aria-busy={loading}
              className="flex h-12 w-full items-center justify-center gap-2 rounded-[8px] bg-[#18181a] px-4 text-[15px] font-semibold text-white outline-none hover:bg-[#303035] focus-visible:ring-2 focus-visible:ring-[#315efe] focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {loading && <LoaderCircle aria-hidden="true" className="size-[17px] animate-spin" />}
              {loading ? '登录中…' : '登录'}
            </button>
          </form>

          <p className="staffdeck-login-helper mt-5 text-center text-[12px] leading-[18px] text-[#646b7c]">
            首次使用：账号与密码均为 admin
          </p>

          <div className="staffdeck-login-divider my-6 h-px bg-[#edf0f3]" />

          <p className="text-center text-[12px] leading-[18px] text-[#646b7c]">
            StaffDeck 数字员工运营平台
          </p>
          </section>
        </div>
      </main>
    </div>
  );
}
