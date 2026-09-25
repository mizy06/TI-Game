#!/usr/bin/env bash
# Full firmware host checks and strict noise challenge gate. Run from anywhere.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
bash "$project_dir/tests/run_all_checks.sh"
for test_seed in 20260925 20260926 20261007; do
    python3 -B "$project_dir/tools/check_complete_matrix.py" \
        --library "$project_dir/tests/waveform_analyzer_host.so" \
        --seed "$test_seed" \
        --output "$project_dir/tests/results/noise_cascade_strict_seed${test_seed}.json"
done
