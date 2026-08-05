import { useEffect } from 'react';
import { ArrowUpRight, CircleArrowUp, X } from 'lucide-react';
import { toast } from 'sonner';

import { api } from '@/api/client';
import { useI18n } from '@/i18n';

export const REMINDED_VERSION_KEY = 'staffdeck_update_reminded_version';

type AppVersion = {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  release_url: string;
  check_enabled: boolean;
  check_succeeded: boolean;
};

export function UpdateNotice({
  currentVersion,
  latestVersion,
  releaseUrl,
  onClose,
}: {
  currentVersion: string;
  latestVersion: string;
  releaseUrl: string;
  onClose: () => void;
}) {
  const { t } = useI18n();

  return (
    <div
      role="status"
      aria-live="polite"
      className="pointer-events-auto relative flex w-[460px] max-w-[calc(100vw-32px)] items-center gap-[12px] rounded-[8px] border border-[#dce2ed] bg-white px-[14px] py-[12px] pr-[42px] shadow-[0_12px_32px_rgba(36,46,82,0.16)]"
    >
      <span className="grid size-[34px] shrink-0 place-items-center rounded-[8px] bg-[#eef4ff] text-[#2864d7]">
        <CircleArrowUp className="size-[17px]" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium leading-[20px] text-[#18181a]">
          {t('StaffDeck 有新版本')}
        </p>
        <p className="mt-[1px] text-[12px] leading-[18px] text-[#757f9c]">
          {t('v{1} 已发布，你正在使用 v{2}', { 1: latestVersion, 2: currentVersion })}
        </p>
      </div>
      <a
        href={releaseUrl}
        target="_blank"
        rel="noreferrer"
        onClick={onClose}
        className="flex h-[32px] shrink-0 items-center gap-[4px] rounded-[7px] bg-[#18181a] px-[10px] text-[12px] font-medium text-white transition-opacity hover:opacity-80"
      >
        {t('查看更新')}
        <ArrowUpRight className="size-[13px]" aria-hidden="true" />
      </a>
      <button
        type="button"
        aria-label={t('关闭更新提醒')}
        onClick={onClose}
        className="absolute right-[8px] top-[7px] grid size-[24px] place-items-center text-[#9aa3ba] transition-colors hover:text-[#464c5e]"
      >
        <X className="size-[14px]" aria-hidden="true" />
      </button>
    </div>
  );
}

export default function UpdateReminder({ enabled }: { enabled: boolean }) {
  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;

    void api.get<AppVersion>('/api/app/version').then((result) => {
      if (
        cancelled
        || !result.check_enabled
        || !result.check_succeeded
        || !result.update_available
        || !result.latest_version
      ) return;
      if (window.localStorage.getItem(REMINDED_VERSION_KEY) === result.latest_version) return;

      const toastId = toast.custom((id) => (
        <UpdateNotice
          currentVersion={result.current_version}
          latestVersion={result.latest_version!}
          releaseUrl={result.release_url}
          onClose={() => toast.dismiss(id)}
        />
      ), {
        duration: 30_000,
        closeButton: false,
        unstyled: true,
      });
      if (toastId !== undefined) {
        window.localStorage.setItem(REMINDED_VERSION_KEY, result.latest_version);
      }
    }).catch(() => {
      // Update checks are best-effort and must never interrupt normal use.
    });

    return () => {
      cancelled = true;
    };
  }, [enabled]);

  return null;
}
