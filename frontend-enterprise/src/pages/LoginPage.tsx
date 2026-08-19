import { useState, type KeyboardEvent } from 'react';

import { api, TENANT_ID } from '../api/client';
import { setEnterpriseAuthSession, type EnterpriseAuthSession } from '../auth';
import AppHeader from '../components/AppHeader';
import BrandLogo from '../components/BrandLogo';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import loginPreview from '../assets/staffdeck/login-preview.png';

export type LoginPageProps = {
  onLogin: (session: EnterpriseAuthSession) => void;
};

/**
 * Signed-out landing / login page. Mirrors Figma node 68:201 (`Login_light`):
 * a full-bleed hero with the StaffDeck wordmark and a product-preview placeholder
 * anchored to the bottom. Clicking "登录" slides the credentials form (node 68:1563)
 * down into view in place of the call-to-action button.
 */
export default function LoginPage({ onLogin }: LoginPageProps) {
  const [showForm, setShowForm] = useState(false);
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

  function onFieldKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') void login();
  }

  const inputBaseClass =
    'flex h-[44px] w-full items-center gap-[8px] rounded-[10px] border bg-white px-[16px] transition-colors';

  return (
    <div className="relative flex min-h-screen flex-col bg-white">
      <AppHeader
        className="h-[60px] shrink-0 px-[32px]"
        left={<BrandLogo markSize={28} />}
        right={null}
      />

      <main className="flex flex-1 flex-col items-center px-[32px]">
        <div className="flex flex-col items-center pt-[60px]">
          <span className="flex items-center justify-center rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-[#f6f6f6] px-[20px] py-[6px] text-[14px] text-[#464c5e]">
            我们来做什么？
          </span>
          <h1 className="mt-[6px] text-center text-[54px] font-semibold leading-[80px] tracking-[1.08px] text-[#18181a]">
            StaffDeck
            <br />
            数字员工运营平台
          </h1>

          {!showForm ? (
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="mt-[24px] flex items-center justify-center rounded-[10px] bg-[#18181a] px-[36px] py-[10px] text-[16px] font-normal text-white transition-colors hover:bg-[#18181a]/90"
            >
              登录
            </button>
          ) : (
            <form
              className="mt-[24px] flex w-[320px] flex-col duration-300 ease-out animate-in fade-in slide-in-from-top-4"
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
                  placeholder="请输入账号（首次使用请输入admin）"
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

              <div
                className={`mt-[24px] ${inputBaseClass} ${passwordError ? 'border-[#f54a45]' : password ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
              >
                <input
                  value={password}
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  placeholder="请输入密码（首次使用请输入admin）"
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

              <button
                type="submit"
                disabled={loading}
                className="mt-[24px] flex h-[40px] w-[120px] items-center justify-center self-center rounded-[10px] bg-[#18181a] text-[16px] font-normal text-white transition-colors hover:bg-[#18181a]/90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? '登录中…' : '登录'}
              </button>
            </form>
          )}
        </div>

        <div className="mt-[32px] flex w-full justify-center">
          <img
            src={loginPreview}
            alt="StaffDeck 产品预览"
            className="h-auto w-full max-w-[1200px] select-none object-contain"
            draggable={false}
          />
        </div>
      </main>
    </div>
  );
}
