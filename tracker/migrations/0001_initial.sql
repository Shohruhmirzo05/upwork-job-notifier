CREATE TABLE IF NOT EXISTS jobs (
  cipher TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  skills_json TEXT NOT NULL DEFAULT '[]',
  matched_json TEXT NOT NULL DEFAULT '[]',
  budget TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  publish_time TEXT,
  score INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL DEFAULT '',
  proposal TEXT NOT NULL DEFAULT '',
  hook_type TEXT NOT NULL DEFAULT 'unclassified',
  screening_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','generated','applied','viewed','replied','interview','won','lost','skipped')),
  applied_confirmed INTEGER NOT NULL DEFAULT 0,
  tags_json TEXT NOT NULL DEFAULT '[]',
  notes TEXT NOT NULL DEFAULT '',
  notified_at TEXT,
  generated_at TEXT,
  applied_at TEXT,
  viewed_at TEXT,
  replied_at TEXT,
  interview_at TEXT,
  won_at TEXT,
  lost_at TEXT,
  skipped_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS jobs_status_updated_idx ON jobs(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS jobs_publish_idx ON jobs(publish_time DESC);
CREATE INDEX IF NOT EXISTS jobs_hook_idx ON jobs(hook_type, status);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_cipher TEXT NOT NULL,
  event_type TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(job_cipher) REFERENCES jobs(cipher) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS events_job_idx ON events(job_cipher, created_at DESC);
