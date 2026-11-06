SELECT COUNT(*) AS rows,
       COALESCE(MAX(length(CAST(data AS BLOB))), 0) AS largest
FROM events;
