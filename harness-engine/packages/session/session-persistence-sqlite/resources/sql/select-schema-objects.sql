SELECT type, name, tbl_name, sql
FROM sqlite_schema
WHERE name NOT GLOB 'sqlite_*'
ORDER BY type, name;
