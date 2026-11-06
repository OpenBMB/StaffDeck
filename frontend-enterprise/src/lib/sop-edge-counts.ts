/** Count graph labels without interpreting user-provided names as object prototypes. */
export function countEdgeLabels<T extends { source_node_id?: unknown }>(
  edges: readonly T[],
  labelFor: (edge: T) => string,
): Record<string, Record<string, number>> {
  const counts: Record<string, Record<string, number>> = Object.create(null);
  for (const edge of edges) {
    const source = String(edge.source_node_id || '').trim();
    if (!source) continue;
    const label = labelFor(edge);
    const labels = counts[source] ||= Object.create(null);
    labels[label] = (labels[label] || 0) + 1;
  }
  return counts;
}
