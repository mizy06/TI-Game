#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -x c "$script_dir/host_test.c.txt" \
  -lm -o "$script_dir/host_test"

"$script_dir/host_test"

cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -x c "$script_dir/precision_validation.c.txt" \
  -lm -o "$script_dir/precision_validation"

"$script_dir/precision_validation"

cc -std=c99 -O2 -Wall -Wextra -Werror -fPIC -shared \
  -I"$project_dir" \
  "$project_dir/waveform_analyzer.c" \
  -lm -o "$script_dir/waveform_analyzer_host.so"

python3 -B "$project_dir/tools/competition_stress_suite.py" \
  --library "$script_dir/waveform_analyzer_host.so" \
  --json "$script_dir/results/competition_stress_report.json"
