"""Derive facts from the ledger and write the five report files.

Everything here is a pure function of the database contents, so the reports always reflect
everything ingested so far and do not depend on the order or grouping of `ingest` runs.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .normalize import ist_date, to_ist_iso

BUCKETS = ["yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed"]


@dataclass
class Event:
    event_id: str
    event_type: str | None
    workflow_id: str | None
    call_id: str | None
    occurred_at: datetime | None
    phone_hash: str | None
    payload: dict

    @property
    def sort_key(self):
        # Unparseable timestamps sort first; event_id breaks exact-time ties deterministically.
        return (self.occurred_at is not None, self.occurred_at or datetime.min, self.event_id)


@dataclass
class Lead:
    lead_id: int
    workflow_id: str
    batch_id: str
    external_ref: str
    phone_last4: str


@dataclass
class Call:
    call_id: str
    lead_id: int
    initiated_at: datetime | None
    connected: bool = False
    telephony_failed: bool = False
    sets: list[Event] = field(default_factory=list)
    overrides: list[Event] = field(default_factory=list)

    @property
    def effective_disposition(self) -> str | None:
        src = self.overrides or self.sets
        return max(src, key=lambda e: e.sort_key).payload["disposition_code"] if src else None


@dataclass
class Facts:
    leads: dict[int, Lead]
    calls: dict[str, Call]
    orphan_events: int
    unknown_disposition_events: int


def bucket_for(calls: list[Call], catalogue: dict, workflow_id: str) -> str:
    if not calls:
        return "yet_to_reach_out"
    if any(catalogue[(workflow_id, c.effective_disposition)][0]
           for c in calls if c.effective_disposition):
        return "completed"
    if all(c.telephony_failed for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def derive(conn: sqlite3.Connection, catalogue: dict) -> Facts:
    leads = {r[0]: Lead(*r) for r in conn.execute(
        "SELECT lead_id, workflow_id, batch_id, external_ref, phone_last4 FROM leads")}
    link = {(wf, h): lid for lid, wf, h in conn.execute(
        "SELECT lead_id, workflow_id, phone_hash FROM leads")}

    events = []
    for r in conn.execute("SELECT event_id, event_type, workflow_id, call_id, occurred_at,"
                          " phone_hash, payload_json FROM events"):
        events.append(Event(r[0], r[1], r[2], r[3],
                            datetime.fromisoformat(r[4]) if r[4] else None, r[5], json.loads(r[6])))
    events.sort(key=lambda e: e.sort_key)

    # 1) Link phone-bearing events by (workflow_id, phone). 2) Overrides inherit the lead of
    #    their call's earliest linked phone-bearing event.
    linked: dict[str, int] = {}
    call_lead: dict[str, int] = {}
    for e in events:
        if e.event_type == "disposition.override":
            continue
        lid = link.get((e.workflow_id, e.phone_hash)) if e.phone_hash else None
        if lid is not None:
            linked[e.event_id] = lid
            if e.call_id:
                call_lead.setdefault(e.call_id, lid)
    for e in events:
        if e.event_type == "disposition.override" and e.call_id in call_lead:
            linked[e.event_id] = call_lead[e.call_id]

    orphans = sum(1 for e in events if e.event_id not in linked)

    # Calls exist only where a linked call.initiated is present.
    calls: dict[str, Call] = {}
    for e in events:
        if e.event_type == "call.initiated" and e.event_id in linked and e.call_id \
                and e.call_id not in calls:
            calls[e.call_id] = Call(e.call_id, linked[e.event_id], e.occurred_at)

    unknown = 0
    for e in events:
        if e.event_id not in linked:
            continue
        lid = linked[e.event_id]
        if e.event_type in ("disposition.set", "disposition.override"):
            code = (e.payload or {}).get("disposition_code")
            if (leads[lid].workflow_id, code) not in catalogue:
                unknown += 1
                continue
        c = calls.get(e.call_id)
        if c is None:
            continue
        if e.event_type == "call.connected":
            c.connected = True
        elif e.event_type == "call.telephony_failed":
            c.telephony_failed = True
        elif e.event_type == "disposition.set":
            c.sets.append(e)
        elif e.event_type == "disposition.override":
            c.overrides.append(e)
    return Facts(leads, calls, orphans, unknown)


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _b(v: bool) -> str:
    return "true" if v else "false"


def write_reports(conn: sqlite3.Connection, catalogue: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    f = derive(conn, catalogue)
    is_success = lambda wf, code: bool(code) and catalogue[(wf, code)][1]  # noqa: E731

    calls_by_lead: dict[int, list[Call]] = defaultdict(list)
    for c in f.calls.values():
        calls_by_lead[c.lead_id].append(c)

    # ---- lead_report.csv + batch_summary.csv
    batch_counts: dict[tuple, dict] = defaultdict(lambda: dict.fromkeys(BUCKETS, 0))
    lead_rows = []
    for lead in f.leads.values():
        lc = sorted(calls_by_lead.get(lead.lead_id, []),
                    key=lambda c: (c.initiated_at is not None, c.initiated_at or datetime.min,
                                   c.call_id))
        bucket = bucket_for(lc, catalogue, lead.workflow_id)
        batch_counts[(lead.workflow_id, lead.batch_id)][bucket] += 1
        last_at = next((c.initiated_at for c in reversed(lc) if c.initiated_at), None)
        current = next((c.effective_disposition for c in reversed(lc)
                        if c.effective_disposition), None)
        lead_rows.append([
            lead.workflow_id, lead.external_ref, lead.batch_id, f"+91******{lead.phone_last4}",
            bucket, len(lc), sum(c.connected for c in lc),
            to_ist_iso(last_at) if last_at else "", current or "",
            _b(any(is_success(lead.workflow_id, c.effective_disposition) for c in lc)),
        ])
    lead_rows.sort(key=lambda r: (r[0], r[1]))
    _write_csv(out / "lead_report.csv",
               ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
                "connected_calls", "last_call_at", "current_disposition", "ever_success"],
               lead_rows)

    _write_csv(out / "batch_summary.csv",
               ["workflow_id", "batch_id", "total_leads", *BUCKETS],
               [[wf, b, sum(c.values()), *(c[k] for k in BUCKETS)]
                for (wf, b), c in sorted(batch_counts.items())])

    # ---- daily_overview.csv
    daily: dict[tuple, dict] = defaultdict(lambda: {"total": 0, "connected": 0, "success": set()})
    for c in f.calls.values():
        if c.initiated_at is None:
            continue
        wf = f.leads[c.lead_id].workflow_id
        d = daily[(wf, ist_date(c.initiated_at))]
        d["total"] += 1
        d["connected"] += c.connected
        if is_success(wf, c.effective_disposition):
            d["success"].add(c.lead_id)
    _write_csv(out / "daily_overview.csv",
               ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate",
                "success_leads"],
               [[wf, day, d["total"], d["connected"], f"{d['connected'] / d['total']:.4f}",
                 len(d["success"])] for (wf, day), d in sorted(daily.items())])

    # ---- rejections.csv (no PII: only identifiers and reason)
    _write_csv(out / "rejections.csv", ["workflow_id", "batch_id", "row_number", "reason"],
               [list(r) for r in conn.execute(
                   "SELECT workflow_id, batch_id, row_number, reason FROM lead_row_outcomes"
                   " WHERE outcome = 'rejected' ORDER BY workflow_id, batch_id, row_number")])

    # ---- data_quality.json
    q = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(outcome = 'rejected'), 0), COALESCE(SUM(outcome = 'merged'), 0),"
        " COALESCE(SUM(CASE WHEN outcome <> 'rejected' THEN warnings END), 0)"
        " FROM lead_row_outcomes").fetchone()
    e = conn.execute("SELECT COALESCE(SUM(lines_total), 0), COALESCE(SUM(malformed), 0),"
                     " COALESCE(SUM(duplicates), 0) FROM ingested_files WHERE kind = 'events'"
                     ).fetchone()
    by_reason = dict(conn.execute(
        "SELECT reason, COUNT(*) FROM lead_row_outcomes WHERE outcome = 'rejected'"
        " GROUP BY reason ORDER BY reason").fetchall())
    dq = {
        "lead_rows_total": q[0],
        "lead_rows_rejected": q[1],
        "rejections_by_reason": by_reason,
        "duplicate_lead_rows_merged": q[2],
        "field_warnings": q[3],
        "events_total": e[0],
        "malformed_events": e[1],
        "duplicate_events_dropped": e[2],
        "orphan_events": f.orphan_events,
        "unknown_disposition_events": f.unknown_disposition_events,
    }
    (out / "data_quality.json").write_text(json.dumps(dq, indent=2) + "\n", encoding="utf-8")
