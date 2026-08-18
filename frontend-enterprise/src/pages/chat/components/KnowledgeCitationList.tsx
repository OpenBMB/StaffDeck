import StaffdeckIcon from '@/components/StaffdeckIcon';
import type { KnowledgeCitation } from '@/types';

import {
  CHAT_CITATION_CHIP_CLASS,
  CHAT_CITATION_HEADING_CLASS,
  CHAT_CITATION_INDEX_CLASS,
  CHAT_CITATION_LIST_CLASS,
  CHAT_CITATION_TITLE_CLASS,
  CHAT_CITATIONS_CLASS,
} from '../chatPageStyles';
import { citationDisplayTitle } from '../chatHelpers';

type KnowledgeCitationListProps = {
  citations: KnowledgeCitation[];
  onOpen: (citation: KnowledgeCitation) => void;
};

export default function KnowledgeCitationList({
  citations,
  onOpen,
}: KnowledgeCitationListProps) {
  if (citations.length === 0) return null;

  return (
    <div className={CHAT_CITATIONS_CLASS} aria-label="知识引用">
      <div className={CHAT_CITATION_HEADING_CLASS}>
        <StaffdeckIcon name="file" size={14} />
        <span>知识来源</span>
      </div>
      <div className={CHAT_CITATION_LIST_CLASS}>
        {citations.map((citation) => (
          <button
            key={citation.id}
            type="button"
            className={CHAT_CITATION_CHIP_CLASS}
            onClick={() => onOpen(citation)}
          >
            <span className={CHAT_CITATION_INDEX_CLASS} data-i18n-ignore>
              {citation.label || citation.id}
            </span>
            <span className={CHAT_CITATION_TITLE_CLASS} data-i18n-ignore>
              {citationDisplayTitle(citation)}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
