CREATE TABLE persistence_state (singleton ANY, store_id ANY);
CREATE TABLE sessions (
  id ANY, version ANY, created_at ANY, cwd ANY, parent_session ANY,
  seed_length ANY, origin ANY, delegation_depth ANY, agent_preset ANY,
  incarnation ANY, revision ANY
);
CREATE TABLE events (
  session_id ANY, seq ANY, type ANY, time ANY, data ANY,
  source_event_seqs ANY, surface_op ANY, ignorable ANY
);
INSERT INTO persistence_state (singleton, store_id)
VALUES (1, '00000000-0000-4000-8000-000000000000');
PRAGMA application_id = 1146308688;
PRAGMA user_version = 20;
