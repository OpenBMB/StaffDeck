DELETE FROM events
WHERE session_id = (SELECT id FROM sessions WHERE session_key = ?)
