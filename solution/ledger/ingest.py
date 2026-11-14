"""Ingest drops into the database: idempotent per file, atomic per file."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .normalize import normalise_phone, parse_ts
from .pii import Pii
from .schema import WorkflowSchema, validate_row

log = logging.getLogger(__name__)


class IngestError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_drops(path: Path) -> list[Path]:
    """A drop dir (has leads/ or events/), or a parent of drop dirs (ingested in name order)."""
    if (path / "leads").is_dir() or (path / "events").is_dir():
        return [path]
    drops = sorted(p for p in path.iterdir() if p.is_dir() and
                   ((p / "leads").is_dir() or (p / "events").is_dir()))
    if not drops:
        raise IngestError(f"{path} is not a drop directory and contains no drops")
    return drops


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, schemas: dict[str, WorkflowSchema], pii: Pii):
        self.conn = conn
        self.schemas = schemas
        self.pii = pii

    # ------------------------------------------------------------------ public
    def ingest_path(self, path: Path) -> dict:
        stats = {"files_ingested": 0, "files_skipped": 0}
        for drop in resolve_drops(path):
            lead_files = sorted((drop / "leads").glob("*/*.csv"),
                                key=lambda p: (p.parent.name, p.name))
            event_files = sorted((drop / "events").glob("*.jsonl"), key=lambda p: p.name)
            for f, kind in [*((x, "leads") for x in lead_files),
                            *((x, "events") for x in event_files)]:
                applied = self._ingest_file(drop, f, kind)
                stats["files_ingested" if applied else "files_skipped"] += 1
        return stats

    # ------------------------------------------------------------------ per file
    def _ingest_file(self, drop: Path, f: Path, kind: str) -> bool:
        key = f"{drop.name}/{f.relative_to(drop).as_posix()}"
        digest = _sha256(f)
        row = self.conn.execute(
            "SELECT sha256 FROM ingested_files WHERE file_key = ?", (key,)).fetchone()
        if row:
            if row[0] != digest:
                raise IngestError(
                    f"{key} was already ingested with different content; refusing to re-apply")
            log.info("skip %s (already ingested)", key)
            return False
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(ingest_seq), 0) + 1 FROM ingested_files").fetchone()[0]
            self.conn.execute(
                "INSERT INTO ingested_files (file_key, sha256, kind, ingest_seq, ingested_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, digest, kind, seq, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            if kind == "leads":
                self._ingest_leads(key, f)
            else:
                self._ingest_events(key, f)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        log.info("ingested %s", key)
        return True

    # ------------------------------------------------------------------ leads
    def _ingest_leads(self, key: str, f: Path) -> None:
        workflow_id = f.parent.name
        schema = self.schemas.get(workflow_id)
        if schema is None:
            raise IngestError(f"no schema for workflow '{workflow_id}' ({key})")
        batch_id = f.stem
        phone_field = schema.phone_field
        ref_field = schema.reference_field
        pii_fields = [x.name for x in schema.fields if x.pii]
        defaults = {x.name: x.default for x in schema.fields if x.default is not None}

        with open(f, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            headers = next(reader, None)
            if headers is None:
                return
            colmap = schema.header_map(headers)
            row_number = 0
            for cells in reader:
                if all(not c.strip() for c in cells):
                    continue  # blank row: skipped and not counted
                row_number += 1
                raw = {}
                for i, fld in colmap.items():
                    raw[fld.name] = cells[i].strip() if i < len(cells) else ""
                res = validate_row(schema, raw)
                if res.values is None:
                    self._outcome(key, workflow_id, batch_id, row_number, "rejected", res.reason, 0)
                    continue
                phone = res.values[phone_field]
                phash = self.pii.phone_hash(phone)
                existing = self.conn.execute(
                    "SELECT lead_id, attrs_json, pii_enc FROM leads"
                    " WHERE workflow_id = ? AND phone_hash = ?", (workflow_id, phash)).fetchone()
                if existing is None:
                    merged = dict(res.values)
                    outcome = "created"
                else:
                    merged = {**json.loads(existing[1]), **json.loads(self.pii.decrypt(existing[2]))}
                    for k, v in res.values.items():
                        if v is not None:  # last non-null write wins
                            merged[k] = v
                    outcome = "merged"
                for k, d in defaults.items():
                    if merged.get(k) is None:
                        merged[k] = d
                attrs = {k: v for k, v in merged.items() if k not in pii_fields}
                pii_blob = self.pii.encrypt(json.dumps(
                    {k: merged.get(k) for k in pii_fields}, sort_keys=True))
                if existing is None:
                    self.conn.execute(
                        "INSERT INTO leads (workflow_id, phone_hash, phone_last4, batch_id,"
                        " external_ref, attrs_json, pii_enc) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (workflow_id, phash, phone[-4:], batch_id, merged[ref_field],
                         json.dumps(attrs, sort_keys=True), pii_blob))
                else:
                    self.conn.execute(
                        "UPDATE leads SET external_ref = ?, attrs_json = ?, pii_enc = ?"
                        " WHERE lead_id = ?",
                        (merged[ref_field], json.dumps(attrs, sort_keys=True), pii_blob,
                         existing[0]))
                self._outcome(key, workflow_id, batch_id, row_number, outcome, None, res.warnings)

    def _outcome(self, key, wf, batch, rn, outcome, reason, warnings):
        self.conn.execute(
            "INSERT INTO lead_row_outcomes (file_key, workflow_id, batch_id, row_number, outcome,"
            " reason, warnings) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, wf, batch, rn, outcome, reason, warnings))

    # ------------------------------------------------------------------ events
    def _ingest_events(self, key: str, f: Path) -> None:
        total = malformed = duplicates = 0
        insert = ("INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id,"
                  " occurred_at, phone_hash, payload_json, file_key, line_no)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
        with open(f, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                total += 1
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(ev, dict) or ev.get("event_id") in (None, ""):
                    malformed += 1
                    continue
                phone = normalise_phone(ev["phone"]) if ev.get("phone") else None
                ts = parse_ts(ev.get("occurred_at"))
                payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                cur = self.conn.execute(insert, (
                    str(ev["event_id"]), ev.get("event_type"), ev.get("workflow_id"),
                    ev.get("call_id"), ts.isoformat() if ts else None,
                    self.pii.phone_hash(phone) if phone else None,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False), key, line_no))
                if cur.rowcount == 0:
                    duplicates += 1
        self.conn.execute(
            "UPDATE ingested_files SET lines_total = ?, malformed = ?, duplicates = ?"
            " WHERE file_key = ?", (total, malformed, duplicates, key))
