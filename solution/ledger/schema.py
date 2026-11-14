"""Workflow lead schemas, disposition catalogue, and row validation."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalise_header, normalise_phone, parse_date, parse_decimal, parse_enum


@dataclass
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    default: str | None = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    fields: list[Field]

    @property
    def reference_field(self) -> str:
        return next(f.name for f in self.fields if f.reference)

    @property
    def phone_field(self) -> str:
        return next(f.name for f in self.fields if f.type == "phone")

    def header_map(self, headers: list[str]) -> dict[int, Field]:
        """column index -> Field. First matching column wins if two map to one field."""
        lookup: dict[str, Field] = {}
        for f in self.fields:
            for key in [f.name, *f.aliases]:
                lookup.setdefault(normalise_header(key), f)
        out: dict[int, Field] = {}
        taken: set[str] = set()
        for i, h in enumerate(headers):
            f = lookup.get(normalise_header(h))
            if f and f.name not in taken:
                out[i] = f
                taken.add(f.name)
        return out


@dataclass
class RowResult:
    values: dict[str, object] | None  # normalised values (None if rejected)
    reason: str | None                # rejection reason
    warnings: int = 0


def _convert(f: Field, raw: str):
    if f.type == "string":
        return raw
    if f.type == "phone":
        return normalise_phone(raw)
    if f.type == "date":
        d = parse_date(raw)
        return d.isoformat() if d else None
    if f.type == "decimal":
        v = parse_decimal(raw)
        return str(v) if v is not None else None
    if f.type == "enum":
        return parse_enum(raw, f.values)
    raise ValueError(f"unknown field type {f.type}")


def validate_row(schema: WorkflowSchema, raw: dict[str, str]) -> RowResult:
    """raw: field name -> trimmed cell value ('' = missing). Walk fields in schema order."""
    values: dict[str, object] = {}
    warnings = 0
    for f in schema.fields:
        v = raw.get(f.name, "")
        if v == "":
            if f.required:
                return RowResult(None, f"missing_required:{f.name}")
            values[f.name] = None
            continue
        conv = _convert(f, v)
        if conv is None:
            if f.type == "phone":
                return RowResult(None, "invalid_phone")
            if f.required:
                # Not produced by the current schemas; see SUBMISSION.md.
                return RowResult(None, f"invalid_required:{f.name}")
            warnings += 1
        values[f.name] = conv
    return RowResult(values, None, warnings)


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    out = {}
    for p in sorted(schema_dir.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        fields = [Field(**{k: v for k, v in f.items() if k in Field.__dataclass_fields__})
                  for f in d["fields"]]
        out[d["workflow_id"]] = WorkflowSchema(d["workflow_id"], fields)
    return out


def load_dispositions(schema_dir: Path) -> dict[tuple[str, str], tuple[bool, bool]]:
    """(workflow_id, code) -> (is_terminal, is_success)"""
    out = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[(r["workflow_id"].strip(), r["code"].strip())] = (
                r["is_terminal"].strip().lower() == "true",
                r["is_success"].strip().lower() == "true",
            )
    return out
