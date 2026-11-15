"""CSV validation, ingest transaction and hourly-usage recomputation."""

from __future__ import annotations

import csv
import io
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

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

    Identical retransmissions inside one batch collapse to one row. Rows that
    carry *different* values for the same (meter, utc_second) are kept as
    separate rows: a registered meter swap legitimately has two codes at the
    swap instant, so the final decision needs the database and is made inside
    the ingest transaction (ValidationError for an ordinary contradiction).
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
        if key in seen and seen[key] == kwh:
            continue  # identical retransmission inside the batch: keep one copy
        seen.setdefault(key, kwh)
        rows.append((meter, ts, kwh))
        if len(rows) > MAX_ROWS_PER_BATCH:
            raise ValidationError(f"too many rows (limit {MAX_ROWS_PER_BATCH})")

    if not rows:
        raise ValidationError("CSV contains no data rows")
    return rows


@dataclass(frozen=True)
class Replacement:
    """A meter swap registered from a maintenance work order.

    The gateway keeps reporting under the old ``meter_id``; these two codes
    are the endpoints the work order contributes at ``second``. Two work
    orders are the same iff meter, instant and both codes match.
    """

    meter_id: str
    second: int
    old_last_kwh: float
    new_initial_kwh: float

    def as_dict(self) -> dict:
        return {
            "meter_id": self.meter_id,
            "replacement_second": self.second,
            "replacement_utc": utc_iso(self.second),
            "old_meter_last_kwh": self.old_last_kwh,
            "new_meter_initial_kwh": self.new_initial_kwh,
        }


def register_replacement(meter_id: str, second: int, old_last_kwh: float,
                         new_initial_kwh: float) -> dict:
    """Register the single swap point of one meter, atomically.

    Re-sending an identical work order is a no-op and returns the original
    registration (``created=False``). A different work order for the same
    meter is rejected with ConflictError. Readings that contradict both work
    order codes at the swap instant are rejected as well. The swap and the
    recomputed hours around it commit or roll back together; raw readings are
    never modified.
    """
    registered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    probe = Replacement(meter_id, second, old_last_kwh, new_initial_kwh)
    stored_registered_at = registered_at

    conn = connect()
    hours_written = 0
    created = False
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialize writers across instances
        existing = _load_replacement(conn, meter_id)
        if existing is not None:
            if existing != probe:
                raise ConflictError(
                    f"meter {meter_id!r} already has a meter swap registered at "
                    f"{existing.second} ({utc_iso(existing.second)}) with old_last_kwh="
                    f"{existing.old_last_kwh}, new_initial_kwh={existing.new_initial_kwh}; "
                    f"a different work order was rejected")
            stored_registered_at = conn.execute(
                "SELECT registered_at FROM meter_replacements WHERE meter_id = ?",
                (meter_id,)).fetchone()["registered_at"]
        else:
            stored = conn.execute(
                "SELECT kwh FROM readings WHERE meter_id = ? AND utc_second = ?",
                (meter_id, second)).fetchone()
            if stored is not None and float(stored["kwh"]) not in (
                    old_last_kwh, new_initial_kwh):
                raise ConflictError(
                    f"meter {meter_id!r} already stores kwh={stored['kwh']} at {second} "
                    f"({utc_iso(second)}), which matches neither the old-meter last code "
                    f"({old_last_kwh}) nor the new-meter initial code ({new_initial_kwh}); "
                    f"work order rejected")
            conn.execute(
                "INSERT INTO meter_replacements (meter_id, replacement_second,"
                " old_meter_last_kwh, new_meter_initial_kwh, registered_at)"
                " VALUES (?,?,?,?,?)",
                (meter_id, second, old_last_kwh, new_initial_kwh, registered_at))
            created = True
            hours_written = recompute_window(conn, meter_id, second, second, probe)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "meter_id": meter_id,
        "replacement_second": second,
        "replacement_utc": utc_iso(second),
        "old_meter_last_kwh": old_last_kwh,
        "new_meter_initial_kwh": new_initial_kwh,
        "registered_at": stored_registered_at,
        "created": created,
        "hourly_usage_updated": hours_written,
    }


