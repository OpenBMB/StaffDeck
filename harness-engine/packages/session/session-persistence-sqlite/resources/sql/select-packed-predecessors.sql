SELECT seq, type, time, data, source_event_seqs, surface_op, ignorable
FROM events
WHERE session_id = ? AND seq >= ? AND seq < ?
  AND type IN ('text-chunks', 'reasoning-chunks', 'tool-call-chunks')
  AND ignorable = 0
ORDER BY seq;
