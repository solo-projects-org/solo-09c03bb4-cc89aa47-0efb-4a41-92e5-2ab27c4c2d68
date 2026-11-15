#!/usr/bin/env bash
# End-to-end demo against the two docker-compose instances.
# Usage: ./scripts/demo.sh   (expects api-a on :8001 and api-b on :8002)
set -euo pipefail

API_A="${API_A:-http://localhost:8001}"
API_B="${API_B:-http://localhost:8002}"
SAMPLES="$(cd "$(dirname "$0")/.." && pwd)/samples"
METER="meter-east-01"
T0=1793491200   # 2026-11-01T00:00:00Z
T5=1793509200   # 2026-11-01T05:00:00Z

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# post_batch <api> <csv-file>: prints "HTTP <code>" plus the pretty JSON body
post_batch() {
  local out
  out="$(curl -s -w '\n%{http_code}' -F "file=@$2;type=text/csv" "$1/batches")"
  echo "  HTTP $(echo "$out" | tail -1)"
  echo "$out" | head -n -1 | jq . | sed 's/^/  /'
}

query_hours() {
  curl -s "$1/hourly-usage?meter_id=$2&from_second=$T0&to_second=$T5" |
    jq -r '.[] | "  \(.hour_start_utc)  billable=\(.billable)  kwh_used=\(.kwh_used)  reason=\(.reason)"'
}

say "0. wait for both instances"
for api in "$API_A" "$API_B"; do
  for i in $(seq 1 60); do
    curl -sf "$api/health" >/dev/null 2>&1 && break
    [ "$i" = 60 ] && { echo "$api not healthy"; exit 1; }
    sleep 1
  done
  echo "  $api healthy"
done

say "1. api-a <- batch1 (backfill with a 2-hour gap: readings at 00,01,02,05)"
post_batch "$API_A" "$SAMPLES/batch1_initial_with_gap.csv"
echo "hourly usage of $METER (hours 02,03,04 must be unbillable gap):"
query_hours "$API_A" "$METER"

say "2. api-b <- batch2 (LATE readings 03,04 fill the gap; neighbours update together)"
post_batch "$API_B" "$SAMPLES/batch2_late_gap_fill.csv"
echo "hourly usage after the late fill (all hours billable now):"
query_hours "$API_B" "$METER"

say "3. api-a <- batch3 (identical retransmission: recorded once, deduplicated)"
post_batch "$API_A" "$SAMPLES/batch3_retransmit_same_values.csv"
echo "raw readings of $METER (still one row per second):"
curl -s "$API_A/readings?meter_id=$METER&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'

say "4. api-b <- batch4 (conflicting value for an existing second: whole batch rejected)"
post_batch "$API_B" "$SAMPLES/batch4_conflicting_value.csv"
echo "state after the rejected batch (unchanged):"
query_hours "$API_A" "$METER"

say "5. api-a <- batch5 (meter-west-02 rolls back 860 -> 855)"
post_batch "$API_A" "$SAMPLES/batch5_meter_rollback.csv"
echo "hourly usage of meter-west-02 (hour 00 unbillable rollback, hour 01 billable):"
query_hours "$API_A" "meter-west-02"

say "6. concurrency: api-a and api-b backfill meter-north-03 at the same time"
post_batch "$API_A" "$SAMPLES/batch6a_concurrent.csv" > /tmp/demo_cc_a.txt &
PID_A=$!
post_batch "$API_B" "$SAMPLES/batch6b_concurrent.csv" > /tmp/demo_cc_b.txt &
PID_B=$!
wait "$PID_A" "$PID_B"
echo "api-a:"; cat /tmp/demo_cc_a.txt
echo "api-b:"; cat /tmp/demo_cc_b.txt
echo "all readings of meter-north-03 (both successful batches preserved):"
curl -s "$API_A/readings?meter_id=meter-north-03" | jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "hourly usage of meter-north-03 (consistent with the final readings):"
query_hours "$API_B" "meter-north-03"

say "demo finished"
