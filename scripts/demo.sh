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

say "7. meter swap: api-a <- batch7 (00,01,02,03,04 with the 02:00 reading still on the old code)"
post_batch "$API_A" "$SAMPLES/batch7_swap_before_registration.csv"
echo "meter-south-04 before the work order (02:00 looks like a rollback 903 -> 43):"
query_hours "$API_A" "meter-south-04"

say "8. api-b <- work order for meter-south-04 (swap at 02:00, old last 903, new initial 40)"
rm -f /tmp/demo_wo_1.json /tmp/demo_wo_2.json
curl -s -o /tmp/demo_wo_1.json -w '  HTTP %{http_code}\n' \
  -X POST "$API_B/meter-replacements" -H 'content-type: application/json' \
  --data-binary @"$SAMPLES/work_order_swap.json"
jq . /tmp/demo_wo_1.json | sed 's/^/  /'
echo "hourly usage after registration (hour 02 now starts with the new code 40 -> 43):"
query_hours "$API_A" "meter-south-04"
echo "raw readings (unchanged: the stored 02:00 row is not rewritten):"
curl -s "$API_A/readings?meter_id=meter-south-04&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'

say "9. api-a <- identical work order again (200, created=false, no duplicate registration)"
curl -s -o /tmp/demo_wo_2.json -w '  HTTP %{http_code}\n' \
  -X POST "$API_A/meter-replacements" -H 'content-type: application/json' \
  --data-binary @"$SAMPLES/work_order_swap.json"
jq . /tmp/demo_wo_2.json | sed 's/^/  /'

say "10. both instances <- batch8 at the same time (old and new codes for the swap hour)"
curl -s -o /tmp/demo_swap_a.json -w '  api-a HTTP %{http_code}\n' \
  -F "file=@$SAMPLES/batch8_swap_retransmit_both_codes.csv;type=text/csv" "$API_A/batches" &
PID_A=$!
curl -s -o /tmp/demo_swap_b.json -w '  api-b HTTP %{http_code}\n' \
  -F "file=@$SAMPLES/batch8_swap_retransmit_both_codes.csv;type=text/csv" "$API_B/batches" &
PID_B=$!
wait "$PID_A" "$PID_B"
echo "  api-a response:"; jq . /tmp/demo_swap_a.json | sed 's/^/  /'
echo "  api-b response:"; jq . /tmp/demo_swap_b.json | sed 's/^/  /'
echo "raw readings (still one row per second; nothing rewritten):"
curl -s "$API_A/readings?meter_id=meter-south-04&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'
echo "hourly usage (hour 01 keeps the old code, hour 02 the new code):"
query_hours "$API_B" "meter-south-04"

say "11. api-b <- batch9 (a third value at the swap hour: whole batch rejected)"
post_batch "$API_B" "$SAMPLES/batch9_swap_conflicting_value.csv"

say "12. api-a <- batch7b + work order for meter-south-06 (gateway never reported the swap hour)"
post_batch "$API_A" "$SAMPLES/batch7b_swap_hour_skipped.csv"
echo "before the work order (01:00 and 02:00 are a gap):"
query_hours "$API_A" "meter-south-06"
curl -s -o /tmp/demo_wo_skipped.json -w '  work order HTTP %{http_code}\n' \
  -X POST "$API_A/meter-replacements" -H 'content-type: application/json' \
  --data-binary @"$SAMPLES/work_order_swap_skipped.json"
echo "after the work order (02:00 is never stored, yet both hours are billable):"
query_hours "$API_A" "meter-south-06"
echo "raw readings of meter-south-06 (no synthetic row at 02:00):"
curl -s "$API_A/readings?meter_id=meter-south-06&from_second=$T0&to_second=$T5" |
  jq -r '.[] | "  \(.utc_time)  kwh=\(.kwh)"'

say "13. concurrent conflicting work orders on the two instances (exactly one wins)"
rm -f /tmp/demo_wo_a.json /tmp/demo_wo_b.json
jq -c '.old_meter_last_kwh = 904.0 | .meter_id = "meter-south-05"' \
  "$SAMPLES/work_order_swap.json" > /tmp/demo_wo_alt_1.json
jq -c '.old_meter_last_kwh = 905.0 | .meter_id = "meter-south-05"' \
  "$SAMPLES/work_order_swap.json" > /tmp/demo_wo_alt_2.json
curl -s -o /tmp/demo_wo_a.json -w '  api-a HTTP %{http_code}\n' \
  -X POST "$API_A/meter-replacements" -H 'content-type: application/json' \
  --data-binary @/tmp/demo_wo_alt_1.json &
PID_A=$!
curl -s -o /tmp/demo_wo_b.json -w '  api-b HTTP %{http_code}\n' \
  -X POST "$API_B/meter-replacements" -H 'content-type: application/json' \
  --data-binary @/tmp/demo_wo_alt_2.json &
PID_B=$!
wait "$PID_A" "$PID_B"
echo "  rejected side:"
for f in /tmp/demo_wo_a.json /tmp/demo_wo_b.json; do
  jq -r 'select(.detail) | "    " + .detail' "$f" || true
done
echo "registered work orders:"
curl -s "$API_A/meter-replacements" | jq . | sed 's/^/  /'

say "demo finished"
