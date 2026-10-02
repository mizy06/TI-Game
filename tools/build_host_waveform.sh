#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$(uname -s)" in
  Darwin) out="$root/tools/libwaveform_analyzer_host.dylib" ;;
  *)      out="$root/tools/libwaveform_analyzer_host.so" ;;
esac
"${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -fPIC -shared \
  -I"$root" "$root/waveform_analyzer.c" "$root/waveform_cascade.c" \
  "$root/tools/host_api.c" -lm -o "$out"
printf 'Built %s\n' "$out"
