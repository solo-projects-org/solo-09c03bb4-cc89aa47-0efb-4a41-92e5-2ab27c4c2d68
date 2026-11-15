"""Meter swap work orders: registration, hourly boundaries and concurrency."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

BASE = 1793491200  # 2026-11-01T00:00:00Z, hour-aligned
H = 3600


def hour(n: int) -> int:
    return BASE + n * H


def csv_of(rows: list[tuple[str, int, float]]) -> str:
    lines = ["meter_id,utc_second,kwh"]
    lines += [f"{m},{t},{k}" for m, t, k in rows]
    return "\n".join(lines) + "\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from app.main import app
    with TestClient(app) as c:
        yield c


def post_file(client: TestClient, text: str):
    return client.post("/batches", files={"file": ("batch.csv", text, "text/csv")})


def register(client: TestClient, meter: str, second: int, old: float, new: float):
    return client.post("/meter-replacements", json={
        "meter_id": meter,
        "replacement_second": second,
        "old_meter_last_kwh": old,
        "new_meter_initial_kwh": new,
    })


def hourly(client: TestClient, meter: str) -> dict[int, dict]:
    resp = client.get("/hourly-usage", params={"meter_id": meter})
    assert resp.status_code == 200
    return {r["hour_start"]: r for r in resp.json()}


def readings(client: TestClient, meter: str) -> list[dict]:
    resp = client.get("/readings", params={"meter_id": meter})
    assert resp.status_code == 200
    return resp.json()


def usage(hours: dict[int, dict], n: int):
    row = hours[hour(n)]
    return row["kwh_used"], row["billable"], row["reason"]


# ---------- registration semantics ----------

def test_register_returns_work_order_and_identical_resend_is_idempotent(client):
    resp = register(client, "m1", hour(2), 100.0, 40.0)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["replacement_second"] == hour(2)
    assert body["replacement_utc"] == "2026-11-01T02:00:00Z"
    assert body["old_meter_last_kwh"] == 100.0
    assert body["new_meter_initial_kwh"] == 40.0

    again = register(client, "m1", hour(2), 100.0, 40.0)
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert len(client.get("/meter-replacements").json()) == 1


def test_different_work_order_for_same_meter_rejected_without_change(client):
    register(client, "m1", hour(2), 100.0, 40.0)
    before = client.get("/meter-replacements").json()

    resp = register(client, "m1", hour(3), 100.0, 40.0)
    assert resp.status_code == 409
    resp = register(client, "m1", hour(2), 101.0, 40.0)
    assert resp.status_code == 409
    resp = register(client, "m1", hour(2), 100.0, 41.0)
    assert resp.status_code == 409
    assert client.get("/meter-replacements").json() == before


def test_unregistered_meter_allows_later_registration(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 100.0)]))
    assert register(client, "m1", hour(2), 100.0, 40.0).status_code == 201


def test_registration_rejected_when_stored_reading_matches_neither_code(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 77.0)]))
    before_readings = readings(client, "m1")
    before_hours = hourly(client, "m1")

    resp = register(client, "m1", hour(2), 100.0, 40.0)
    assert resp.status_code == 409
    assert "77" in resp.json()["detail"]
    assert client.get("/meter-replacements").json() == []
    assert readings(client, "m1") == before_readings
    assert hourly(client, "m1") == before_hours


def test_registration_accepted_when_stored_reading_matches_either_code(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 100.0)]))
    assert register(client, "m1", hour(2), 100.0, 40.0).status_code == 201
    # a reading with the new code at the swap instant is equally acceptable
    assert register(client, "m2", hour(2), 100.0, 40.0).status_code == 201


def test_unaligned_or_invalid_work_order_rejected(client):
    resp = register(client, "m1", hour(2) + 1, 100.0, 40.0)
    assert resp.status_code == 422
    resp = client.post("/meter-replacements", json={
        "meter_id": "", "replacement_second": hour(2),
        "old_meter_last_kwh": 1.0, "new_meter_initial_kwh": 2.0})
    assert resp.status_code == 422
    resp = client.post("/meter-replacements", json={
        "meter_id": "m1", "replacement_second": hour(2),
        "old_meter_last_kwh": -1.0, "new_meter_initial_kwh": 2.0})
    assert resp.status_code == 422
    assert client.get("/meter-replacements").json() == []


# ---------- hourly boundaries without a reading at the swap instant ----------

def test_swap_instant_without_gateway_reading_splits_the_hours(client):
    # Gateway kept the old meter id but never reported the 02:00 hour.
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 95.0),
                              ("m1", hour(3), 45.0), ("m1", hour(4), 48.0)]))
    assert register(client, "m1", hour(2), 100.0, 40.0).status_code == 201

    hours = hourly(client, "m1")
    assert usage(hours, 0) == (5.0, True, "ok")
    # hour ending at the swap uses the old meter's last code
    assert usage(hours, 1) == (5.0, True, "ok")
    # hour starting at the swap uses the new meter's initial code
    assert usage(hours, 2) == (5.0, True, "ok")
    assert usage(hours, 3) == (3.0, True, "ok")
    # raw readings are untouched: no synthetic row at the swap instant
    assert [r["utc_second"] for r in readings(client, "m1")] == [
        hour(0), hour(1), hour(3), hour(4)]


def test_work_order_endpoints_do_not_hide_gaps_or_rollbacks(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(4), 30.0)]))
    assert register(client, "m1", hour(2), 80.0, 40.0).status_code == 201

    hours = hourly(client, "m1")
    # The work order codes only act as endpoints; missing hours stay gap.
    assert usage(hours, 0) == (None, False, "gap")        # 90 .. old last at 02:00
    assert usage(hours, 1) == (None, False, "gap")        # ... still 2 hours away
    assert usage(hours, 2) == (None, False, "gap")        # 02:00 .. next reading 04
    assert usage(hours, 3) == (None, False, "gap")

    # A real rollback across one hour, with the old meter code as the endpoint.
    post_file(client, csv_of([("m2", hour(0), 90.0), ("m2", hour(2), 45.0)]))
    assert register(client, "m2", hour(1), 80.0, 40.0).status_code == 201
    hours = hourly(client, "m2")
    assert usage(hours, 0) == (None, False, "rollback")   # 90 -> old last 80
    assert usage(hours, 1) == (5.0, True, "ok")           # new initial 40 -> 45


# ---------- readings reported at the swap instant ----------

def test_gateway_reported_old_code_at_swap_instant(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 95.0),
                              ("m1", hour(2), 100.0), ("m1", hour(3), 45.0)]))
    assert register(client, "m1", hour(2), 100.0, 40.0).status_code == 201

    hours = hourly(client, "m1")
    assert usage(hours, 1) == (5.0, True, "ok")
    # the hour starting at the swap must use the new initial code, never the
    # stored old code at the same second
    assert usage(hours, 2) == (5.0, True, "ok")
    assert len(readings(client, "m1")) == 4


def test_gateway_reported_new_code_at_swap_instant(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 95.0),
                              ("m1", hour(2), 40.0), ("m1", hour(3), 45.0)]))
    assert register(client, "m1", hour(2), 100.0, 40.0).status_code == 201

    hours = hourly(client, "m1")
    assert usage(hours, 1) == (5.0, True, "ok")    # 95 -> old last 100
    assert usage(hours, 2) == (5.0, True, "ok")    # new initial 40 -> 45


def test_registration_recomputes_hours_previously_computed_without_work_order(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 95.0),
                              ("m1", hour(2), 40.0), ("m1", hour(3), 45.0)]))
    # before registration the raw values look like a rollback at hour 1
    assert usage(hourly(client, "m1"), 1) == (None, False, "rollback")

    register(client, "m1", hour(2), 100.0, 40.0)
    assert usage(hourly(client, "m1"), 1) == (5.0, True, "ok")
    assert usage(hourly(client, "m1"), 2) == (5.0, True, "ok")


# ---------- retransmissions after registration ----------

def test_retransmitting_old_code_at_swap_adds_no_raw_reading(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 100.0),
                              ("m1", hour(3), 45.0)]))
    register(client, "m1", hour(2), 100.0, 40.0)
    before = readings(client, "m1")

    resp = post_file(client, csv_of([("m1", hour(2), 100.0)]))
    assert resp.status_code == 201, resp.text
    assert resp.json()["rows_inserted"] == 0
    assert resp.json()["rows_deduplicated"] == 1
    assert readings(client, "m1") == before


def test_batch_with_both_work_order_codes_accepted_as_retransmission(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 100.0),
                              ("m1", hour(3), 45.0)]))
    register(client, "m1", hour(2), 100.0, 40.0)
    before = readings(client, "m1")

    resp = post_file(client, csv_of([("m1", hour(2), 100.0), ("m1", hour(2), 40.0)]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rows_inserted"] == 0
    assert body["rows_deduplicated"] == 2
    assert readings(client, "m1") == before
    assert usage(hourly(client, "m1"), 2) == (5.0, True, "ok")


def test_first_upload_with_both_codes_after_registration_adds_no_raw_row_at_swap(client):
    register(client, "m1", hour(2), 100.0, 40.0)
    resp = post_file(client, csv_of([("m1", hour(1), 95.0),
                                     ("m1", hour(2), 100.0), ("m1", hour(2), 40.0),
                                     ("m1", hour(3), 45.0)]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rows_inserted"] == 2          # only the ordinary hours
    assert body["rows_deduplicated"] == 2      # both work order codes
    # the registered swap instant never gains a raw reading
    assert [r["utc_second"] for r in readings(client, "m1")] == [hour(1), hour(3)]

    hours = hourly(client, "m1")
    assert usage(hours, 1) == (5.0, True, "ok")   # 95 -> old last 100
    assert usage(hours, 2) == (5.0, True, "ok")   # new initial 40 -> 45


def test_other_value_at_swap_instant_rejects_whole_batch(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(2), 100.0),
                              ("m1", hour(3), 45.0)]))
    register(client, "m1", hour(2), 100.0, 40.0)
    before_readings = readings(client, "m1")
    before_hours = hourly(client, "m1")
    before_batches = client.get("/batches").json()

    resp = post_file(client, csv_of([("m1", hour(2), 99.0), ("m1", hour(4), 50.0)]))
    assert resp.status_code == 409
    assert "99" in resp.json()["detail"]
    assert readings(client, "m1") == before_readings
    assert hourly(client, "m1") == before_hours
    assert client.get("/batches").json() == before_batches


def test_dedup_rules_unchanged_at_other_instants(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 91.0)]))
    register(client, "m1", hour(2), 100.0, 40.0)

    same = post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 91.0)]))
    assert same.status_code == 201
    assert same.json()["rows_deduplicated"] == 2

    other = post_file(client, csv_of([("m1", hour(1), 92.0), ("m1", hour(3), 50.0)]))
    assert other.status_code == 409
    assert len(readings(client, "m1")) == 2


def test_late_reading_far_from_swap_does_not_rewrite_unrelated_hours(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 91.0)]))
    # swap far in the future relative to the readings that follow it
    register(client, "m1", hour(50), 500.0, 5.0)
    before = hourly(client, "m1")
    assert usage(before, 0) == (1.0, True, "ok")

    resp = post_file(client, csv_of([("m1", hour(5), 95.0)]))
    assert resp.status_code == 201, resp.text
    # hours 1..4 (the gap from the last reading up to hour 5) are rewritten;
    # nothing up to the far-away swap is touched
    assert resp.json()["hourly_usage_updated"] == 4

    hours = hourly(client, "m1")
    for n in (0, 1):
        assert hours[hour(n)] == before[hour(n)]
    # the hour between the last reading and hour 5 is still a gap
    assert usage(hours, 1) == (None, False, "gap")
    assert usage(hours, 4) == (None, False, "gap")


# ---------- concurrency across two instances ----------

def _run_concurrently(calls):
    results = [None] * len(calls)

    def worker(i, fn):
        results[i] = fn()

    threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_identical_work_orders_register_once(client):
    results = _run_concurrently(
        [lambda: register(client, "m1", hour(2), 100.0, 40.0) for _ in range(4)])
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 200, 200, 201], [r.text for r in results]
    assert len(client.get("/meter-replacements").json()) == 1


def test_concurrent_conflicting_work_orders_only_one_accepted(client):
    results = _run_concurrently([
        lambda: register(client, "m1", hour(2), 100.0, 40.0),
        lambda: register(client, "m1", hour(2), 101.0, 41.0),
    ])
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]

    stored = client.get("/meter-replacements").json()
    assert len(stored) == 1
    hours = hourly(client, "m1")
    # whichever work order won, the derived hours reflect it and nothing else
    assert hours == {}


def test_concurrent_conflicting_work_order_and_reading_never_both_accepted(client):
    post_file(client, csv_of([("m1", hour(0), 90.0), ("m1", hour(1), 95.0)]))
    results = _run_concurrently([
        lambda: register(client, "m1", hour(2), 100.0, 40.0),
        lambda: post_file(client, csv_of([("m1", hour(2), 77.0)])),
    ])
    registration, batch = results
    codes = (registration.status_code, batch.status_code)

    # Either the reading landed first (registration sees 77 -> 409) or the
    # work order landed first (batch sees 77 contradicting both codes -> 409).
    assert codes in ((201, 409), (409, 201)), [r.text for r in results]
    stored_readings = readings(client, "m1")
    stored_work_orders = client.get("/meter-replacements").json()

    if codes == (201, 409):
        assert len(stored_work_orders) == 1
        assert [r["utc_second"] for r in stored_readings] == [hour(0), hour(1)]
    else:
        assert stored_work_orders == []
        assert [r["utc_second"] for r in stored_readings] == [hour(0), hour(1), hour(2)]
