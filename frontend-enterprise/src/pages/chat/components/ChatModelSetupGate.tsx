import { useEffect, useState } from 'react';
import { AlertCircle } from 'lucide-react';

import { api } from '@/api/client';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui';
import { notify } from '@/components/ui/app-toast';
import { Button } from '@/components/ui/button';
import { useI18n } from '@/i18n';
import { apiErrorMessage } from '@/lib/apiErrorMessages';
import type { ModelConfigRead } from '@/types';
import type { ApiKeyProtocol } from '@/pages/models/channelPresets';
import ModelSetupWizard from '@/pages/models/ModelSetupWizard';
import { useCodexSubscriptionAccount } from '@/pages/models/useCodexSubscriptionAccount';

export type ChatModelSetupGateProps = {
  open: boolean;
  tenantId: string;
  canConfigure: boolean;
  onOpenChange: (open: boolean) => void;
  onConfigured: (model: ModelConfigRead) => void;
};

/** 在聊天无可用模型时，为管理员展示共享向导，为其他用户保留只读权限提示。 */
export default function ChatModelSetupGate({
  open,
  tenantId,
  canConfigure,
  onOpenChange,
  onConfigured,
}: ChatModelSetupGateProps) {
  const { t } = useI18n();
  const [availableProtocols, setAvailableProtocols] = useState<ApiKeyProtocol[]>(['openai_chat_completions']);
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  const {
    account: subscriptionAccount,
    loading: subscriptionLoading,
    startLogin,
    cancelLogin,
    logout,
  } = useCodexSubscriptionAccount({ tenantId, enabled: open && canConfigure });

  useEffect(() => {
    if (!open || !canConfigure) return;
    let cancelled = false;
    void api
      .get<{ protocols: ApiKeyProtocol[] }>(
        `/api/enterprise/model-configs/protocols?tenant_id=${encodeURIComponent(tenantId)}`,
      )
      .then((result) => {
        if (!cancelled) setAvailableProtocols(result.protocols);
      })
      .catch((error) => {
        if (!cancelled) notify.error(apiErrorMessage(error, t('加载 API 协议失败')));
      });
    return () => {
      cancelled = true;
    };
  }, [canConfigure, open, t, tenantId]);

  if (!canConfigure) {
    return (
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{t('需要先配置模型')}</DialogTitle>
            <DialogDescription>
              {t('当前没有可用模型。完成配置并通过连通性测试后，才能发送对话和执行任务。')}
            </DialogDescription>
          </DialogHeader>
          <div className="flex items-start gap-[10px] rounded-[8px] border border-[#f0d9a8] bg-[#fffaf0] p-[12px] text-[13px] text-[#7b5c16]">
            <AlertCircle className="mt-[1px] size-[16px] shrink-0" />
            <span>{t('当前账号没有模型管理权限，请联系管理员完成模型配置和连通性测试。')}</span>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t('稍后配置')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <>
      <ModelSetupWizard
        open={open}
        tenantId={tenantId}
        onOpenChange={onOpenChange}
        onCreated={(model, options) => {
          if (options?.tested) onConfigured(model);
        }}
        availableProtocols={availableProtocols}
        subscriptionAccount={subscriptionAccount}
        subscriptionLoading={subscriptionLoading}
        onStartSubscriptionLogin={() => void startLogin()}
        onCancelSubscriptionLogin={() => void cancelLogin()}
        onRequestSubscriptionLogout={() => setLogoutConfirmOpen(true)}
        requireVerified
      />

      <ConfirmDialog
        open={logoutConfirmOpen}
        onOpenChange={setLogoutConfirmOpen}
        loading={subscriptionLoading}
        destructive={false}
        title={t('退出本机 Codex？')}
        description={t('这会让本机 Codex 退出 ChatGPT。所有采用“ChatGPT 订阅（Codex）”的模型都会失去授权；同一台电脑上使用该 Codex 登录的其他应用也可能受影响。API Key 模型不受影响。')}
        confirmText={t('退出本机 Codex')}
        onConfirm={() => {
          setLogoutConfirmOpen(false);
          void logout();
        }}
      />
    </>
  );
}
