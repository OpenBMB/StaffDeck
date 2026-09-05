import { useEffect, useMemo, useState } from 'react';

import { loadAuditCaseMembers } from '../auditCaseApi';
import type { AuditCaseMemberOption } from '../auditCaseTypes';
import { auditCaseErrorMessage } from '../auditCaseErrors';

export function MemberSelector({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const [accounts, setAccounts] = useState<AuditCaseMemberOption[]>([]);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    void loadAuditCaseMembers()
      .then((rows) => {
        if (active) setAccounts(rows.filter((row) => row.source === undefined || row.source === 'web'));
      })
      .catch((reason) => {
        if (active) setError(auditCaseErrorMessage(reason, '加载成员账号失败'));
      });
    return () => { active = false; };
  }, []);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const toggle = (id: string) => {
    onChange(selectedSet.has(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  };

  return (
    <fieldset className="grid gap-[8px]">
      <legend className="text-[12px] font-medium text-[#464c5e]">项目成员</legend>
      {error && <p className="text-[12px] text-[#d20b0b]">{error}</p>}
      {!error && accounts.length === 0 && <p className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px] text-[12px] text-[#858b9c]">暂无可选的内部账号。</p>}
      {accounts.map((account) => (
        <label key={account.id} className="flex cursor-pointer items-center gap-[9px] rounded-[9px] border border-[#edf0f5] px-[10px] py-[9px] hover:bg-[#fafbfc]">
          <input
            type="checkbox"
            aria-label={account.display_name || account.username}
            checked={selectedSet.has(account.id)}
            onChange={() => toggle(account.id)}
            className="mt-[1px]"
          />
          <span className="min-w-0 truncate text-[12px] text-[#464c5e]">{account.display_name || account.username}</span>
          <span className="ml-auto text-[11px] text-[#a0a6b5]">{account.username}</span>
        </label>
      ))}
    </fieldset>
  );
}
