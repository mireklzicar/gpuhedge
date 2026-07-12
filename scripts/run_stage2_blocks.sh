#!/bin/bash
# Stage 2 driver: 8 blocks x 6 rounds with inter-block gaps for time-of-day
# diversity. Each invocation is independent and resumable (ledger + cost
# monitor reload their state), so a crash loses at most one block.
#
# Usage: run_stage2_blocks.sh [start_block] [end_block] [gap_seconds]
set -u
START=${1:-1}
END=${2:-8}
GAP=${3:-900}
cd "$(dirname "$0")/.."

for b in $(seq "$START" "$END"); do
  first=$(( (b - 1) * 6 + 1 ))
  echo "=== BLOCK $b (rounds $first-$((first + 5))) $(date -u +%H:%M:%SZ) ==="
  gpuhedge bench --go --start-round "$first" --max-rounds 6 || {
    echo "BLOCK $b FAILED (exit $?) — stopping driver"; exit 1; }
  if [ "$b" -lt "$END" ]; then
    echo "--- inter-block gap ${GAP}s ---"
    sleep "$GAP"
  fi
done
echo "ALL BLOCKS DONE $(date -u +%H:%M:%SZ)"
