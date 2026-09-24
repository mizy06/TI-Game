#!/usr/bin/env python3
"""Time the same pre-generated ADC frames through all three C algorithms.

Host timing compares implementations only. It is not a Cortex-M0+ deadline
measurement. Every frame is reused for each algorithm and each repetition.
"""
import argparse
import ctypes
import json
from pathlib import Path
import platform
import statistics
import time

from competition_stress_suite import (
    SAMPLE_COUNT, ADC_FULL_SCALE, FRAME_CONTIGUOUS, FRAME_CLOCK_CALIBRATED,
    FRAME_VREF_CALIBRATED, WaveformFrame, WaveformResult, gate_cases,
    challenge_cases, generate_samples, load_library,
)


def percentile(values, proportion):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * proportion))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--json', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    lib = load_library(args.library.resolve())
    seed = 20260919
    cases = gate_cases(seed, 50) + challenge_cases(seed, 12)
    frames = []
    for i, case in enumerate(cases):
        sample_seed = seed + i * 7919 if i < 200 else seed ^ ((i - 200) * 104729)
        samples = (ctypes.c_uint16 * SAMPLE_COUNT)(*generate_samples(case, sample_seed))
        frame = WaveformFrame(samples, SAMPLE_COUNT, ADC_FULL_SCALE,
                              round(case.sample_rate_hz * 1000), case.reference_uv,
                              case.gain_ppm, i + 1, FRAME_CONTIGUOUS |
                              FRAME_CLOCK_CALIBRATED | FRAME_VREF_CALIBRATED)
        frames.append((samples, frame))  # Keep C sample buffers alive.
    timings = {algorithm: [] for algorithm in (1, 2, 3)}
    outputs = {algorithm: [] for algorithm in (1, 2, 3)}
    for repeat in range(args.repeats + 1):
        for i, (_, frame) in enumerate(frames):
            # Rotate execution order to limit thermal / scheduling bias.
            order = (1, 2, 3) if (repeat + i) % 2 == 0 else (3, 2, 1)
            for algorithm in order:
                result = WaveformResult()
                start = time.perf_counter_ns()
                ok = lib.waveform_analyze_frame_with_algorithm(
                    ctypes.byref(frame), ctypes.byref(result), algorithm)
                elapsed = time.perf_counter_ns() - start
                if repeat:
                    timings[algorithm].append(elapsed / 1e6)
                if repeat == 1:
                    outputs[algorithm].append((bool(ok), result.status, result.type,
                                               result.frequency_millihz,
                                               result.vpp_microvolts, result.duty_ppm))
    report = {'host': platform.platform(), 'repeats': args.repeats,
              'frame_count': len(frames), 'warmup_passes': 1, 'algorithms': {}}
    for algorithm in (1, 2, 3):
        values = timings[algorithm]
        report['algorithms'][str(algorithm)] = {
            'median_ms': round(statistics.median(values), 4),
            'p95_ms': round(percentile(values, .95), 4),
            'max_ms': round(max(values), 4),
            'total_ms': round(sum(values), 2),
        }
    differences = []
    for i, (old, fast) in enumerate(zip(outputs[2], outputs[3])):
        if old[0:3] != fast[0:3]:
            differences.append({'case_index': i + 1, 'old': old, 'fast': fast})
    report['status_or_class_differences_2_vs_3'] = differences
    report['speedup_median_2_vs_3'] = round(
        report['algorithms']['2']['median_ms'] /
        report['algorithms']['3']['median_ms'], 3)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
