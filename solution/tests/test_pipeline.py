"""End-to-end: idempotency, incremental == all-at-once, late leads, PII, append-only."""
import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.cli import main

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "assignment-a-data" / "tools" / "generate_data.py"


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    subprocess.run([sys.executable, str(GEN), "--seed", "123", "--out", str(out)], check=True,
                   capture_output=True)
    return out


def run(db, *args):
    assert main(["--db", str(db), *map(str, args)]) == 0


def read_reports(d: Path) -> dict:
    return {p.name: p.read_text() for p in sorted(d.iterdir())}


def test_incremental_equals_all_at_once_and_reingest_is_noop(data, tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    run(a, "ingest", data / "drop_1")
    run(a, "ingest", data / "drop_2")
    run(a, "report", "--out", tmp_path / "ra1")
    run(a, "ingest", data / "drop_1")       # re-run: must change nothing
    run(a, "ingest", data)                  # both again
    run(a, "report", "--out", tmp_path / "ra2")
    run(b, "ingest", data)                  # everything at once
    run(b, "report", "--out", tmp_path / "rb")
    r1, r2, rb = (read_reports(tmp_path / x) for x in ("ra1", "ra2", "rb"))
    assert r1 == r2 == rb
    assert set(r1) == {"batch_summary.csv", "daily_overview.csv", "lead_report.csv",
                       "rejections.csv", "data_quality.json"}


def test_orphans_resolve_when_lead_arrives_later(data, tmp_path):
    db = tmp_path / "x.db"
    run(db, "ingest", data / "drop_1")
    run(db, "report", "--out", tmp_path / "r1")
    run(db, "ingest", data / "drop_2")
    run(db, "report", "--out", tmp_path / "r2")
    # batch_003 leads only arrive in drop_2, but some were called before the drop_1 cutoff:
    # those drop_1 events were orphans after drop_1 and must now count.
    with open(tmp_path / "r2" / "lead_report.csv") as fh:
        b3_early = [r for r in csv.DictReader(fh) if r["batch_id"] == "batch_003"
                    and r["last_call_at"] and r["last_call_at"] < "2026-10-07"]
    assert b3_early, "expected batch_003 leads whose calls all came in drop_1"
    with open(tmp_path / "r1" / "lead_report.csv") as fh:
        assert not any(r["batch_id"] == "batch_003" for r in csv.DictReader(fh))


def test_pii_not_in_plaintext(data, tmp_path):
    db = tmp_path / "p.db"
    run(db, "ingest", data / "drop_1")
    some_phone = some_name = None
    with open(next((data / "drop_1" / "leads" / "motor_renewal").glob("*.csv")),
              encoding="utf-8") as fh:
        row = next(csv.DictReader(fh))
        some_name, some_phone = row["customer_name"], "".join(c for c in row["phone"] if c.isdigit())[-10:]
    dump = "\n".join(sqlite3.connect(db).iterdump())
    assert some_name not in dump
    assert some_phone not in dump


def test_ledger_is_append_only(data, tmp_path):
    db = tmp_path / "ao.db"
    run(db, "ingest", data / "drop_1")
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events")


def test_changed_file_is_refused(data, tmp_path):
    import shutil
    drop = tmp_path / "drop_1"
    shutil.copytree(data / "drop_1", drop)
    db = tmp_path / "c.db"
    run(db, "ingest", drop)
    f = next((drop / "events").glob("*.jsonl"))
    f.write_text(f.read_text() + '{"event_id": "evt_new"}\n')
    assert main(["--db", str(db), "ingest", str(drop)]) == 2
