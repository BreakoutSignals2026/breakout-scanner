#!/usr/bin/env bash
# Only GET US equities 5m IEX candles and Alpaca PAPER market CALENDAR.
# NEVER pass any order API secrets to user-facing messages, NEVER create orders.
# First job scans 09:30-12:00 NY, second job re-checkouts latest state until EOD.
set -euo pipefail
phase="${MICRO_SESSION_PHASE:-}"
if [[ "$phase" != "first" && "$phase" != "second" ]]; then
  echo "::error::Set MICRO_SESSION_PHASE to first or second."
  exit 1
fi
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

persist_micro() {
  local files=()
  for file in fast_micro_state_v1.json fast_micro_signals_v1.jsonl \
      fast_micro_events_v1.jsonl fast_micro_results_v1.csv; do
    [[ ! -f "$file" ]] || files+=("$file")
  done
  if (("${#files[@]}" > 0)); then
    git add -- "${files[@]}"
  fi
  if ! git diff --cached --quiet; then
    git commit -m "Update FAST MICRO V1 virtual state and results"
    # Separate scanners may push their own state between our fetch and push.
    local pushed=false
    for attempt in 1 2 3 4; do
      git pull --no-rebase -X ours origin main
      if git push origin HEAD:main; then
        pushed=true
        break
      fi
      echo "::warning::MICRO state push race; retry $attempt"
      sleep 3
    done
    if [[ "$pushed" != "true" ]]; then
      echo "::error::Cannot persist MICRO journal; halting instead of losing results."
      exit 1
    fi
  fi
}

while true; do
  ny_day=$(TZ=America/New_York date +%u)
  ny_time=$(TZ=America/New_York date '+%H:%M:%S')
  hh=${ny_time:0:2}
  mm=${ny_time:3:2}
  ny_min=$((10#$hh * 60 + 10#$mm))
  if ((ny_day >= 6)); then
    echo "MICRO NY weekend; finished."
    break
  fi
  if [[ "$phase" == "first" ]] && ((ny_min >= 720)); then
    echo "MICRO midday handover to second job."
    break
  fi
  if [[ "$phase" == "second" ]] && ((ny_min < 720)); then
    sleep 30
    continue
  fi

  # Alpaca PAPER GET /calendar includes NYSE holidays and early close times.
  if ! close_epoch=$(python scanner_fast_micro_v1.py --close-epoch); then
    echo "::warning::MICRO Alpaca calendar not accessible; retry in 2 minutes."
    sleep 120
    continue
  fi
  if ((close_epoch == 0)); then
    echo "MICRO: NYSE calendar shows market holiday; no simulation."
    break
  fi
  utc_epoch=$(date +%s)
  if ((utc_epoch >= close_epoch + 60)); then
    echo "MICRO: market finished (including early-close holidays)."
    break
  fi
  if ((ny_min < 570)); then
    sleep 30
    continue
  fi

  echo "FAST MICRO virtual cycle: NY $ny_time / $(date -u '+%H:%M:%S') UTC"
  if ! python -u scanner_fast_micro_v1.py; then
    echo "::warning::MICRO simulation/feed error; retry at next 5-minute boundary."
  fi
  # Persist EVERY completed cycle before sleeping or next job, even when scan
  # returned nonzero. The journal and dedupe state must survive restarts.
  persist_micro

  utc_epoch=$(date +%s)
  # 5-minute slots plus 45s give Alpaca's completed candle time to appear.
  slot=$(( (utc_epoch / 300 + 1) * 300 + 45 ))
  delay=$((slot - utc_epoch))
  if ((slot >= close_epoch + 60)); then
    echo "MICRO: final EOD simulation cycle saved."
    break
  fi
  sleep "$delay"
done
