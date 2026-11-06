SELECT type, COUNT(*) AS count
FROM events
WHERE type IN ('text-chunks', 'reasoning-chunks', 'tool-call-chunks')
  AND ignorable = 0
GROUP BY type
ORDER BY type;
