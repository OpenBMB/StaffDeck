import { useEffect, useState } from 'react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  notify,
} from '@/components/ui';
import { Building2 } from 'lucide-react';

import { api } from '../api/client';

export type EditDepartmentDialogProps = {
  open: boolean;
  /** 当前部门名,用于打开时回填输入框(可能为空)。 */
  currentDepartment: string;
  onClose: () => void;
  /** 保存成功后回调,参数为服务端落库后的部门名(可能为空)。 */
  onSaved: (department: string) => void;
};

export default function EditDepartmentDialog({
  open,
  currentDepartment,
  onClose,
  onSaved,
}: EditDepartmentDialogProps) {
  const [value, setValue] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // 每次打开时回填当前部门名并清空校验状态
  useEffect(() => {
    if (open) {
      setValue(currentDepartment);
      setError('');
    }
  }, [open, currentDepartment]);

  function handleOpenChange(next: boolean) {
    if (!next && !loading) {
      onClose();
    }
  }

  function validate(): boolean {
    if (value.trim().length > 80) {
      setError('部门名不能超过 80 个字符');
      return false;
    }
    setError('');
    return true;
  }

  async function submit(event?: React.FormEvent) {
    event?.preventDefault();
    if (loading || !validate()) return;
    setLoading(true);
    try {
      const fresh = await api.put<{ department?: string | null }>(
        '/api/auth/me/profile',
        { department: value.trim() },
      );
      notify.success('部门名已更新');
      onSaved(fresh.department || '');
      onClose();
    } catch (err) {
      notify.error(err instanceof Error ? err.message : '修改部门名失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        aria-describedby="edit-department-description"
        className="w-[calc(100%-2rem)] max-w-[420px] rounded-[18px] border-0 bg-white p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[24px] py-[20px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[36px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Building2 className="size-[16px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">修改部门名</DialogTitle>
              <DialogDescription id="edit-department-description" className="mt-[4px] text-[12px] text-[#757f9c]">
                部门会展示给同租户的其他成员，修改后立即生效。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-[18px] px-[24px] py-[22px]">
          <div className="space-y-2">
            <Label htmlFor="department" className="text-[13px] text-[#18181a]">
              部门名
            </Label>
            <Input
              id="department"
              name="department"
              value={value}
              placeholder="请输入部门名"
              maxLength={80}
              autoFocus
              aria-invalid={error ? 'true' : 'false'}
              onChange={(event) => {
                setValue(event.target.value);
                if (error) setError('');
              }}
              className="h-[40px] rounded-[12px] border-[#e4e8ef] bg-white text-[14px] text-[#18181a] placeholder:text-[#b0b8c8]"
            />
            {error && <p className="text-[12px] text-[#d20b0b]">{error}</p>}
          </div>

          <div className="flex justify-end gap-[10px] pt-[6px]">
            <Button
              type="button"
              variant="outline"
              disabled={loading}
              onClick={() => handleOpenChange(false)}
              className="h-[36px] rounded-[10px] border-[#e2e6ed] px-[16px] text-[13px] text-[#5e687c] hover:bg-[#f4f6f9]"
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={loading}
              className="h-[36px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#2d2d33]"
            >
              {loading ? (
                <span className="mr-2 inline-block size-[14px] animate-spin rounded-full border-2 border-white/40 border-t-white" />
              ) : null}
              保存
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
