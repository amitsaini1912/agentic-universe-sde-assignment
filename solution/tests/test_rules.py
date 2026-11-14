"""Effective disposition, bucket logic and row validation."""
from datetime import datetime, timezone
from pathlib import Path

from ledger.report import Call, Event, bucket_for
from ledger.schema import load_schemas, validate_row

SCHEMAS = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"
CAT = {("w", "renewed"): (True, True), ("w", "callback_requested"): (False, False),
       ("w", "not_interested"): (True, False)}


def ev(eid, etype, hour, code=None):
    return Event(eid, etype, "w", "c1", datetime(2026, 10, 1, hour, tzinfo=timezone.utc), None,
                 {"disposition_code": code} if code else {})


def call(**kw):
    return Call("c1", 1, datetime(2026, 10, 1, tzinfo=timezone.utc), **kw)


def test_effective_disposition_latest_set():
    c = call(sets=[ev("e2", "disposition.set", 5, "renewed"),
                   ev("e1", "disposition.set", 3, "callback_requested")])
    assert c.effective_disposition == "renewed"


def test_override_beats_later_set_and_latest_override_wins():
    c = call(sets=[ev("e1", "disposition.set", 9, "renewed")],
             overrides=[ev("e3", "disposition.override", 4, "not_interested"),
                        ev("e2", "disposition.override", 2, "callback_requested")])
    assert c.effective_disposition == "not_interested"


def test_no_disposition():
    assert call().effective_disposition is None


def test_buckets_in_priority_order():
    assert bucket_for([], CAT, "w") == "yet_to_reach_out"
    failed = call(telephony_failed=True)
    assert bucket_for([failed], CAT, "w") == "in_progress_telephony_failed"
    assert bucket_for([failed, call()], CAT, "w") == "in_progress_dialed"
    done = call(telephony_failed=True, sets=[ev("e1", "disposition.set", 1, "renewed")])
    assert bucket_for([failed, done], CAT, "w") == "completed"
    cb = call(sets=[ev("e1", "disposition.set", 1, "callback_requested")])
    assert bucket_for([cb], CAT, "w") == "in_progress_dialed"


def test_override_can_un_complete_a_call():
    c = call(sets=[ev("e1", "disposition.set", 1, "renewed")],
             overrides=[ev("e2", "disposition.override", 2, "callback_requested")])
    assert bucket_for([c], CAT, "w") == "in_progress_dialed"


def test_validation_first_failure_in_schema_order():
    s = load_schemas(SCHEMAS)["motor_renewal"]
    base = {"customer_name": "A", "phone": "9876543210", "policy_number": "P1",
            "policy_expiry": "", "premium_amount": "", "preferred_language": ""}
    assert validate_row(s, {**base, "customer_name": "", "phone": "bad"}).reason == \
        "missing_required:customer_name"
    assert validate_row(s, {**base, "phone": "bad", "policy_number": ""}).reason == "invalid_phone"
    assert validate_row(s, {**base, "phone": ""}).reason == "missing_required:phone"
    r = validate_row(s, {**base, "policy_expiry": "31/02/2026", "premium_amount": "N/A",
                         "preferred_language": "Hindi"})
    assert r.reason is None and r.warnings == 3
    assert r.values["phone"] == "+919876543210" and r.values["policy_expiry"] is None
