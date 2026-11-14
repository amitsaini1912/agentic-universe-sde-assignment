"""SQLite schema. See SUBMISSION.md for the reasoning behind each table."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- One row per source file ever ingested. A file is ingested in ONE transaction together with
-- everything derived from it, so it is either fully applied or not at all (crash-safe), and a
-- re-run that finds the file_key here skips it (idempotent).
CREATE TABLE IF NOT EXISTS ingested_files (
    file_key      TEXT PRIMARY KEY,            -- '<drop>/<relative path>'
    sha256        TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    ingest_seq    INTEGER NOT NULL,            -- global order files were applied in
    ingested_at   TEXT NOT NULL,
    lines_total   INTEGER NOT NULL DEFAULT 0,  -- events: non-empty lines
    malformed     INTEGER NOT NULL DEFAULT 0,
    duplicates    INTEGER NOT NULL DEFAULT 0
);

-- Outcome of every non-blank lead row. No PII. Feeds rejections.csv and data_quality.json.
CREATE TABLE IF NOT EXISTS lead_row_outcomes (
    file_key    TEXT NOT NULL REFERENCES ingested_files(file_key),
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('created', 'merged', 'rejected')),
    reason      TEXT,
    warnings    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_key, row_number),
    CHECK ((outcome = 'rejected') = (reason IS NOT NULL))
);

-- Current merged state of each lead. Identity = (workflow_id, phone).
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_hash    TEXT NOT NULL,               -- HMAC-SHA256 of '+91XXXXXXXXXX'
    phone_last4   TEXT NOT NULL,               -- for phone_masked only
    batch_id      TEXT NOT NULL,               -- first valid row's file stem; never changes
    external_ref  TEXT NOT NULL,
    attrs_json    TEXT NOT NULL,               -- non-PII normalised fields
    pii_enc       TEXT NOT NULL,               -- Fernet-encrypted JSON of PII fields
    UNIQUE (workflow_id, phone_hash)
);

-- The interaction ledger: append-only, one row per distinct event_id. Raw phone is never
-- stored; only its keyed hash. Linking to leads is derived at report time so an event that
-- arrived before its lead is picked up automatically once the lead exists.
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT,                         -- UTC ISO-8601, NULL if unparseable
    phone_hash   TEXT,                         -- NULL if absent or invalid
    payload_json TEXT NOT NULL,
    file_key     TEXT NOT NULL REFERENCES ingested_files(file_key),
    line_no      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn
