PRAGMA foreign_keys = OFF;
ALTER TABLE events RENAME TO strict_events;
CREATE TABLE events (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  time INTEGER NOT NULL,
  data TEXT NOT NULL,
  source_event_seqs TEXT,
  surface_op TEXT,
  ignorable INTEGER,
  PRIMARY KEY (session_id, seq)
);
DROP TABLE strict_events;
