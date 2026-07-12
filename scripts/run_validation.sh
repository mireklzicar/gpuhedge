#!/usr/bin/env bash
# Queue-cutover validation driver: pre-registered 60-request experiment,
# then the forced loser-cancel audit. Resumable: pass a start block as $1.
set -uo pipefail
cd "$(dirname "$0")/.."
START_BLOCK="${1:-1}"
LOG=traces/validation_driver.log
{
  echo "=== validation driver start $(date -u +%FT%TZ) start_block=$START_BLOCK ==="
  PYTHONUNBUFFERED=1 python -m gpuhedge validate --go --start-block "$START_BLOCK"
  rc=$?
  echo "=== validate exited rc=$rc $(date -u +%FT%TZ) ==="
  if [ $rc -eq 0 ]; then
    echo "=== cancel audit start $(date -u +%FT%TZ) ==="
    PYTHONUNBUFFERED=1 python -m gpuhedge cancel-audit --go --repeats 2
    echo "=== cancel audit exited rc=$? $(date -u +%FT%TZ) ==="
  else
    echo "=== skipping cancel audit (validate failed) ==="
  fi
} >> "$LOG" 2>&1
