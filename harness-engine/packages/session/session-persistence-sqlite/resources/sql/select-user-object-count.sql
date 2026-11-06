SELECT COUNT(*) AS count
FROM sqlite_schema
WHERE name NOT GLOB 'sqlite_*';
