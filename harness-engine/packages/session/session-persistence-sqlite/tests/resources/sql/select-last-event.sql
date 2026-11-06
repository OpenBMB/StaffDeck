SELECT seq, type, data
FROM events
WHERE session_id = (SELECT id FROM sessions WHERE session_key = ?)
ORDER BY seq DESC
LIMIT 1;
