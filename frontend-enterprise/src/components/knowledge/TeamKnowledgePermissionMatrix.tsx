import { useEffect, useState } from 'react';

import { Badge, Button } from '@/components/ui';
import type {
  KnowledgePermission,
  TeamKnowledgeBindingRead,
  TeamKnowledgeGrantInput,
  TeamMemberRead,
} from '@/types';

type PermissionChoice = KnowledgePermission | 'none';

type TeamKnowledgePermissionMatrixProps = {
  binding: TeamKnowledgeBindingRead;
  members: TeamMemberRead[];
  busy?: boolean;
  onSave: (
    binding: TeamKnowledgeBindingRead,
    grants: TeamKnowledgeGrantInput[],
  ) => Promise<void>;
  onSetDefault: (binding: TeamKnowledgeBindingRead) => Promise<void>;
  onRemove: (binding: TeamKnowledgeBindingRead) => Promise<void>;
};

/** Render and edit one team-by-knowledge-base permission matrix. */
export default function TeamKnowledgePermissionMatrix({
  binding,
  members,
  busy = false,
  onSave,
  onSetDefault,
  onRemove,
}: TeamKnowledgePermissionMatrixProps) {
  const [permissions, setPermissions] = useState<Record<string, PermissionChoice>>({});

  useEffect(() => {
    /** Rebuild visible permissions whenever the server revision or roster changes. */
    const activeByAgent = new Map(
      binding.grants
        .filter((grant) => grant.status === 'active')
        .map((grant) => [grant.agent_id, grant.permission] as const),
    );
    setPermissions(Object.fromEntries(
      members.map((member) => [member.agent_id, activeByAgent.get(member.agent_id) || 'none']),
    ));
  }, [binding.grants, binding.revision, members]);

  /** Convert the complete visible matrix into the atomic API payload. */
  function grantPayload(): TeamKnowledgeGrantInput[] {
    return members.map((member) => ({
      agent_id: member.agent_id,
      permission: permissions[member.agent_id] === 'none'
        ? null
        : permissions[member.agent_id] as KnowledgePermission,
    }));
  }

  return (
    <article className="rounded-[14px] border border-[#e7eaf1] bg-[#fafbfd] p-[14px]">
      <div className="flex flex-wrap items-start justify-between gap-[10px]">
        <div>
          <div className="flex flex-wrap items-center gap-[8px]">
            <h3 className="text-[14px] font-medium text-[#18181a]">{binding.knowledge_base_name}</h3>
            <Badge className="rounded-full bg-[#e8f0ff] text-[11px] font-normal text-[#1a71ff]">
              共享知识库
            </Badge>
            {binding.is_default && (
              <Badge className="rounded-full bg-[#eaf7ef] text-[11px] font-normal text-[#287a4d]">
                默认写入
              </Badge>
            )}
          </div>
          <p className="mt-[4px] text-[12px] text-[#858b9c]">
            {`正式版本 ${binding.published_version || '--'} · 配置修订 ${binding.revision}`}
          </p>
        </div>
        <div className="flex flex-wrap gap-[6px]">
          {!binding.is_default && (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              aria-label={`设为默认 ${binding.knowledge_base_name}`}
              onClick={() => void onSetDefault(binding)}
              className="h-[30px] rounded-[9px] px-[10px] text-[12px]"
            >
              设为默认
            </Button>
          )}
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            aria-label={`移除共享知识库 ${binding.knowledge_base_name}`}
            onClick={() => void onRemove(binding)}
            className="h-[30px] rounded-[9px] px-[10px] text-[12px] text-[#c0342b]"
          >
            移除
          </Button>
        </div>
      </div>

      <div className="mt-[12px] grid gap-[8px] sm:grid-cols-2 lg:grid-cols-3">
        {members.map((member) => (
          <label
            key={member.agent_id}
            className="flex items-center justify-between gap-[8px] rounded-[10px] bg-white px-[10px] py-[8px] text-[12px] text-[#464c5e]"
          >
            <span className="min-w-0 truncate">{member.agent_name || member.agent_id}</span>
            <select
              aria-label={`${member.agent_name || member.agent_id} 在 ${binding.knowledge_base_name} 的权限`}
              value={permissions[member.agent_id] || 'none'}
              disabled={busy}
              onChange={(event) => setPermissions((current) => ({
                ...current,
                [member.agent_id]: event.target.value as PermissionChoice,
              }))}
              className="h-[30px] rounded-[8px] border border-[#dfe4ed] bg-white px-[8px] text-[12px] text-[#18181a]"
            >
              <option value="none">无权限</option>
              <option value="reader">可读取</option>
              <option value="editor">可编辑</option>
              <option value="publisher">可发布</option>
            </select>
          </label>
        ))}
      </div>

      <div className="mt-[10px] flex justify-end">
        <Button
          type="button"
          disabled={busy}
          aria-label={`保存 ${binding.knowledge_base_name} 权限`}
          onClick={() => void onSave(binding, grantPayload())}
          className="h-[30px] rounded-[9px] bg-[#18181a] px-[12px] text-[12px] text-white"
        >
          {busy ? '保存中…' : '保存权限'}
        </Button>
      </div>
    </article>
  );
}
