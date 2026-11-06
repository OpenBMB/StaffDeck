UPDATE sessions
SET revision = revision + 1
WHERE session_key = ?;
