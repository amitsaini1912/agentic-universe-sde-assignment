# Submission

## Candidate
- **Name:** Amit Saini
- **Assignment chosen:** A (Data): Interaction Ledger & Reporting Pipeline
- **Time spent (honest estimate):** ~3h, built with heavy use of an AI coding agent (see `AI_USAGE.md`)

## How to run
```bash
# setup (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# generate data (not committed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# run the pipeline (from solution/)
cd solution
python -m ledger --db ledger.db ingest ../data/drop_1
python -m ledger --db ledger.db ingest ../data/drop_2
python -m ledger --db ledger.db report --out reports/

# everything at once also works (a directory of drops is ingested in name order)
python -m ledger --db other.db ingest ../data && python -m ledger --db other.db report --out reports_all/

# tests (one command, from solution/)
python -m pytest -q
```
`--db` defaults to `./ledger.db`. The PII key is read from `$AU_PII_KEY` (urlsafe-base64, 32 bytes);
if unset, a random key is created once at `<db>.key` (mode 0600, git-ignored).

## Design

```
CSV leads ──► validate/normalise ──► leads (merged state)  ┐
          └─► lead_row_outcomes (created/merged/rejected)   │  report = pure function
JSONL events ─► dedupe by event_id ─► events (append-only)  ├─► of DB contents ─► 5 files
          └─► per-file counters in ingested_files           ┘  (linking done here)
```

**Unit of work = one source file.** Each file is applied in a single SQLite transaction together with
its row in `ingested_files` (`file_key = <drop>/<relative path>`, plus SHA-256). Re-ingesting a file
that is already recorded is skipped, so re-runs change nothing; a crash mid-file rolls back cleanly.
If a file with the same key comes back with *different* content, ingest refuses (exit 2) rather than
silently double-applying or mutating history.

**Schema (SQLite):**

| Table | Purpose | Key constraints |
|---|---|---|
| `ingested_files` | idempotency + per-file event counters (lines, malformed, duplicates) | PK `file_key`; `ingest_seq` records apply order |
| `lead_row_outcomes` | one row per non-blank lead row: `created` / `merged` / `rejected` + reason + warnings. No PII. Drives `rejections.csv` and lead counters in `data_quality.json` | PK `(file_key,row_number)`; CHECK reason present iff rejected |
| `leads` | current merged lead. `batch_id` set on insert and never updated | UNIQUE `(workflow_id, phone_hash)` = lead identity |
| `events` | **interaction ledger**, one row per distinct `event_id` | PK `event_id` (dedupe via `INSERT OR IGNORE`); triggers abort any UPDATE/DELETE |

**Linking is derived, not stored.** Events are stored with `phone_hash` and linked to leads at report
time by `(workflow_id, phone_hash)`; overrides inherit the lead of their `call_id`. So an event that
arrived before its lead (an orphan after `drop_1`) is automatically counted once the lead lands in
`drop_2`, orphan counts always reflect the final state, and incremental ingest gives byte-identical
reports to an all-at-once ingest (tested).

**PII at rest.** Phones are stored only as HMAC-SHA256 under a secret key (deterministic, so events
match leads without the number ever being stored) plus the last 4 digits needed for `phone_masked`.
Fields marked `pii: true` (name, phone) are kept recoverable in a Fernet-encrypted JSON blob. Raw event
phones are discarded after hashing. Hash and encryption use separate sub-keys derived from one master
key. A plain SHA-256 was rejected: the Indian mobile space (~4×10⁹) is brute-forceable in minutes.

