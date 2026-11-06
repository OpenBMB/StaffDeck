SELECT seq, type, time, data, source_event_seqs, surface_op, ignorable
FROM events
WHERE session_id = ? AND seq >= ?
ORDER BY seq;
