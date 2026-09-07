import { useState, type KeyboardEvent } from 'react';

import { api, TENANT_ID, ApiError } from '../api/client';
import { setEnterpriseAuthSession, type EnterpriseAuthSession } from '../auth';
import AppHeader from '../components/AppHeader';
import BrandLogo from '../components/BrandLogo';
import IconFieldClear from '../assets/icons/field-clear.svg?react';
import IconFieldEye from '../assets/icons/field-eye.svg?react';
import IconFieldEyeOn from '../assets/icons/field-eye-on.svg?react';
import loginPreview from '../assets/staffdeck/login-preview.png';
import { Dialog, DialogContent, DialogTitle, Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { cn } from '@/lib/utils';

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

  // 注册弹窗
  const [registerOpen, setRegisterOpen] = useState(false);
  const [regUsername, setRegUsername] = useState('');
  const [regName, setRegName] = useState('');
  const [regDepartment, setRegDepartment] = useState('');
  const [regPassword, setRegPassword] = useState('');
  const [regConfirm, setRegConfirm] = useState('');
  const [regUsernameError, setRegUsernameError] = useState('');
  const [regNameError, setRegNameError] = useState('');
  const [regPasswordError, setRegPasswordError] = useState('');
  const [regConfirmError, setRegConfirmError] = useState('');
  const [regSubmitError, setRegSubmitError] = useState('');
  const [regSubmitting, setRegSubmitting] = useState(false);

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

  async function register() {
    const account = regUsername.trim();
    const name = regName.trim();
    const department = regDepartment.trim();
    setRegUsernameError(account ? '' : '请输入账号');
    setRegNameError(name ? '' : '请输入名字');
    setRegPasswordError(regPassword ? '' : '请输入密码');
    setRegConfirmError('');
    setRegSubmitError('');
    if (!account || !name || !regPassword) return;
    if (regPassword.length < 6) {
      setRegPasswordError('密码至少 6 位');
      return;
    }
    if (regPassword !== regConfirm) {
      setRegConfirmError('两次输入的密码不一致');
      return;
    }

    setRegSubmitting(true);
    try {
      await api.post('/api/auth/register', {
        tenant_id: TENANT_ID,
        username: account,
        display_name: name,
        department: department || undefined,
        password: regPassword,
      });
      notify.success('注册成功，请使用账号登录');
      setRegisterOpen(false);
      setRegUsername('');
      setRegName('');
      setRegDepartment('');
      setRegPassword('');
      setRegConfirm('');
      setShowForm(true);
      setUsername(account);
    } catch (error) {
      const message = error instanceof ApiError ? error.message : '注册失败，请稍后重试';
      setRegSubmitError(message);
    } finally {
      setRegSubmitting(false);
    }
  }

  const inputBaseClass =
    'flex h-[44px] w-full items-center gap-[8px] rounded-[10px] border bg-white px-[16px] transition-colors';

  return (
    <div className="relative flex min-h-screen flex-col bg-white">
      <AppHeader
        className="h-[60px] shrink-0 px-[32px]"
        left={<BrandLogo markSize={44} />}
        right={null}
      />

      <main className="flex flex-1 flex-col items-center px-[32px]">
        <div className="flex flex-col items-center pt-[60px]">
          <h1 className="mt-[6px] text-center text-[54px] font-semibold leading-[80px] tracking-[1.08px] text-[#18181a]">
            复星旅文
            <br />
            AI数字员工平台
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
                  placeholder="请输入账号"
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
                <p className="mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                  {usernameError}
                </p>
              )}

              <div
                className={`mt-[24px] ${inputBaseClass} ${passwordError ? 'border-[#f54a45]' : password ? 'border-[#18181a]' : 'border-[#e3e7f1]'}`}
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
                <p className="mt-[6px] text-[12px] leading-none text-[#f54a45]" role="alert">
                  {passwordError}
                </p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="mt-[24px] flex h-[40px] w-[120px] items-center justify-center self-center rounded-[10px] bg-[#18181a] text-[16px] font-normal text-white transition-colors hover:bg-[#18181a]/90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? '登录中…' : '登录'}
              </button>

              <div className="mt-[14px] flex items-center justify-center gap-[4px] text-[13px] text-[#757f9c]">
                <span>还没有账号？</span>
                <button
                  type="button"
                  onClick={() => {
                    setRegNameError('');
                    setRegPasswordError('');
                    setRegConfirmError('');
                    setRegSubmitError('');
                    setRegisterOpen(true);
                  }}
                  className="font-medium text-[#1a71ff] outline-none transition-colors hover:text-[#4a8dff]"
                >
                  立即注册
                </button>
              </div>
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

      <Dialog open={registerOpen} onOpenChange={(next) => !next && setRegisterOpen(false)}>
        <DialogContent
          aria-describedby={undefined}
          className="flex w-[calc(100%-2rem)] flex-col gap-[16px] overflow-hidden rounded-[14px] px-[20px] py-[16px] sm:max-w-[420px]"
        >
          <DialogTitle className="text-[16px] font-semibold text-[#18181a]">注册账号</DialogTitle>
          <p className="text-[12px] leading-[18px] text-[#858b9c]">
            注册后请使用「账号」登录，账号默认普通成员权限。
          </p>

          <div className="flex flex-col gap-[14px]">
            <label className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">账号</span>
              <Input
                value={regUsername}
                autoComplete="username"
                placeholder="用于登录，例如 zhangsan"
                onChange={(event) => {
                  setRegUsername(event.target.value);
                  if (regUsernameError) setRegUsernameError('');
                  if (regSubmitError) setRegSubmitError('');
                }}
                className={cn('h-[40px] text-[13px]', regUsernameError && 'border-[#f54a45]')}
              />
              {regUsernameError && (
                <span className="text-[12px] leading-none text-[#f54a45]" role="alert">
                  {regUsernameError}
                </span>
              )}
            </label>

            <label className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">名字</span>
              <Input
                value={regName}
                autoComplete="name"
                placeholder="用于显示，例如 张三"
                onChange={(event) => {
                  setRegName(event.target.value);
                  if (regNameError) setRegNameError('');
                  if (regSubmitError) setRegSubmitError('');
                }}
                className={cn('h-[40px] text-[13px]', regNameError && 'border-[#f54a45]')}
              />
              {regNameError && (
                <span className="text-[12px] leading-none text-[#f54a45]" role="alert">
                  {regNameError}
                </span>
              )}
            </label>

            <label className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">部门</span>
              <Input
                value={regDepartment}
                autoComplete="organization"
                placeholder="例如 研发一部"
                onChange={(event) => setRegDepartment(event.target.value)}
                className="h-[40px] text-[13px]"
              />
            </label>

            <label className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">密码</span>
              <Input
                type="password"
                value={regPassword}
                autoComplete="new-password"
                placeholder="至少 6 位"
                onChange={(event) => {
                  setRegPassword(event.target.value);
                  if (regPasswordError) setRegPasswordError('');
                  if (regSubmitError) setRegSubmitError('');
                }}
                className={cn('h-[40px] text-[13px]', regPasswordError && 'border-[#f54a45]')}
              />
              {regPasswordError && (
                <span className="text-[12px] leading-none text-[#f54a45]" role="alert">
                  {regPasswordError}
                </span>
              )}
            </label>

            <label className="flex flex-col gap-[6px]">
              <span className="text-[12px] font-medium text-[#464c5e]">密码确认</span>
              <Input
                type="password"
                value={regConfirm}
                autoComplete="new-password"
                placeholder="再次输入密码"
                onChange={(event) => {
                  setRegConfirm(event.target.value);
                  if (regConfirmError) setRegConfirmError('');
                  if (regSubmitError) setRegSubmitError('');
                }}
                className={cn('h-[40px] text-[13px]', regConfirmError && 'border-[#f54a45]')}
              />
              {regConfirmError && (
                <span className="text-[12px] leading-none text-[#f54a45]" role="alert">
                  {regConfirmError}
                </span>
              )}
            </label>

            {regSubmitError && (
              <p className="rounded-[8px] bg-[#fdeeee] px-[10px] py-[8px] text-[12px] leading-[18px] text-[#c0392b]" role="alert">
                {regSubmitError}
              </p>
            )}
          </div>

          <div className="flex items-center justify-end gap-[8px]">
            <UIButton
              variant="outline"
              disabled={regSubmitting}
              onClick={() => setRegisterOpen(false)}
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
            >
              取消
            </UIButton>
            <UIButton
              disabled={regSubmitting}
              onClick={() => void register()}
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] text-[14px] font-normal text-white hover:bg-[#303030]"
            >
              {regSubmitting ? '注册中…' : '注册'}
            </UIButton>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
