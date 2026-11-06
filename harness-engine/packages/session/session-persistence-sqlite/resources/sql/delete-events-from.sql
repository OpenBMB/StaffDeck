DELETE FROM events
WHERE session_id = ? AND seq >= ?;
