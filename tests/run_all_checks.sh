#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"
project_dir="$(cd "$script_dir/.." && pwd)"

cc -std=c99 -O2 -Wall -Wextra -Werror \
  -I"$script_dir/stubs" -I"$project_dir" \
  -c "$project_dir/empty.c" -o "$script_dir/empty_host_syntax.o"

rm -f "$script_dir/empty_host_syntax.o"
"$script_dir/run_host_tests.sh"
