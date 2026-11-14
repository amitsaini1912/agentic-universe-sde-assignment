from datetime import date
from decimal import Decimal

import pytest

from ledger.normalize import (normalise_header, normalise_phone, parse_date, parse_decimal,
                              parse_enum, parse_ts, to_ist_iso, ist_date)


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
                                 "(+91) 9876543210", "+919876543210", " 98765-43210 "])
def test_phone_valid_formats(raw):
    assert normalise_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["987654321", "5876543210", "022-2345-6789", "N/A",
                                 "+1 415 555 0134", "", "919876543", "1234567890",
                                 "0919876543210"])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", date(2026, 12, 1)), ("01/12/2026", date(2026, 12, 1)),
    ("01-12-2026", date(2026, 12, 1)), ("01-Dec-2026", date(2026, 12, 1)),
    ("01-DEC-2026", date(2026, 12, 1)), ("05/11/2026", date(2026, 11, 5)),  # day-first
])
def test_dates(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "soon", "TBD", "29-Feb-2026",
                                 "01-Foo-2026", "01/12-2026"])
def test_dates_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12000", "12000.00"), ("₹28,397", "28397.00"), ("Rs. 24787", "24787.00"),
    ("Rs 5", "5.00"), ("rs.5", "5.00"), ("INR 18678.3", "18678.30"), ("inr18678.345", "18678.35"),
    ("1,234,567.891", "1234567.89"), ("0", "0.00"),
])
def test_decimal(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "-", "twelve thousand", "-5", "₹-5", "1,23", "1.2.3"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum_and_header():
    assert parse_enum(" HI ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None
    assert normalise_header("  Mobile__No ") == "mobile no"
    assert normalise_header("Insured\t Name") == "insured name"


def test_timestamps_naive_is_ist():
    z = parse_ts("2026-10-03T04:31:00Z")
    off = parse_ts("2026-10-03T10:01:00+05:30")
    naive = parse_ts("2026-10-03 10:01:00")
    assert z == off == naive
    assert to_ist_iso(z) == "2026-10-03T10:01:00+05:30"


def test_ist_date_crosses_midnight():
    # 20:00 UTC is 01:30 IST the next day
    assert ist_date(parse_ts("2026-10-03T20:00:00Z")) == "2026-10-04"
    assert parse_ts("garbage") is None and parse_ts(None) is None
