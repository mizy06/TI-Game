#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
"${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$root/tests/stubs" -I"$root" -fsyntax-only "$root/empty.c"
bash "$root/tests/run_host_tests.sh"
python3 -m py_compile "$root/server.py" "$root/tools/host_waveform.py" \
  "$root/tools/waveform_server.py" "$root/tools/capture_store.py" "$root/tools/self_test.py" \
  "$root/tools/export_capture.py" "$root/tools/verify_rt_archive.py" \
  "$root/tools/unified_solver.py" "$root/tools/parallel_analysis.py"
