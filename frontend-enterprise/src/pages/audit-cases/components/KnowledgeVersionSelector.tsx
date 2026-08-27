import type { AuditCaseManagementOptions } from '@/types';

export function KnowledgeVersionSelector({
  options,
  selected,
  onChange,
}: {
  options: AuditCaseManagementOptions;
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const toggle = (id: string) => {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  };

  return (
    <fieldset className="grid gap-[8px]">
      <legend className="text-[12px] font-medium text-[#464c5e]">关联知识库版本</legend>
      {options.knowledge_versions.length === 0 ? (
        <p className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px] text-[12px] text-[#858b9c]">暂无可用知识库版本，可稍后在项目详情中补充。</p>
      ) : options.knowledge_versions.map((version) => (
        <label key={version.id} className="flex cursor-pointer items-start gap-[9px] rounded-[9px] border border-[#edf0f5] px-[10px] py-[9px] hover:bg-[#fafbfc]">
          <input
            type="checkbox"
            aria-label={`${version.name} ${version.version}`}
            checked={selected.includes(version.id)}
            onChange={() => toggle(version.id)}
            className="mt-[2px]"
          />
          <span className="min-w-0">
            <span className="block text-[12px] font-medium text-[#464c5e]">{version.name} · {version.version}</span>
            <span className="block text-[11px] text-[#a0a6b5]">{version.status}{version.description ? ` · ${version.description}` : ''}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}
