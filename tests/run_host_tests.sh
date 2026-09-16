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
