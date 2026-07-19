import { useEffect, useRef, useState } from 'react';
import QRCode from 'qrcode';
import { notify } from '@/components/ui/app-toast';

import { Input } from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';

import { api, TENANT_ID } from '../../api/client';
import type { ChannelBindingRead } from '../../types';
import { StatusBadge } from '../scheduled-tasks/StatusBadge';

type WechatQrcodeResponse = {
  qrcode?: string;
  qrcode_img_content?: string;
  qrcode_img_url?: string;
};

type WechatQrcodeStatusResponse = {
  status?: string;
};

type QrState = {
  qrcode: string;
  content: string;
  imageUrl: string;
};

const PRIMARY_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] bg-[#18181a] px-5 text-[12px] font-normal text-white hover:bg-[#303030]';
const OUTLINE_BUTTON_CLASS =
  'h-8 gap-1 rounded-[10px] border-[#e3e7f1] px-5 text-[12px] font-normal text-[#464c5e] hover:bg-[#f6f6f6] hover:text-[#18181a]';

export default function WechatSetup({
  binding,
  onChanged,
}: {
  binding: ChannelBindingRead;
  onChanged: () => void;
}) {
  const [qr, setQr] = useState<QrState | null>(null);
  const [qrStatus, setQrStatus] = useState('');
  const [qrLoading, setQrLoading] = useState(false);
  const [verifyCode, setVerifyCode] = useState('');
  const qrSessionRef = useRef(0);
  const pollTimerRef = useRef<number | null>(null);
  const verifyCodeRef = useRef('');

  useEffect(() => {
    return () => {
      qrSessionRef.current += 1;
      clearPollTimer();
    };
  }, []);

  useEffect(() => {
    resetQrFlow();
  }, [binding.id]);

  function clearPollTimer() {
    if (pollTimerRef.current != null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }

  function resetQrFlow() {
    qrSessionRef.current += 1;
    clearPollTimer();
    verifyCodeRef.current = '';
    setVerifyCode('');
    setQr(null);
    setQrStatus('');
  }

  async function startQr(bindingId: string) {
    const session = ++qrSessionRef.current;
    clearPollTimer();
    verifyCodeRef.current = '';
    setVerifyCode('');
    setQrLoading(true);
    setQrStatus('');
    try {
      const result = await api.post<WechatQrcodeResponse>(
        `/api/enterprise/channels/${bindingId}/wechat/qrcode?tenant_id=${TENANT_ID}`,
      );
      const code = String(result.qrcode || '');
      const content = String(result.qrcode_img_content || result.qrcode_img_url || '');
      if (!code || !content) throw new Error('获取微信二维码失败');
      const imageUrl = await QRCode.toDataURL(content, { width: 220, margin: 1 });
      if (session !== qrSessionRef.current) return;
      setQr({ qrcode: code, content, imageUrl });
      setQrStatus('wait');
      scheduleStatusPoll(bindingId, code, session);
    } catch (error) {
      if (session === qrSessionRef.current) {
        notify.error(error instanceof Error ? error.message : '获取微信二维码失败');
      }
    } finally {
      if (session === qrSessionRef.current) setQrLoading(false);
    }
  }

  function scheduleStatusPoll(bindingId: string, code: string, session: number) {
    clearPollTimer();
    pollTimerRef.current = window.setTimeout(() => {
      void pollQrStatus(bindingId, code, session);
    }, 2000);
  }

  async function pollQrStatus(bindingId: string, code: string, session: number) {
    try {
      const submittedCode = verifyCodeRef.current.trim();
      const verifyParam = submittedCode
        ? `&verify_code=${encodeURIComponent(submittedCode)}`
        : '';
      const result = await api.get<WechatQrcodeStatusResponse>(
        `/api/enterprise/channels/${bindingId}/wechat/qrcode-status?tenant_id=${TENANT_ID}&qrcode=${encodeURIComponent(code)}${verifyParam}`,
      );
      if (session !== qrSessionRef.current) return;
      const status = String(result.status || 'wait');
      if (status === 'confirmed') {
        resetQrFlow();
        notify.success('微信接入成功');
        onChanged();
        return;
      }
      if (status === 'binded_redirect') {
        resetQrFlow();
        notify.success('该微信已接入过，已恢复连接');
        onChanged();
        return;
      }
      if (status === 'expired' || status === 'verify_code_blocked') {
        setQrStatus(status);
        return;
      }
      setQrStatus(status);
      scheduleStatusPoll(bindingId, code, session);
    } catch (error) {
      if (session !== qrSessionRef.current) return;
      clearPollTimer();
      notify.error(error instanceof Error ? error.message : '确认接入状态失败');
    }
  }

  function submitVerifyCode() {
    const code = verifyCode.trim();
    if (!code) return;
    verifyCodeRef.current = code;
  }

  const sessionExpired = Boolean(
    binding.session_expired ?? binding.config_json?.session_expired,
  );
  const trulyExpired = binding.status === 'expired';
  const recovering = sessionExpired && !trulyExpired;
  const showScanButton = !qr && (trulyExpired || binding.status === 'pending');
  const showRescanButton =
    !qr && !recovering && binding.status === 'active' && binding.connected;

  const qrHint =
    qrStatus === 'expired'
      ? '二维码已过期，请重新获取'
      : qrStatus === 'verify_code_blocked'
        ? '多次输入错误，请重新扫码'
        : qrStatus === 'need_verifycode'
          ? '请在手机微信上查看并输入显示的数字'
          : qrStatus === 'scaned' || qrStatus === 'scaned_but_redirect'
            ? '已扫码，请在手机上确认'
            : '请使用微信扫描二维码完成接入';

  return (
    <>
      {recovering && (
        <span className="flex items-center gap-[6px]">
          <StatusBadge tone="orange">恢复中</StatusBadge>
          <span className="text-[12px] text-[#858b9c]">会话恢复中，系统将自动重试</span>
        </span>
      )}
      {trulyExpired && (
        <span className="text-[12px] text-[#d20b0b]">会话已过期，请重新扫码接入。</span>
      )}
      {showScanButton && (
        <div className="flex items-center gap-[8px]">
          <UIButton
            onClick={() => void startQr(binding.id)}
            disabled={qrLoading}
            className={PRIMARY_BUTTON_CLASS}
          >
            {qrLoading ? '正在获取二维码…' : trulyExpired ? '重新扫码' : '扫码接入'}
          </UIButton>
        </div>
      )}
      {showRescanButton && (
        <div className="flex items-center gap-[8px]">
          <UIButton
            variant="outline"
            onClick={() => void startQr(binding.id)}
            disabled={qrLoading}
            className={OUTLINE_BUTTON_CLASS}
          >
            {qrLoading ? '正在获取二维码…' : '重新扫码'}
          </UIButton>
        </div>
      )}
      {qr && (
        <div className="flex flex-col items-center gap-[10px] rounded-[10px] bg-[#fafbfc] p-[16px]">
          <img
            src={qr.imageUrl}
            alt="微信接入二维码"
            className="size-[180px] rounded-[8px] border border-[#eef0f4]"
          />
          <span className="text-[12px] text-[#858b9c]">{qrHint}</span>
          {qrStatus === 'need_verifycode' && (
            <div className="flex items-center gap-[8px]">
              <Input
                value={verifyCode}
                onChange={(event) =>
                  setVerifyCode(event.target.value.replace(/\D/g, '').slice(0, 8))
                }
                placeholder="数字验证码"
                inputMode="numeric"
                className="h-8 w-[140px] rounded-[10px] text-[12px]"
              />
              <UIButton
                onClick={submitVerifyCode}
                disabled={!verifyCode.trim()}
                className={PRIMARY_BUTTON_CLASS}
              >
                确定
              </UIButton>
            </div>
          )}
          {qrStatus === 'expired' || qrStatus === 'verify_code_blocked' ? (
            <UIButton
              onClick={() => void startQr(binding.id)}
              disabled={qrLoading}
              className={PRIMARY_BUTTON_CLASS}
            >
              {qrLoading
                ? '正在获取二维码…'
                : qrStatus === 'expired'
                  ? '刷新二维码'
                  : '重新扫码'}
            </UIButton>
          ) : (
            <UIButton variant="outline" onClick={resetQrFlow} className={OUTLINE_BUTTON_CLASS}>
              取消
            </UIButton>
          )}
          <div className="flex max-w-full flex-col items-center gap-[4px]">
            <span className="text-[11px] text-[#a0a6b8]">扫码失败时，可复制以下内容手动打开</span>
            <code className="max-w-[420px] text-center text-[11px] leading-[1.5] break-all select-all text-[#858b9c]">
              {qr.content}
            </code>
          </div>
        </div>
      )}
    </>
  );
}
