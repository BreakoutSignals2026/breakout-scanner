#!/usr/bin/env bash
# CRYPTO V1 continuous GitHub Actions runner, no API keys or LIVE endpoints.
# One job covers up to 3h45m, with planned handover every 3 hours.
# Triggered push/manual run: exactly ONE scan. Prevent duplicate PAPER writers
# using the workflow-level concurrency group "crypto-breakout-v1".
set -euo pipefail

continuous="${CRYPTO_CONTINUOUS:-false}"
if [[ "$continuous" == "true" && "${GITHUB_EVENT_NAME:-}" != "schedule" ]]; then
  echo "::error::Continuous mode is reserved for scheduled workflow runs."
  exit 1
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# A GitHub-hosted job cannot run forever; the workflow times out at 4h.
# Leave ample time for the final commit and clean handover to queued run.
deadline_epoch=$(( $(date +%s) + 3 * 3600 + 45 * 60 ))
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
  git add -- crypto_virtual_v1_state.json crypto_virtual_v1_signals.jsonl \
      crypto_virtual_v1_trades.csv crypto_paper_state_v1.json \
      crypto_paper_events_v1.jsonl 2>/dev/null || true

  if ! git diff --cached --quiet; then
    git commit -m "Update CRYPTO V1 PAPER/virtual state (cycle $cycle)"
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
    echo "CRYPTO 3h45 session finished cleanly at $(date -u '+%H:%M:%S') UTC."
    break
  fi
  pause=$((next_epoch - now_epoch))
  echo "CRYPTO next scan at $(date -u -d "@$next_epoch" '+%H:%M:%S') UTC (sleep ${pause}s)"
  sleep "$pause"
done
