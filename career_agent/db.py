"""SQLite storage. One connection per thread, WAL mode, plain SQL."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id              INTEGER PRIMARY KEY,
  filename        TEXT NOT NULL,
  source_path     TEXT,
  stored_path     TEXT,
  sha256          TEXT UNIQUE,
  ext             TEXT,
  kind            TEXT NOT NULL DEFAULT 'unknown',
  language        TEXT,
  doc_date        TEXT,
  title           TEXT,
  summary         TEXT,
  text            TEXT NOT NULL DEFAULT '',
  word_count      INTEGER NOT NULL DEFAULT 0,
  target_company  TEXT,
  target_role     TEXT,
  outcome         TEXT NOT NULL DEFAULT 'unknown',
  use_for_style   INTEGER NOT NULL DEFAULT 1,
  authored        INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'new',
  error           TEXT,
  created_at      TEXT NOT NULL,
  analyzed_at     TEXT
);

CREATE TABLE IF NOT EXISTS facts (
  id            INTEGER PRIMARY KEY,
  category      TEXT NOT NULL,
  title         TEXT NOT NULL,
  organization  TEXT,
  location      TEXT,
  start_date    TEXT,
  end_date      TEXT,
  detail        TEXT,
  evidence      TEXT,
  tags          TEXT,
  document_id   INTEGER REFERENCES documents(id) ON DELETE SET NULL,
  verified      INTEGER NOT NULL DEFAULT 0,
  hidden        INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'extracted',
  created_at    TEXT NOT NULL,
  updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS facts_category ON facts(category);

CREATE TABLE IF NOT EXISTS companies (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  website      TEXT,
  industry     TEXT,
  location     TEXT,
  description  TEXT,
  notes        TEXT,
  user_rating  INTEGER,
  blocked      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  kind              TEXT NOT NULL,
  url               TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  interval_minutes  INTEGER NOT NULL DEFAULT 360,
  last_run_at       TEXT,
  last_status       TEXT,
  last_error        TEXT,
  jobs_found        INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  id               INTEGER PRIMARY KEY,
  url              TEXT UNIQUE,
  source_id        INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  company_id       INTEGER REFERENCES companies(id) ON DELETE SET NULL,
  title            TEXT,
  company_name     TEXT,
  location         TEXT,
  work_mode        TEXT,
  employment_type  TEXT,
  seniority        TEXT,
  language         TEXT,
  posted_at        TEXT,
  deadline         TEXT,
  apply_url        TEXT,
  apply_method     TEXT,
  contact_name     TEXT,
  contact_email    TEXT,
  contact_phone    TEXT,
  salary           TEXT,
  summary          TEXT,
  description      TEXT,
  details          TEXT,
  fingerprint      TEXT,
  reposted         INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'new',
  parse_status     TEXT NOT NULL DEFAULT 'pending',
  parse_error      TEXT,
  fit_score        INTEGER,
  fit              TEXT,
  scored_at        TEXT,
  notes            TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS applications (
  id                 INTEGER PRIMARY KEY,
  job_id             INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  status             TEXT NOT NULL DEFAULT 'drafting',
  language           TEXT,
  subject            TEXT,
  letter             TEXT,
  cv                 TEXT,
  claims             TEXT,
  voice              TEXT,
  writer_notes       TEXT,
  instructions       TEXT,
  interview_prep     TEXT,
  followup_draft     TEXT,
  applied_at         TEXT,
  first_response_at  TEXT,
  last_event_at      TEXT,
  closed_at          TEXT,
  auto_drafted       INTEGER NOT NULL DEFAULT 0,
  created_at         TEXT NOT NULL,
  updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS app_versions (
  id              INTEGER PRIMARY KEY,
  application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
  label           TEXT,
  subject         TEXT,
  letter          TEXT,
  cv              TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY,
  application_id  INTEGER REFERENCES applications(id) ON DELETE CASCADE,
  company_id      INTEGER REFERENCES companies(id) ON DELETE CASCADE,
  type            TEXT NOT NULL,
  occurred_at     TEXT NOT NULL,
  summary         TEXT,
  content         TEXT,
  feedback        TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_app ON events(application_id);

CREATE TABLE IF NOT EXISTS tasks (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  ref          TEXT,
  status       TEXT NOT NULL DEFAULT 'queued',
  progress     TEXT,
  error        TEXT,
  created_at   TEXT NOT NULL,
  started_at   TEXT,
  finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS llm_calls (
  id                  INTEGER PRIMARY KEY,
  purpose             TEXT,
  provider            TEXT,
  model               TEXT,
  input_tokens        INTEGER,
  output_tokens       INTEGER,
  cache_read_tokens   INTEGER,
  cache_write_tokens  INTEGER,
  cost_usd            REAL,
  duration_ms         INTEGER,
  ok                  INTEGER,
  error               TEXT,
  created_at          TEXT NOT NULL
);
"""

