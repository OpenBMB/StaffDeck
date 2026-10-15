import type { KnowledgeClient } from '../client';

export type Props = { client: KnowledgeClient; suffix: string; scope: Record<string, string>; writable: boolean; report: (error: unknown) => void; revision?: number; onChange?: () => Promise<void> };

export function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
