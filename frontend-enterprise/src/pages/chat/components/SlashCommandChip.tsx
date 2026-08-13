import StaffdeckIcon from '@/components/StaffdeckIcon';
import type { ChatSlashCommand } from '@/types';

import {
  CHAT_COMPOSER_COMMAND_CHIP_CLASS,
  CHAT_COMPOSER_COMMAND_ICON_CLASS,
  CHAT_COMPOSER_COMMAND_KIND_CLASS,
  CHAT_COMPOSER_COMMAND_LABEL_CLASS,
  CHAT_COMPOSER_COMMAND_REMOVE_CLASS,
} from '../chatPageStyles';
import { slashCommandKindLabel } from '../slashCommands';

type SlashCommandChipProps = {
  command: ChatSlashCommand;
  onRemove?: () => void;
  removeLabel?: string;
};

export default function SlashCommandChip({
  command,
  onRemove,
  removeLabel = `移除命令 ${command.label}`,
}: SlashCommandChipProps) {
  return (
    <span
      className={CHAT_COMPOSER_COMMAND_CHIP_CLASS}
      role="group"
      aria-label={`${slashCommandKindLabel(command.kind)} ${command.label}`}
      title={command.command}
      data-chat-slash-command={command.command}
    >
      <span className={CHAT_COMPOSER_COMMAND_ICON_CLASS} aria-hidden="true">
        <StaffdeckIcon
          name={command.kind === 'sop' ? 'branch' : command.kind === 'skill' ? 'spark' : 'tool'}
          size={13}
        />
      </span>
      <span className={CHAT_COMPOSER_COMMAND_LABEL_CLASS}>{command.label}</span>
      <span className={CHAT_COMPOSER_COMMAND_KIND_CLASS}>{slashCommandKindLabel(command.kind)}</span>
      {onRemove && (
        <button
          type="button"
          className={CHAT_COMPOSER_COMMAND_REMOVE_CLASS}
          onClick={onRemove}
          aria-label={removeLabel}
          title={removeLabel}
        >
          ×
        </button>
      )}
    </span>
  );
}
