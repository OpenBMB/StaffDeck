UPDATE sessions
SET origin = 'external', delegation_depth = -1, seed_length = -1
WHERE session_key = ?;
