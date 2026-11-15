"""CSV validation, ingest transaction and hourly-usage recomputation."""

from __future__ import annotations

import csv
import io
import math
import re
import uuid
from datetime import datetime, timezone

from .db import connect

HOUR = 3600
MAX_ROWS_PER_BATCH = 100_000
REQUIRED_COLUMNS = {"meter_id", "utc_second", "kwh"}

_TS_RE = re.compile(r"^[0-9]+$")


class ValidationError(Exception):
    """The CSV itself is malformed or violates a rule (HTTP 422)."""


class ConflictError(Exception):
    """A row contradicts a reading already stored (HTTP 409)."""


def utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_csv(text: str) -> list[tuple[str, int, float]]:
    """Validate the whole CSV and return (meter_id, utc_second, kwh) rows.

    Nothing is written here; any problem raises ValidationError so the
    caller can reject the batch before touching the database.
    """
    if not text.strip():
        raise ValidationError("empty CSV: a header row and data rows are required")

    reader = csv.DictReader(io.StringIO(text))
    raw_headers = reader.fieldnames or []
    headers = [h.strip() if h else h for h in raw_headers]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise ValidationError(f"missing required column(s): {sorted(missing)}")
    extra = set(headers) - REQUIRED_COLUMNS
    if extra:
        raise ValidationError(f"unknown column(s): {sorted(extra)}")

    rows: list[tuple[str, int, float]] = []
    seen: dict[tuple[str, int], float] = {}
    for lineno, rec in enumerate(reader, start=2):
        if None in rec:  # more fields than headers
            raise ValidationError(f"line {lineno}: wrong number of fields")

        meter = (rec.get("meter_id") or "").strip()
        if not meter:
            raise ValidationError(f"line {lineno}: meter_id is empty")
        if len(meter) > 128:
            raise ValidationError(f"line {lineno}: meter_id exceeds 128 characters")

        ts_raw = (rec.get("utc_second") or "").strip()
        if not _TS_RE.match(ts_raw):
            raise ValidationError(
                f"line {lineno}: utc_second must be a non-negative integer, got {ts_raw!r}")
        ts = int(ts_raw)
        if ts % HOUR != 0:
            raise ValidationError(
                f"line {lineno}: utc_second {ts} is not aligned to a UTC hour")

        kwh_raw = (rec.get("kwh") or "").strip()
        try:
            kwh = float(kwh_raw)
        except ValueError:
            raise ValidationError(f"line {lineno}: kwh is not a number: {kwh_raw!r}")
        if not math.isfinite(kwh) or kwh < 0:
            raise ValidationError(f"line {lineno}: kwh must be a finite number >= 0, got {kwh_raw!r}")

        key = (meter, ts)
        if key in seen:
            if seen[key] != kwh:
                raise ValidationError(
                    f"line {lineno}: conflicting kwh values for meter {meter!r} "
                    f"at {ts} ({utc_iso(ts)}) inside the same batch")
            continue  # identical retransmission inside the batch: keep one copy
        seen[key] = kwh
        rows.append((meter, ts, kwh))
        if len(rows) > MAX_ROWS_PER_BATCH:
            raise ValidationError(f"too many rows (limit {MAX_ROWS_PER_BATCH})")

    if not rows:
        raise ValidationError("CSV contains no data rows")
    return rows


def ingest(rows: list[tuple[str, int, float]]) -> dict:
    """Store one batch and recompute affected hourly usage, atomically.

    The whole batch (readings + batch record + recomputed hours) commits or
    rolls back together, so a failed batch leaves previously stored results
    untouched and concurrent readers never see half-written state.
    """
    batch_id = uuid.uuid4().hex
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = connect()
    inserted = 0
    deduplicated = 0
    hours_written = 0
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialize writers across instances
        conn.execute(
            "INSERT INTO batches (batch_id, received_at, rows_total, rows_inserted,"
            " rows_deduplicated) VALUES (?,?,?,0,0)",
            (batch_id, received_at, len(rows)))

        new_per_meter: dict[str, list[int]] = {}
        for meter, ts, kwh in rows:
            existing = conn.execute(
                "SELECT kwh FROM readings WHERE meter_id = ? AND utc_second = ?",
                (meter, ts)).fetchone()
            if existing is not None:
                if existing["kwh"] != kwh:
                    raise ConflictError(
                        f"meter {meter!r} at {ts} ({utc_iso(ts)}) already stored with "
                        f"kwh={existing['kwh']}, batch carries kwh={kwh}; whole batch rejected")
                deduplicated += 1  # identical retransmission: record only once
                continue
            conn.execute(
                "INSERT INTO readings (meter_id, utc_second, kwh, batch_id) VALUES (?,?,?,?)",
                (meter, ts, kwh, batch_id))
            inserted += 1
            new_per_meter.setdefault(meter, []).append(ts)

        for meter, stamps in new_per_meter.items():
            hours_written += recompute_window(conn, meter, min(stamps), max(stamps))

        conn.execute(
            "UPDATE batches SET rows_inserted = ?, rows_deduplicated = ? WHERE batch_id = ?",
            (inserted, deduplicated, batch_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "batch_id": batch_id,
        "received_at": received_at,
        "rows_total": len(rows),
        "rows_inserted": inserted,
        "rows_deduplicated": deduplicated,
        "meters": sorted(new_per_meter),
        "hourly_usage_updated": hours_written,
    }


def recompute_window(conn, meter: str, tmin: int, tmax: int) -> int:
    """Rebuild hourly usage for the hours touching [tmin, tmax] of one meter.

    The window extends to the neighbouring stored readings on both sides, so
    when a late reading fills a gap the hours before and after it are
    recomputed in the same transaction.
    """
    row = conn.execute(
        "SELECT MAX(utc_second) AS t FROM readings WHERE meter_id = ? AND utc_second < ?",
        (meter, tmin)).fetchone()
    lo = row["t"] if row["t"] is not None else tmin
    row = conn.execute(
        "SELECT MIN(utc_second) AS t FROM readings WHERE meter_id = ? AND utc_second > ?",
        (meter, tmax)).fetchone()
    hi = row["t"] if row["t"] is not None else tmax

    conn.execute(
        "DELETE FROM hourly_usage WHERE meter_id = ? AND hour_start >= ? AND hour_start < ?",
        (meter, lo, hi))

    readings = conn.execute(
        "SELECT utc_second, kwh FROM readings "
        "WHERE meter_id = ? AND utc_second BETWEEN ? AND ? ORDER BY utc_second",
        (meter, lo, hi)).fetchall()

    written = 0
    for prev, cur in zip(readings, readings[1:]):
        t0, k0 = prev["utc_second"], prev["kwh"]
        t1, k1 = cur["utc_second"], cur["kwh"]
        if t1 - t0 == HOUR:
            if k1 >= k0:
                _insert_hour(conn, meter, t0, round(k1 - k0, 6), 1, "ok")
            else:
                _insert_hour(conn, meter, t0, None, 0, "rollback")
            written += 1
        else:
            # Missing readings in between: every hour of the gap is unbillable.
            h = t0
            while h < t1:
                _insert_hour(conn, meter, h, None, 0, "gap")
                written += 1
                h += HOUR
    return written


def _insert_hour(conn, meter: str, hour_start: int, kwh_used, billable: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO hourly_usage (meter_id, hour_start, kwh_used, billable, reason)"
        " VALUES (?,?,?,?,?)",
        (meter, hour_start, kwh_used, billable, reason))
