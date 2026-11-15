"""End-to-end tests against a temporary SQLite file (WAL, like production)."""

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


def hourly(client: TestClient, meter: str) -> dict[int, dict]:
    resp = client.get("/hourly-usage", params={"meter_id": meter})
    assert resp.status_code == 200
    return {r["hour_start"]: r for r in resp.json()}


def readings(client: TestClient, meter: str) -> list[dict]:
    resp = client.get("/readings", params={"meter_id": meter})
    assert resp.status_code == 200
    return resp.json()


# ---------- baseline & hourly computation ----------

def test_first_reading_is_baseline_only(client):
    resp = post_file(client, csv_of([("m1", hour(0), 100.0)]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["rows_inserted"] == 1
    assert hourly(client, "m1") == {}
    assert len(readings(client, "m1")) == 1


def test_adjacent_hours_produce_billable_usage(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.5),
                              ("m1", hour(2), 104.0)]))
    hours = hourly(client, "m1")
    assert hours[hour(0)]["kwh_used"] == 1.5 and hours[hour(0)]["billable"]
    assert hours[hour(1)]["kwh_used"] == 2.5 and hours[hour(1)]["billable"]
    assert hours[hour(0)]["reason"] == "ok"
    assert hour(2) not in hours  # latest reading is the baseline for the next hour


def test_gap_hours_are_unbillable(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(3), 109.0)]))
    hours = hourly(client, "m1")
    for n in (0, 1, 2):
        assert hours[hour(n)]["billable"] is False
        assert hours[hour(n)]["reason"] == "gap"
        assert hours[hour(n)]["kwh_used"] is None


def test_late_readings_fill_gap_and_update_neighbouring_hours(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0),
                              ("m1", hour(4), 107.0)]))
    assert hourly(client, "m1")[hour(1)]["reason"] == "gap"

    resp = post_file(client, csv_of([("m1", hour(2), 103.0), ("m1", hour(3), 105.0)]))
    assert resp.status_code == 201
    hours = hourly(client, "m1")
    assert hours[hour(1)]["kwh_used"] == 2.0 and hours[hour(1)]["billable"]
    assert hours[hour(2)]["kwh_used"] == 2.0 and hours[hour(2)]["billable"]
    assert hours[hour(3)]["kwh_used"] == 2.0 and hours[hour(3)]["billable"]
    assert hours[hour(0)]["kwh_used"] == 1.0  # untouched hour still correct


def test_rollback_hour_is_unbillable(client):
    post_file(client, csv_of([("m1", hour(0), 50.0), ("m1", hour(1), 48.0),
                              ("m1", hour(2), 49.5)]))
    hours = hourly(client, "m1")
    assert hours[hour(0)]["billable"] is False
    assert hours[hour(0)]["reason"] == "rollback"
    assert hours[hour(0)]["kwh_used"] is None
    assert hours[hour(1)]["kwh_used"] == 1.5 and hours[hour(1)]["billable"]


# ---------- idempotency & conflicts ----------

def test_identical_retransmission_recorded_once(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    resp = post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    assert resp.status_code == 201
    body = resp.json()
    assert body["rows_inserted"] == 0
    assert body["rows_deduplicated"] == 2
    assert len(readings(client, "m1")) == 2  # still exactly one copy each
    assert hourly(client, "m1")[hour(0)]["kwh_used"] == 1.0


def test_conflicting_value_rejects_whole_batch_and_keeps_prior_state(client):
    post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(1), 101.0)]))
    before_readings = readings(client, "m1")
    before_hours = hourly(client, "m1")
    before_batches = client.get("/batches").json()

    resp = post_file(client, csv_of([("m1", hour(1), 999.0), ("m1", hour(2), 105.0)]))
    assert resp.status_code == 409
    assert "999" in resp.json()["detail"]

    assert readings(client, "m1") == before_readings      # no partial writes
    assert hourly(client, "m1") == before_hours
    assert client.get("/batches").json() == before_batches  # failed batch leaves no trace


def test_conflicting_duplicates_inside_one_batch_rejected(client):
    resp = post_file(client, csv_of([("m1", hour(0), 100.0), ("m1", hour(0), 100.5)]))
    assert resp.status_code == 422
    assert readings(client, "m1") == []


# ---------- validation ----------

@pytest.mark.parametrize("row,fragment", [
    ("m1,1793491201,1.0", "aligned"),          # not a whole hour
    ("m1,1793491200,-1.0", ">= 0"),            # negative kwh
    ("m1,1793491200,abc", "not a number"),
    ("m1,1793491200,nan", "finite"),
    (",1793491200,1.0", "meter_id is empty"),
    ("m1,not-a-number,1.0", "non-negative integer"),
])
def test_invalid_rows_rejected(client, row, fragment):
    resp = post_file(client, f"meter_id,utc_second,kwh\n{row}\n")
    assert resp.status_code == 422
    assert fragment in resp.json()["detail"]
    assert readings(client, "m1") == []


def test_missing_and_unknown_columns_rejected(client):
    assert post_file(client, "meter_id,kwh\nm1,1.0\n").status_code == 422
    assert post_file(client, "meter_id,utc_second,kwh,extra\nm1,1793491200,1.0,x\n").status_code == 422


def test_raw_text_csv_body_accepted(client):
    resp = client.post("/batches", content=csv_of([("m1", hour(0), 1.0)]),
                       headers={"content-type": "text/csv"})
    assert resp.status_code == 201, resp.text
    assert len(readings(client, "m1")) == 1


def test_query_window_filters_and_validation(client):
    post_file(client, csv_of([("m1", hour(n), 100.0 + n) for n in range(5)]))
    resp = client.get("/hourly-usage",
                      params={"meter_id": "m1", "from_second": hour(1), "to_second": hour(2)})
    assert [r["hour_start"] for r in resp.json()] == [hour(1), hour(2)]
    assert client.get("/readings", params={"meter_id": "m1",
                                           "from_second": 1793491201}).status_code == 422
    assert client.get("/readings", params={"meter_id": "m1", "from_second": hour(3),
                                           "to_second": hour(1)}).status_code == 422


# ---------- concurrency: two writers, one shared database file ----------

def test_concurrent_ingests_same_meter_all_survive(client):
    # Two "instances" (threads, separate connections) backfill the same meter
    # with disjoint hours at the same time; every successful batch must persist
    # and hourly usage must match the final readings.
    batches = [
        [("m1", hour(0), 100.0), ("m1", hour(1), 101.0)],
        [("m1", hour(2), 103.0), ("m1", hour(3), 106.0)],
        [("m1", hour(4), 110.0), ("m1", hour(5), 115.0)],
        [("m1", hour(6), 121.0), ("m1", hour(7), 128.0)],
    ]
    results: list = [None] * len(batches)

    def worker(i, rows):
        results[i] = post_file(client, csv_of(rows))

    threads = [threading.Thread(target=worker, args=(i, b)) for i, b in enumerate(batches)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results)
    assert sum(r.json()["rows_inserted"] for r in results) == 8
    assert len(readings(client, "m1")) == 8

    hours = hourly(client, "m1")
    expected = {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0, 4: 5.0, 5: 6.0, 6: 7.0}
    for n, want in expected.items():
        assert hours[hour(n)]["kwh_used"] == want, f"hour {n}"
        assert hours[hour(n)]["billable"]
