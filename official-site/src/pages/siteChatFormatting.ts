export type SiteChatSource = {
  index: number;
  id: string;
  title: string;
};

export type NormalizedSiteChatCitations = {
  content: string;
  sources: SiteChatSource[];
};

function sourceIdentity(source: SiteChatSource): string {
  return source.id.trim() || source.title.trim() || String(source.index);
}

function uniqueSources(sources: SiteChatSource[]): SiteChatSource[] {
  const seen = new Set<string>();
  return sources.filter((source) => {
    const identity = sourceIdentity(source);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

/**
 * Renumber citations by first appearance in the answer. Markdown code spans and
 * fenced code blocks are intentionally skipped so array indexes are not treated
 * as knowledge citations.
 */
export function normalizeSiteChatCitations(
  content: string,
  sources: SiteChatSource[],
): NormalizedSiteChatCitations {
  const sourceByIndex = new Map(sources.map((source) => [source.index, source]));
  const displayIndexBySource = new Map<string, number>();
  const normalizedSources: SiteChatSource[] = [];
  let inFence = false;
  let inInlineCode = false;
  let cursor = 0;
  let normalizedContent = "";

  while (cursor < content.length) {
    if (content.startsWith("```", cursor)) {
      inFence = !inFence;
      normalizedContent += "```";
      cursor += 3;
      continue;
    }

    const character = content[cursor];
    if (!inFence && character === "`" && content[cursor - 1] !== "\\") {
      inInlineCode = !inInlineCode;
      normalizedContent += character;
      cursor += 1;
      continue;
    }

    if (!inFence && !inInlineCode && character === "[" && content[cursor - 1] !== "\\") {
      const marker = content.slice(cursor).match(/^\[(\d+)\]/);
      if (marker) {
        const source = sourceByIndex.get(Number(marker[1]));
        if (source) {
          const identity = sourceIdentity(source);
          let displayIndex = displayIndexBySource.get(identity);
          if (!displayIndex) {
            displayIndex = normalizedSources.length + 1;
            displayIndexBySource.set(identity, displayIndex);
            normalizedSources.push({ ...source, index: displayIndex });
          }
          normalizedContent += `[${displayIndex}]`;
          cursor += marker[0].length;
          continue;
        }
      }
    }

    normalizedContent += character;
    cursor += 1;
  }

  if (normalizedSources.length > 0) {
    return { content: normalizedContent, sources: normalizedSources };
  }

  return {
    content,
    sources: uniqueSources(sources).map((source, index) => ({ ...source, index: index + 1 })),
  };
}
