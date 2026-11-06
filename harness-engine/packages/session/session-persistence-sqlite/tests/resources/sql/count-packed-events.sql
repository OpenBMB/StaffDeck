SELECT COUNT(*) AS count
FROM events
WHERE type = 'text-chunks' AND ignorable = 0;
