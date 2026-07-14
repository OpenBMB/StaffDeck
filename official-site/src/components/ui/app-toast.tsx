import type { ReactNode } from 'react';
import { toast, type ExternalToast } from 'sonner';

import { cn } from '@/lib/utils';

import IconError from '@/assets/icons/error-fill.svg?react';
import IconSuccess from '@/assets/icons/success-fill.svg?react';

type ToastVariant = 'success' | 'error';

// Colors, radius and spacing mirror SD1 "Basic components/Dialog/Message"
// (success node 281:3334, error node 281:3342).
const VARIANTS: Record<
  ToastVariant,
  { container: string; icon: string; Icon: typeof IconSuccess }
> = {
  success: {
    container: 'border-[#96d9b0] bg-[#e9f7ef] text-[#018434]',
    icon: 'text-[#2cb360]',
    Icon: IconSuccess,
  },
  error: {
    container: 'border-[#f38989] bg-[#fce7e7] text-[#d20b0b]',
    icon: 'text-[#d20b0b]',
    Icon: IconError,
  },
};

function ToastPill({ variant, message }: { variant: ToastVariant; message: ReactNode }) {
  const { container, icon, Icon } = VARIANTS[variant];
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'pointer-events-auto flex max-w-full items-center gap-[12px] rounded-[14px] border border-solid px-[24px] py-[10px] shadow-[0px_12px_32px_rgba(0,0,0,0.12)]',
        container,
      )}
    >
      <Icon className={cn('size-[16px] shrink-0', icon)} aria-hidden="true" />
      <span className="text-[14px] leading-[normal] wrap-anywhere">{message}</span>
    </div>
  );
}

/**
 * Options accepted by the branded toasts. Presentation (icon, styling and
 * centered placement) is owned by the component, so those keys are excluded.
 */
export type AppToastOptions = Omit<
  ExternalToast,
  'icon' | 'className' | 'style' | 'unstyled' | 'descriptionClassName'
>;

function showVariant(variant: ToastVariant, message: ReactNode, options?: AppToastOptions) {
  return toast.custom(() => <ToastPill variant={variant} message={message} />, {
    duration: variant === 'success' ? 3200 : 4800,
    unstyled: true,
    className: 'flex w-full justify-center',
    ...options,
  });
}

/**
 * Global toast helper. `success` / `error` render the SD1 message pill;
 * `warning` / `info` / `loading` delegate to sonner so they share the same
 * centered placement configured on the app-wide <Toaster />.
 */
export const notify = {
  success: (message: ReactNode, options?: AppToastOptions) =>
    showVariant('success', message, options),
  error: (message: ReactNode, options?: AppToastOptions) => showVariant('error', message, options),
  warning: (message: ReactNode, options?: AppToastOptions) => toast.warning(message, options),
  info: (message: ReactNode, options?: AppToastOptions) => toast.info(message, options),
  loading: (message: ReactNode, options?: AppToastOptions) => toast.loading(message, options),
  dismiss: (id?: string | number) => toast.dismiss(id),
};
