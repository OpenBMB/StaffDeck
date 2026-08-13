import type { ChatSlashCommand } from '@/types';

const PREFIX_PATTERN = /^\/([^\s]*)(?:\s+([^\s]*))?$/i;
const KINDS = new Set<ChatSlashCommand['kind']>(['sop', 'skill', 'tool']);

export type SlashCommandQuery = {
  kind?: ChatSlashCommand['kind'];
  search: string;
};

export type SlashCommandMessage = {
  command: ChatSlashCommand;
  requestText: string;
};

export function slashCommandQuery(input: string): SlashCommandQuery | null {
  const match = PREFIX_PATTERN.exec(input);
  if (!match) return null;
  const prefix = (match[1] || '').trim().toLowerCase();
  const kind = KINDS.has(prefix as ChatSlashCommand['kind'])
    ? prefix as ChatSlashCommand['kind']
    : undefined;
  return {
    kind,
    search: (kind ? match[2] || '' : prefix).trim().toLowerCase(),
  };
}

export function matchingSlashCommands(
  input: string,
  commands: ChatSlashCommand[],
  limit = 10,
): ChatSlashCommand[] {
  const query = slashCommandQuery(input);
  if (!query) return [];
  return commands
    .filter((item) => !query.kind || item.kind === query.kind)
    .filter((item) => {
      if (!query.search) return true;
      return [item.kind, item.target, item.label, item.description]
        .some((value) => value.toLowerCase().includes(query.search));
    })
    .slice(0, Math.max(1, limit));
}

export function selectedSlashCommandText(command: ChatSlashCommand): string {
  return `${command.command} `;
}

export function slashCommandComposerText(
  input: string,
  command: ChatSlashCommand | null,
): string {
  if (!command) return input;
  const prefix = selectedSlashCommandText(command);
  return input.startsWith(prefix) ? input.slice(prefix.length) : input;
}

export function slashCommandInput(command: ChatSlashCommand, composerText: string): string {
  return `${selectedSlashCommandText(command)}${composerText}`;
}

export function slashCommandMessage(
  input: string,
  commands: ChatSlashCommand[],
): SlashCommandMessage | null {
  const match = /^\/(sop|skill|tool)\s+([^\s]+)(?:\s+([\s\S]*))?$/iu.exec(input.trim());
  if (!match) return null;
  const kind = match[1].toLocaleLowerCase() as ChatSlashCommand['kind'];
  const target = match[2];
  const commandText = `/${kind} ${target}`;
  const command = commands.find((item) => (
    item.kind === kind
    && (item.target.toLocaleLowerCase() === target.toLocaleLowerCase()
      || item.command.toLocaleLowerCase() === commandText.toLocaleLowerCase())
  )) || {
    kind,
    target,
    label: target,
    description: '',
    command: commandText,
  };
  return { command, requestText: (match[3] || '').trim() };
}

export function slashMenuScrollTop(
  scrollTop: number,
  viewportHeight: number,
  optionTop: number,
  optionHeight: number,
  padding = 6,
): number {
  const visibleTop = scrollTop + padding;
  const visibleBottom = scrollTop + viewportHeight - padding;
  if (optionTop < visibleTop) return Math.max(0, optionTop - padding);
  const optionBottom = optionTop + optionHeight;
  if (optionBottom > visibleBottom) {
    return Math.max(0, optionBottom - viewportHeight + padding);
  }
  return scrollTop;
}

export function slashCommandKindLabel(kind: ChatSlashCommand['kind']): string {
  if (kind === 'sop') return 'SOP';
  if (kind === 'skill') return '技能';
  return '工具';
}
