# Solution: Assignment A (Interaction Ledger & Reporting Pipeline)

See [`../SUBMISSION.md`](../SUBMISSION.md) for setup, design and decisions.

```
ledger/
├── normalize.py   pure parsing rules: phone, date, decimal, enum, header, timestamps
├── schema.py      workflow schemas, disposition catalogue, row validation
├── pii.py         HMAC phone hashing + Fernet encryption of PII fields
├── db.py          SQLite schema (append-only ledger enforced by triggers)
├── ingest.py      per-file, transactional, idempotent ingestion of drops
├── report.py      derives calls/buckets from the ledger and writes the five reports
└── cli.py         python -m ledger [--db PATH] {ingest DIR... | report --out DIR}
tests/             python -m pytest -q
```
