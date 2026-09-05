import { useMemo, useState } from 'react';

import { Input } from '@/components/ui';
import type { AuditCaseKnowledgeVersionOption, AuditCaseManagementOptions } from '@/types';

function versionLabel(version: AuditCaseKnowledgeVersionOption): string {
  if (version.is_agent_branch) return '数字员工工作版本';
  return version.version;
}

function groupKey(version: AuditCaseKnowledgeVersionOption): string {
  return version.duplicate_group ? `${version.duplicate_group}:${version.knowledge_base_id}` : version.knowledge_base_id;
}

export function KnowledgeVersionSelector({
  options,
  selected,
  onChange,
}: {
  options: AuditCaseManagementOptions;
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  const [query, setQuery] = useState('');
  const [selectedOnly, setSelectedOnly] = useState(false);
  const toggle = (id: string) => {
    onChange(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  };
  const groups = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const visible = options.knowledge_versions.filter((version) => {
      if (selectedOnly && !selected.includes(version.id)) return false;
      if (!normalizedQuery) return true;
      return [version.name, version.version, version.description || '']
        .join(' ')
        .toLocaleLowerCase()
        .includes(normalizedQuery);
    });
    const grouped = new Map<string, AuditCaseKnowledgeVersionOption[]>();
    visible.forEach((version) => {
      const key = groupKey(version);
      grouped.set(key, [...(grouped.get(key) || []), version]);
    });
    return [...grouped.values()];
  }, [options.knowledge_versions, query, selected, selectedOnly]);

  return (
    <fieldset className="grid gap-[8px]">
      <div className="flex flex-wrap items-center justify-between gap-[8px]">
        <legend className="text-[12px] font-medium text-[#464c5e]">关联知识库版本</legend>
        <span className="text-[11px] text-[#a0a6b5]">已选择 {selected.length} 个版本</span>
      </div>
      {options.knowledge_versions.length > 0 && (
        <div className="flex flex-wrap items-center gap-[8px]">
          <Input
            type="search"
            aria-label="搜索知识库"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索知识库名称或版本"
            className="h-[32px] min-w-[200px] flex-1 rounded-[8px] text-[12px]"
          />
          <label className="flex shrink-0 items-center gap-[5px] text-[11px] text-[#858b9c]">
            <input
              type="checkbox"
              aria-label="只看已选"
              checked={selectedOnly}
              onChange={(event) => setSelectedOnly(event.target.checked)}
            />
            只看已选
          </label>
        </div>
      )}
      {options.knowledge_versions.length === 0 ? (
        <p className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px] text-[12px] text-[#858b9c]">暂无可用知识库版本，可稍后在项目详情中补充。</p>
      ) : groups.length === 0 ? (
        <p className="rounded-[9px] bg-[#f7f8fb] px-[12px] py-[10px] text-[12px] text-[#858b9c]">没有匹配的知识库版本。</p>
      ) : (
        <div className="grid max-h-[360px] gap-[8px] overflow-y-auto pr-[2px]">
          {groups.map((versions) => {
            const first = versions[0];
            const duplicate = Boolean(first.duplicate_group);
            return (
              <section key={groupKey(first)} className="grid gap-[5px] rounded-[10px] border border-[#edf0f5] p-[8px]">
                <div className="flex items-center justify-between gap-[8px] px-[3px]">
                  <span className="min-w-0 truncate text-[12px] font-medium text-[#464c5e]">{first.name}</span>
                  {duplicate && <span className="shrink-0 text-[10px] text-[#a15c00]">疑似重复知识库</span>}
                </div>
                {versions.map((version) => (
                  <label key={version.id} className="flex cursor-pointer items-start gap-[8px] rounded-[8px] px-[6px] py-[7px] hover:bg-[#fafbfc]">
                    <input
                      type="checkbox"
                      aria-label={`${version.name} ${version.version}`}
                      checked={selected.includes(version.id)}
                      onChange={() => toggle(version.id)}
                      className="mt-[2px]"
                    />
                    <span className="min-w-0">
                      <span className="flex flex-wrap items-center gap-[5px] text-[12px] font-medium text-[#464c5e]">
                        <span>{version.name} · {versionLabel(version)}</span>
                        {version.recommended && <span className="rounded-full bg-[#eef4ff] px-[5px] py-[2px] text-[10px] font-normal text-[#4d8cff]">推荐版本</span>}
                      </span>
                      <span className="block text-[11px] text-[#a0a6b5]">{version.status}{version.is_agent_branch ? ' · 数字员工工作版本' : ''}{version.description ? ` · ${version.description}` : ''}</span>
                    </span>
                  </label>
                ))}
              </section>
            );
          })}
        </div>
      )}
    </fieldset>
  );
}
