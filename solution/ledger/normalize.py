"""Pure value-normalisation rules from the spec. No I/O, no DB: easy to unit-test."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

IST = timezone(timedelta(hours=5, minutes=30))

_WS_US = re.compile(r"[\s_]+")


def normalise_header(h: str) -> str:
    """lower-case, trim, collapse every run of '_' or whitespace into one space."""
    return _WS_US.sub(" ", (h or "").strip().lower()).strip()


# ---------------------------------------------------------------- phone
def normalise_phone(raw: str | None) -> str | None:
    """Return '+91XXXXXXXXXX' or None if invalid. Caller decides missing vs invalid."""
    if raw is None:
        return None
    d = re.sub(r"\D", "", str(raw))
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10 and d[0] in "6789":
        return "+91" + d
    return None


# ---------------------------------------------------------------- date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")
_DMONY = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")


def parse_date(raw: str) -> date | None:
    s = raw.strip()
    try:
        if m := _ISO.match(s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DMY.match(s):
            # DD/MM/YYYY or DD-MM-YYYY; mixed separators are rejected below
            if ("/" in s) and ("-" in s):
                return None
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := _DMONY.match(s):
            mon = _MONTHS.get(m[2].lower())
            return date(int(m[3]), mon, int(m[1])) if mon else None
    except ValueError:  # impossible date such as 31/02
        return None
    return None


# ---------------------------------------------------------------- decimal
_CURRENCY = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+(?:\.\d+)?$")


def parse_decimal(raw: str) -> Decimal | None:
    s = _CURRENCY.sub("", raw.strip(), count=1).strip()
    if not _NUMBER.match(s):  # also rejects negatives
        return None
    try:
        v = Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- enum
def parse_enum(raw: str, values: list[str]) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


# ---------------------------------------------------------------- timestamps
def parse_ts(raw) -> datetime | None:
    """UTC 'Z', explicit offset, or naive (= IST). Returns aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def to_ist_iso(dt_utc: datetime) -> str:
    return dt_utc.astimezone(IST).isoformat(timespec="seconds")


def ist_date(dt_utc: datetime) -> str:
    return dt_utc.astimezone(IST).date().isoformat()