**Derived facts** (in `ledger/report.py`): a call exists only with a linked `call.initiated`;
effective disposition = latest known override, else latest known set (ordered by `occurred_at`, then
`event_id`); buckets evaluated in the spec's priority order.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Ordering ties on equal `occurred_at` broken by `event_id` | deterministic output | arrival order (not stable across drops) |
| Override linking: lead of the call's **earliest** linked phone-bearing event | spec says "the call's other events"; data never splits a call across leads | require all events to agree |
| Unknown-code overrides/sets are ignored when picking the effective disposition (an earlier known override still applies) | spec says unknown dispositions are ignored | treat unknown override as "clears" disposition |
| `unknown_disposition_events` counts only **linked** events | spec check order: orphan is decided before unknown disposition | count all |
| Two columns mapping to the same field: first column wins | deterministic; not present in data | last wins / reject file |
| Required non-phone field present but unparseable → `invalid_required:<field>` | spec only defines `missing_required` and `invalid_phone`; current schemas can't hit this | treat as missing |
| `DD/MM/YYYY` and `DD-MM-YYYY` accept 1–2 digit day/month; mixed separators (`01/12-2026`) invalid | lenient on padding, strict on format | require zero padding |
| Decimal: `₹`, `Rs`, `Rs.`, `INR` prefix only; commas must be valid thousands groups (`1,23` invalid); negatives invalid; ROUND_HALF_UP to 2 dp | money should round half-up | banker's rounding |
| `connectivity_rate` uses Python's `f"{x:.4f}"` | matches the spec example `0.5625`; exact-tie cases (e.g. 1/32) round half-even on the float | Decimal half-up |
| `rejections_by_reason` lists only reasons that occurred, sorted | avoids inventing zero keys for schema-specific reasons | list every possible reason |
| Defaults applied when a lead is written (after each merge) | equivalent to "after merging" since merges only overwrite with non-null values | apply at read time |
| Events with unparseable `occurred_at` are kept (not malformed) and sort first | spec defines malformed narrowly; doesn't occur in data | count as malformed |
| A parent directory of drops is ingested in drop-name order | "ingest everything at once" | require one drop per call |

## Failure modes considered
- **Re-runs / partial runs:** per-file transaction + `ingested_files` ⇒ idempotent and crash-safe.
- **Changed source file under the same name:** refused with a clear error (would otherwise corrupt history).
- **Duplicate events within a file, across files, across drops:** PK on `event_id`.
- **Events before leads, overrides with no phone:** linking is recomputed at report time.
- **Dirty input:** six phone formats, BOM, blank rows, renamed/re-ordered/extra columns, mixed dates
  and currencies, impossible dates, malformed JSON lines, events with invalid phones.
- **Accidental ledger edits:** SQLite triggers abort UPDATE/DELETE on `events`.
- **Not handled:** concurrent `ingest` processes on one DB are serialised by `BEGIN IMMEDIATE` but not
  load-tested; key rotation for the PII key; deleting/correcting a bad batch (would need a
  compensating "batch retracted" record rather than a delete).

## Testing
- `python -m pytest -q` (59 tests, ~2 s): phone formats/invalids, all date formats and impossible dates,
  currency parsing, enum/header normalisation, timestamp forms incl. naive=IST and IST date rollover,
  effective disposition (override beats later set, latest override wins), bucket priority, schema-order
  rejection reasons and warnings, and end-to-end on generated data: incremental == all-at-once == re-run,
  late-arriving leads de-orphan their events, no plaintext PII in a full DB dump, ledger append-only,
  changed file refused.
- Cross-checked the five reports against a separate from-scratch implementation (in-memory, no DB) on
  seeds 42, 7 and 2026: identical output.

## Results
- Small (seed 42): ingest both drops ≈0.3 s, report ≈0.1 s.
- **Large (seed 42, ~144k lead rows, ~600k event lines): ingest 44 s + report 10 s ≈ 55 s** on a
  2-vCPU Linux VM (stretch goal: < 2 min).
- Seed 42 small: 2057 lead rows, 108 rejected, 57 merged, 122 field warnings; 8745 event lines,
  8 malformed, 237 duplicates, 462 orphans, 18 unknown dispositions.
- Surprising: some `batch_003` leads (drop 2) were called entirely within drop 1's window, so their
  events are orphans until drop 2. Treating linking as a derived view rather than an ingest-time
  decision is what makes this correct.

## Stretch done
- **Performance** (above).
- **Incremental:** drop 2 does not reprocess drop 1's files; only linking and aggregation are
  recomputed at report time. Trade-off: report cost is O(all events). At much larger volume I would
  materialise a `call_facts` table updated per call_id touched by the new drop.

## Production note (100k leads/day)
Run ingest per file as soon as it lands (object-store notification → queue → worker), with the same
file-level idempotency key. Use Postgres with the ledger partitioned by month, `event_id` unique, and
the PII key in a KMS. Build a `call_facts`/`lead_facts` layer incrementally (only call_ids touched by
new events) and run reports from it on a schedule. Backfills = re-ingest files into a fresh schema and
diff reports before swapping. Alert on spikes in orphan, malformed or rejection rates per batch.

## If I had another day
1. Materialised `call_facts` so reports are incremental too; move aggregation to SQL.
2. Postgres via `docker-compose.yml`, with the same tests running against both backends.
3. Property-based tests (hypothesis) for phone/date/decimal parsers.
4. A `batch_retracted` compensating event for removing a bad upload without deleting history.
