#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$root/tests/.build"
mkdir -p "$tmp"
trap 'rm -rf "$tmp"' EXIT
for case_name in host_test precision_validation; do
  "${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -I"$root" \
    "$root/waveform_analyzer.c" "$root/waveform_cascade.c" \
    -x c "$root/tests/$case_name.c.txt" -lm -o "$tmp/$case_name"
  "$tmp/$case_name"
done
bash "$root/tools/build_host_waveform.sh"
python3 -m unittest discover -s "$root/tests" -p 'test_host_integration.py' -v
python3 -m unittest discover -s "$root/tests" -p 'test_server.py' -v