def list_replacements() -> list[dict]:
    """List registered meter swap work orders, oldest swap instant first."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT meter_id, replacement_second, old_meter_last_kwh,"
            " new_meter_initial_kwh, registered_at FROM meter_replacements"
            " ORDER BY replacement_second, meter_id").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        item = _replacement_from_row(row).as_dict()
        item["registered_at"] = row["registered_at"]
        out.append(item)
    return out


def _replacement_from_row(row) -> Replacement:
    return Replacement(row["meter_id"], row["replacement_second"],
                       row["old_meter_last_kwh"], row["new_meter_initial_kwh"])


def _load_replacement(conn, meter_id: str) -> Optional[Replacement]:
    row = conn.execute(
        "SELECT meter_id, replacement_second, old_meter_last_kwh,"
        " new_meter_initial_kwh FROM meter_replacements"
        " WHERE meter_id = ?", (meter_id,)).fetchone()
    return _replacement_from_row(row) if row is not None else None


def _load_replacements(conn, meter_ids: list[str] | set[str]) -> dict[str, Replacement]:
    ids = sorted(set(meter_ids))
    found: dict[str, Replacement] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            "SELECT meter_id, replacement_second, old_meter_last_kwh,"
            " new_meter_initial_kwh FROM meter_replacements"
            f" WHERE meter_id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
        for row in rows:
            found[row["meter_id"]] = _replacement_from_row(row)
    return found


def _matching_code(replacement: Replacement, kwh: float) -> bool:
    return kwh in (replacement.old_last_kwh, replacement.new_initial_kwh)


def _reject_batch_internal_contradictions(
        rows: list[tuple[str, int, float]],
        replacements: dict[str, Replacement]) -> None:
    """Reject a batch that carries two different values for one (meter, hour).

    The only exception is a registered swap instant: the old meter's last code
    and the new meter's initial code may arrive together in one batch (the
    gateway retransmitting both sides), and that is treated as a retransmission
    of the work order values rather than a contradiction.
    """
    values_by_key: dict[tuple[str, int], set[float]] = {}
    for meter, ts, kwh in rows:
        values_by_key.setdefault((meter, ts), set()).add(kwh)

    for (meter, ts), values in values_by_key.items():
        if len(values) == 1:
            continue
        replacement = replacements.get(meter)
        if (replacement is not None and ts == replacement.second
                and values <= {replacement.old_last_kwh, replacement.new_initial_kwh}):
            continue
        shown = ", ".join(str(v) for v in sorted(values))
        raise ValidationError(
            f"conflicting kwh values for meter {meter!r} at {ts} "
            f"({utc_iso(ts)}) inside the same batch: {shown}")


def ingest(rows: list[tuple[str, int, float]]) -> dict:
    """Store one batch and recompute affected hourly usage, atomically.

    The whole batch (readings + batch record + recomputed hours) commits or
    rolls back together, so a failed batch leaves previously stored results
    untouched and concurrent readers never see half-written state.

    Once a meter swap is registered for a meter, readings stamped exactly at
    the swap instant are accepted only with the work order's old-last or
    new-initial code; anything else rejects the whole batch (409). Identical
    retransmissions are still deduplicated and never add raw readings.
    """
    batch_id = uuid.uuid4().hex
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn = connect()
    inserted = 0
    deduplicated = 0
    hours_written = 0
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialize writers across instances
        replacements = _load_replacements(conn, [m for m, _, _ in rows])

        _reject_batch_internal_contradictions(rows, replacements)

        conn.execute(
            "INSERT INTO batches (batch_id, received_at, rows_total, rows_inserted,"
            " rows_deduplicated) VALUES (?,?,?,0,0)",
            (batch_id, received_at, len(rows)))

        new_per_meter: dict[str, list[int]] = {}
        for meter, ts, kwh in rows:
            replacement = replacements.get(meter)
            if replacement is not None and ts == replacement.second:
                # The work order already defines this instant. A matching code
                # is a retransmission of work-order values: accepted, counted
                # as deduplicated, and never written to raw readings (whether
                # or not a gateway reading is already stored there).
                if not _matching_code(replacement, kwh):
                    raise ConflictError(
                        f"meter {meter!r} has a registered meter swap at {ts} "
                        f"({utc_iso(ts)}) with codes old_last_kwh={replacement.old_last_kwh} "
                        f"and new_initial_kwh={replacement.new_initial_kwh}; batch carries "
                        f"kwh={kwh}, so the whole batch is rejected")
                deduplicated += 1
                continue

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
            hours_written += recompute_window(conn, meter, min(stamps), max(stamps),
                                              replacements.get(meter))

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


@dataclass(frozen=True)
class Point:
    """One endpoint in a meter's timeline; ``kwh`` must compare equal."""

    utc_second: int
    kwh: float


def recompute_window(conn, meter: str, tmin: int, tmax: int,
                     replacement: Optional[Replacement] = None) -> int:
    """Rebuild hourly usage for the hours touching [tmin, tmax] of one meter.

    The window extends to the neighbouring stored readings on both sides, so
    when a late reading fills a gap the hours before and after it are
    recomputed in the same transaction.

    When the meter has a registered swap inside the window, the swap instant
    joins the timeline as a visible endpoint even if the gateway never
    reported that hour: the hour *ending* at the swap uses the old meter's
    last code, the hour *starting* at the swap uses the new meter's initial
    code. With no visible reading between the neighbours the windows are still
    contiguous 1-hour steps, so no gap is created by construction. Raw
    readings are untouched.
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
    points = [Point(r["utc_second"], r["kwh"]) for r in readings]
    if replacement is not None and lo <= replacement.second <= hi:
        # The swap instant contributes both endpoints. The old last code is
        # placed before any stored reading at that second and the new initial
        # code after it, so the hour ending there always compares against the
        # old meter and the hour starting there always against the new one,
        # whichever code (if any) the gateway reported. The 0-second steps
        # between same-instant endpoints are never treated as a gap.
        before = [p for p in points if p.utc_second < replacement.second]
        at = [p for p in points if p.utc_second == replacement.second]
        after = [p for p in points if p.utc_second > replacement.second]
        points = (before
                  + [Point(replacement.second, replacement.old_last_kwh)]
                  + at
                  + [Point(replacement.second, replacement.new_initial_kwh)]
                  + after)

    return _write_hours(conn, meter, points)


def _write_hours(conn, meter: str, points: list[Point]) -> int:
    written = 0
    for prev, cur in zip(points, points[1:]):
        t0, k0 = prev.utc_second, prev.kwh
        t1, k1 = cur.utc_second, cur.kwh
        if t1 - t0 == 0:
            # Same instant: the meter was swapped, so codes belong to two
            # different meters and must never be compared.
            continue
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
