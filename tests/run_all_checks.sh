#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
"${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$root/tests/stubs" -I"$root" -fsyntax-only "$root/empty.c"
"$root/tests/run_host_tests.sh"
python3 -m py_compile "$root/tools/host_waveform.py"
