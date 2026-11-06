SELECT session_key AS id, version, created_at, cwd, parent_session, seed_length, origin,
       delegation_depth, agent_preset, incarnation, revision
FROM sessions;
