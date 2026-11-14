"""Command line: python -m ledger [--db PATH] {ingest DIR... | report --out DIR}"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .db import connect
from .ingest import IngestError, Ingestor
from .pii import Pii
from .report import write_reports
from .schema import load_dispositions, load_schemas

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMAS = REPO_ROOT / "assignment-a-data" / "schemas"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ledger")
    ap.add_argument("--db", type=Path, default=Path("ledger.db"), help="SQLite file")
    ap.add_argument("--schemas", type=Path, default=DEFAULT_SCHEMAS)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_in = sub.add_parser("ingest", help="ingest one or more drops (or a dir of drops)")
    p_in.add_argument("paths", nargs="+", type=Path)
    p_rep = sub.add_parser("report", help="write the five report files")
    p_rep.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    conn = connect(args.db)
    pii = Pii.from_env_or_file(args.db.with_name(args.db.name + ".key"))
    t0 = time.perf_counter()
    try:
        if args.cmd == "ingest":
            ing = Ingestor(conn, load_schemas(args.schemas), pii)
            for p in args.paths:
                s = ing.ingest_path(p)
                print(f"{p}: {s['files_ingested']} file(s) ingested,"
                      f" {s['files_skipped']} already ingested")
        else:
            write_reports(conn, load_dispositions(args.schemas), args.out)
            print(f"reports written to {args.out}")
    except IngestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"done in {time.perf_counter() - t0:.2f}s", file=sys.stderr)
    return 0
