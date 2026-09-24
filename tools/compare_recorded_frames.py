#!/usr/bin/env python3
"""Compare old and fast C paths on the bundled 4096-sample ADC records."""
import argparse
import ctypes
import json
from pathlib import Path

from competition_stress_suite import WaveformFrame, WaveformResult, load_library


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--captures', type=Path, default=Path('tests/results'))
    parser.add_argument('--json', type=Path, required=True)
    args = parser.parse_args()
    library = load_library(args.library.resolve())
    differences = []
    status_counts = {str(algorithm): {} for algorithm in (2, 3)}
    total = 0
    for path in sorted(args.captures.rglob('*.bin')):
        if path.stat().st_size != 8192:
            continue
        raw = path.read_bytes()
        samples = (ctypes.c_uint16 * 4096).from_buffer_copy(raw)
        frame = WaveformFrame(samples, 4096, 4095, 20000000, 3300000,
                              1000000, total + 1, 1)
        current = {}
        for algorithm in (2, 3):
            result = WaveformResult()
            ok = library.waveform_analyze_frame_with_algorithm(
                ctypes.byref(frame), ctypes.byref(result), algorithm)
            current[algorithm] = (bool(ok), result.status, result.type,
                                  result.frequency_millihz,
                                  result.vpp_microvolts, result.duty_ppm)
            counts = status_counts[str(algorithm)]
            counts[str(result.status)] = counts.get(str(result.status), 0) + 1
        if current[2] != current[3]:
            differences.append({'path': str(path), 'old': current[2],
                                'fast': current[3]})
        total += 1
    report = {'frames': total, 'status_counts': status_counts,
              'different_outputs': len(differences), 'differences': differences}
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f"recorded_frames={total} differing_outputs={len(differences)}")
    for difference in differences[:10]:
        print(difference)


if __name__ == '__main__':
    main()
