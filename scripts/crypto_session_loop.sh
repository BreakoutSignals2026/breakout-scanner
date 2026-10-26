#!/usr/bin/env bash
# CRYPTO V1 continuous GitHub Actions runner, no API keys or LIVE endpoints.
# One job covers up to 3h45m; the hourly schedule maintains a queued
# replacement to reduce gaps. Concurrency allows only one PAPER writer.
# Ordinary push/manual run: exactly ONE scan. The special safe-restart push
# runs multiple 5min observation cycles for 25min, never sends new PAPER orders.
# Workflow concurrency cancels prior session ONLY on this explicit restart.
# Operator recovery test on 2026-10-26 after a missed hourly CRYPTO handover;
# diagnostic push always disables new PAPER entries and expires in 25 minutes.
set -euo pipefail

continuous="${CRYPTO_CONTINUOUS:-false}"
safe_restart="${CRYPTO_SAFE_RESTART:-false}"
if [[ "$safe_restart" == "true" ]]; then
  if [[ "${GITHUB_EVENT_NAME:-}" != "push" || "${ENABLE_CRYPTO_PAPER_TRADING:-false}" != "false" || "$continuous" != "true" ]]; then
    echo "::error::CRYPTO safe restart only permits a continuous push with PAPER entries OFF."
    exit 1
  fi
fi
if [[ "$continuous" == "true" && "${GITHUB_EVENT_NAME:-}" != "schedule" && "$safe_restart" != "true" ]]; then
  echo "::error::Continuous mode must be a schedule or explicitly SAFE restart."
  exit 1
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# A GitHub-hosted job cannot run forever; the workflow times out at 4h.
# Leave ample time for the final commit and clean handover to queued run.
deadline_epoch=$(( $(date +%s) + 3 * 3600 + 45 * 60 ))
if [[ "$safe_restart" == "true" ]]; then
  deadline_epoch=$(( $(date +%s) + 25 * 60 ))
  echo "CRYPTO V1.2 SAFE RESTART: max 25min; PAPER new entries disabled; recurring schedule unchanged."
fi
cycle=0

while true; do
  cycle=$((cycle + 1))
  echo "CRYPTO session cycle $cycle UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ') mode=$continuous"
  set +e
  python -u scanner_crypto_v1.py
  scan_status=$?
  set -e

  # Always save even if scanner failed; its PAPER recovery state and previous
  # signal records must be durable BEFORE another process can trade.
  # Fresh accounts have no signals, trades or journal yet. Stage only
  # files that EXIST: one missing path can otherwise cause git add to fail.
  changed_state=()
  for state_file in \
      crypto_virtual_v1_state.json crypto_virtual_v1_signals.jsonl \
      crypto_virtual_v1_trades.csv crypto_paper_state_v1.json \
      crypto_paper_events_v1.jsonl \
      crypto_candles_v1.csv crypto_quality_v1.csv \
      crypto_research_v1_state.json crypto_research_v1_signals.jsonl \
      crypto_research_v1_trades.csv \
      crypto_kraken_candles_v1.csv crypto_crossfeed_v1.csv \
      crypto_trend_v1_state.json crypto_trend_v1_signals.jsonl \
      crypto_trend_v1_trades.csv; do
    if [[ -f "$state_file" ]]; then
      changed_state+=("$state_file")
    fi
  done
  if (("${#changed_state[@]}" > 0)); then
    git add -- "${changed_state[@]}"
  fi

  if ! git diff --cached --quiet; then
    git commit -m "Update CRYPTO V1.2 PAPER/virtual state (cycle $cycle)"
    pushed=false
    # US stock scanners can push their own journal files in parallel.
    # Merge their newest commit; retry non-fast-forward push briefly.
    for attempt in 1 2 3 4; do
      git pull --no-rebase -X ours origin main
      if git push origin HEAD:main; then
        pushed=true
        break
      fi
      echo "::warning::CRYPTO state push race, retry $attempt"
      sleep 3
    done
    if [[ "$pushed" != "true" ]]; then
      echo "::error::Could not persist PAPER broker state: stopping session."
      exit 1
    fi
  fi

  if [[ "$continuous" != "true" ]]; then
    exit "$scan_status"
  fi
  if ((scan_status != 0)); then
    echo "::warning::CRYPTO scan failed ($scan_status); retrying next 5min slot."
  fi

  # UTC five-minute boundaries +15 seconds: 00:00:15, 00:05:15, ...
  # If API/sync ran over five minutes, skip missed slots; never pile up
  # overlapping broker requests or synthetic catch-up signals.
  now_epoch=$(date +%s)
  next_epoch=$(( (now_epoch / 300 + 1) * 300 + 15 ))
  if ((next_epoch >= deadline_epoch)); then
    echo "CRYPTO 3h45 session finished cleanly at $(date -u '+%H:%M:%S') UTC. Hourly recovery runs handle handover."
    break
  fi
  pause=$((next_epoch - now_epoch))
  echo "CRYPTO next scan at $(date -u -d "@$next_epoch" '+%H:%M:%S') UTC (sleep ${pause}s)"
  sleep "$pause"
done
