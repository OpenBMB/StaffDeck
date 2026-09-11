import type { ChannelConversationMessageRead } from '../../types';

function formatFileSize(size: number | undefined): string {
  if (size === undefined || size === null || Number.isNaN(size)) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function attachmentKindLabel(kind: string | undefined, contentType: string | undefined): string {
  const ct = contentType || '';
  if ((kind || '').toLowerCase() === 'image' || ct.startsWith('image/')) return '图片';
  if ((kind || '').toLowerCase() === 'pdf' || ct === 'application/pdf' || ct.includes('pdf')) {
    return 'PDF';
  }
  return '文件';
}

export default function ChannelMessageAttachments({
  message,
}: {
  message: ChannelConversationMessageRead;
}) {
  const attachments = message.attachments || [];
  if (attachments.length === 0) return null;

  return (
    <div className="mt-[10px] grid gap-[8px]">
      {attachments.map((attachment, index) => {
        const kindLabel = attachmentKindLabel(attachment.kind, attachment.content_type);
        const sizeLabel = formatFileSize(attachment.size);
        return (
          <div
            key={attachment.id || `${message.id}-${index}`}
            className="grid min-h-[46px] w-[min(280px,100%)] grid-cols-[36px_minmax(0,1fr)] items-center gap-[10px] rounded-[10px] border border-[#e3e7f1] bg-[#fafbfc] p-[7px]"
          >
            <span className="inline-grid size-[36px] place-items-center rounded-[8px] bg-[#eef0f4] text-[11px] text-[#464c5e]">
              {kindLabel}
            </span>
            <span className="grid min-w-0 gap-px">
              <span className="truncate text-[12px] font-medium text-[#18181a]">
                {attachment.filename || kindLabel}
              </span>
              <span className="truncate text-[11px] text-[#858b9c]">
                {kindLabel}
                {sizeLabel ? ` · ${sizeLabel}` : ''}
                {/* 演进点：后端消息查询端点暂不返回附件访问 URL，这里仅展示文件名与大小；
                    待后端在 ChannelConversationAttachmentRead 中补充 url 字段后，
                    image 类型可改为 <img> 缩略图预览（参照 chat 会话 MessageBubble 的附件卡片）。 */}
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}
