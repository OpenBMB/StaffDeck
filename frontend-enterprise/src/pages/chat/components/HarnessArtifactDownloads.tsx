import { useState } from 'react';

import StaffdeckIcon from '@/components/StaffdeckIcon';
import { notify } from '@/components/ui/app-toast';
import { api } from '@/api/client';
import type { HarnessWorkspaceArtifact } from '@/types';

import {
  CHAT_ARTIFACTS_CLASS,
  CHAT_ARTIFACT_BUTTON_CLASS,
  CHAT_ARTIFACT_COPY_CLASS,
  CHAT_ARTIFACT_HEADING_CLASS,
  CHAT_ARTIFACT_ICON_CLASS,
  CHAT_ARTIFACT_LIST_CLASS,
  CHAT_ARTIFACT_META_CLASS,
  CHAT_ARTIFACT_NAME_CLASS,
} from '../chatPageStyles';

type HarnessArtifactDownloadsProps = {
  artifacts: HarnessWorkspaceArtifact[];
  tenantId: string;
  sessionId: string;
};

export default function HarnessArtifactDownloads({
  artifacts,
  tenantId,
  sessionId,
}: HarnessArtifactDownloadsProps) {
  const [downloading, setDownloading] = useState('');

  if (artifacts.length === 0) return null;

  async function downloadArtifact(artifact: HarnessWorkspaceArtifact) {
    const identity = `${artifact.task_frame_id}\u001f${artifact.path}`;
    const filename = artifactFilename(artifact.path);
    setDownloading(identity);
    try {
      const query = new URLSearchParams({
        tenant_id: tenantId,
        path: artifact.path,
      });
      const blob = await api.blob(
        `/api/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/`
          + `${encodeURIComponent(artifact.task_frame_id)}?${query.toString()}`,
      );
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(objectUrl);
      notify.success(`已下载文件：${filename}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '文件下载失败');
    } finally {
      setDownloading('');
    }
  }

  return (
    <div className={CHAT_ARTIFACTS_CLASS} aria-label="生成文件">
      <div className={CHAT_ARTIFACT_HEADING_CLASS}>
        <StaffdeckIcon name="folder" size={14} />
        <span>生成文件</span>
      </div>
      <div className={CHAT_ARTIFACT_LIST_CLASS}>
        {artifacts.map((artifact) => {
          const identity = `${artifact.task_frame_id}\u001f${artifact.path}`;
          const filename = artifactFilename(artifact.path);
          const isDownloading = downloading === identity;
          return (
            <button
              type="button"
              className={CHAT_ARTIFACT_BUTTON_CLASS}
              key={identity}
              disabled={isDownloading || !sessionId || !tenantId}
              aria-label={`下载文件 ${filename}`}
              aria-busy={isDownloading}
              onClick={() => void downloadArtifact(artifact)}
            >
              <span className={CHAT_ARTIFACT_ICON_CLASS}>
                <StaffdeckIcon name="file" size={17} />
              </span>
              <span className={CHAT_ARTIFACT_COPY_CLASS}>
                <span className={CHAT_ARTIFACT_NAME_CLASS} data-i18n-ignore>
                  {filename}
                </span>
                <span className={CHAT_ARTIFACT_META_CLASS}>
                  {isDownloading ? '下载中' : artifactMeta(artifact)}
                </span>
              </span>
              <StaffdeckIcon name="download" size={16} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

function artifactFilename(path: string): string {
  const filename = path.replace(/\\/g, '/').split('/').pop()?.trim() || '';
  return filename.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 180) || 'artifact';
}

function artifactMeta(artifact: HarnessWorkspaceArtifact): string {
  const size = formatArtifactSize(artifact.size);
  return size ? `Harness 文件 · ${size}` : 'Harness 文件';
}

function formatArtifactSize(size: number | null | undefined): string {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