# Schema changes after the first release, applied in order (tracked with PRAGMA user_version).
MIGRATIONS: list[str] = [
    # 1: retrieval (chunks + embeddings) and watched folders
    """
    ALTER TABLE documents ADD COLUMN source_mtime REAL;
    ALTER TABLE documents ADD COLUMN watch_id INTEGER;
    CREATE INDEX IF NOT EXISTS documents_source ON documents(source_path);

    CREATE TABLE IF NOT EXISTS chunks (
      id           INTEGER PRIMARY KEY,
      document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      idx          INTEGER NOT NULL,
      text         TEXT NOT NULL,
      UNIQUE (document_id, idx)
    );

    CREATE TABLE IF NOT EXISTS embeddings (
      kind    TEXT NOT NULL,
      ref_id  INTEGER NOT NULL,
      model   TEXT NOT NULL,
      hash    TEXT NOT NULL,
      vec     BLOB NOT NULL,
      PRIMARY KEY (kind, ref_id)
    );

    CREATE TABLE IF NOT EXISTS watched_folders (
      id                INTEGER PRIMARY KEY,
      kind              TEXT NOT NULL DEFAULT 'local',
      path              TEXT NOT NULL UNIQUE,
      username          TEXT,
      enabled           INTEGER NOT NULL DEFAULT 1,
      interval_minutes  INTEGER NOT NULL DEFAULT 30,
      last_scan_at      TEXT,
      last_status       TEXT,
      last_error        TEXT,
      files_seen        INTEGER NOT NULL DEFAULT 0,
      created_at        TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS remote_files (
      folder_id   INTEGER NOT NULL REFERENCES watched_folders(id) ON DELETE CASCADE,
      href        TEXT NOT NULL,
      etag        TEXT,
      local_path  TEXT NOT NULL,
      PRIMARY KEY (folder_id, href)
    );
    """,
    # 2: your to-do list and reminders
    """
    CREATE TABLE IF NOT EXISTS todos (
      id           INTEGER PRIMARY KEY,
      title        TEXT NOT NULL,
      detail       TEXT,
      href         TEXT,
      check_kind   TEXT,
      show_from    TEXT,
      position     INTEGER NOT NULL DEFAULT 0,
      done_at      TEXT,
      notified_at  TEXT,
      created_at   TEXT NOT NULL
    );
    """,
    # 3: retrying passing errors, and which item each model call was for (so you can see what made what)
    """
    ALTER TABLE tasks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN run_after TEXT;
    ALTER TABLE llm_calls ADD COLUMN ref TEXT;
    CREATE INDEX IF NOT EXISTS llm_calls_ref ON llm_calls(ref);
    """,
]


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == str(config.DB_PATH):
        return conn
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    _local.path = str(config.DB_PATH)
    return conn


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, script in enumerate(MIGRATIONS, start=1):
        if version < n:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {n};\nCOMMIT;")


def now() -> str:
    """UTC timestamp, ISO 8601 without timezone suffix (SQLite convention)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = connect().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def scalar(sql: str, params: Iterable[Any] = ()) -> Any:
    row = connect().execute(sql, tuple(params)).fetchone()
    return row[0] if row else None


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    cur = connect().execute(sql, tuple(params))
    return cur.lastrowid


def insert(table: str, data: dict) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return ex(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data.values())


def update(table: str, row_id: int, data: dict) -> None:
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    ex(f"UPDATE {table} SET {sets} WHERE id = ?", [*data.values(), row_id])


@contextlib.contextmanager
def transaction():
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def jload(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
