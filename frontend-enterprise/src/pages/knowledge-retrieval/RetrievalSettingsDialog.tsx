import type { ReactNode } from 'react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
} from '@/components/ui';
import { Button } from '@/components/ui/button';

export type RetrievalSettingsDialogProps = {
  title: string;
  description?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  onTest?: () => void;
  onApply: () => void;
  onCancel?: () => void;
  testLabel?: string;
  applyLabel?: string;
};

export function RetrievalSettingsDialog({
  title,
  description,
  open,
  onOpenChange,
  children,
  onTest,
  onApply,
  onCancel,
  testLabel = '测试连接',
  applyLabel = '应用到草稿',
}: RetrievalSettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-[760px] overflow-y-auto p-0">
        <div className="border-b border-[#eceef1] px-[24px] py-[18px]">
          <DialogTitle className="text-[16px] text-[#18181a]">{title}</DialogTitle>
          {description ? <DialogDescription className="mt-[8px] text-[12px] leading-[1.6]">{description}</DialogDescription> : null}
        </div>
        <div className="px-[24px] py-[18px]">{children}</div>
        <DialogFooter className="border-t border-[#eceef1]">
          {onTest ? <Button variant="outline" onClick={onTest}>{testLabel}</Button> : null}
          <Button variant="outline" onClick={() => { onCancel?.(); onOpenChange(false); }}>取消</Button>
          <Button onClick={() => { onApply(); onOpenChange(false); }}>{applyLabel}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
export function FormField({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="flex flex-col gap-[6px] text-[12px] text-[#5b6273]">
      <span>{label}</span>
      {children}
      {hint ? <span className="text-[11px] leading-[1.5] text-[#858b9c]">{hint}</span> : null}
    </label>
  );
}

export const fieldClassName = 'h-[34px] rounded-[8px] border border-[#e3e7f1] bg-white px-[10px] text-[12px] text-[#18181a] outline-none focus:border-[#1677ff]';
