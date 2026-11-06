INSERT INTO events (session_id, seq, type, time, data, ignorable)
VALUES ((SELECT id FROM sessions WHERE session_key = ?), ?, ?, ?, ?, ?);
