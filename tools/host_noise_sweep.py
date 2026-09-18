#!/usr/bin/env python3
"""Deterministic host-side noise sweep for waveform_analyzer.dll."""

import argparse
import ctypes
import json
import math
import random
from pathlib import Path

FS = 20_000.0
N = 4096
ADC_MAX = 4095.0
VDDA = 3.3


class WaveformResult(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("status", ctypes.c_int),
        ("frequency_millihz", ctypes.c_uint32),
        ("vpp_millivolts", ctypes.c_uint32),
        ("duty_permille", ctypes.c_uint16),
        ("confidence_permille", ctypes.c_uint16),
        ("best_score_permille", ctypes.c_uint16),
        ("second_score_permille", ctypes.c_uint16),
        ("accepted_cycles", ctypes.c_uint16),
        ("high_clip_samples", ctypes.c_uint16),
        ("dc_offset_counts", ctypes.c_uint16),
        ("low_clip_samples", ctypes.c_uint16),
        ("invalid_samples", ctypes.c_uint16),
    ]


TYPE_NAMES = ["sine", "square", "triangle", "sawtooth", "unknown"]
STATUS_NAMES = [
    "OK",
    "SIGNAL_TOO_SMALL",
    "ADC_HIGH_CLIPPED",
    "PERIOD_NOT_FOUND",
    "FREQUENCY_OUT_OF_RANGE",
    "ADC_LOW_CLIPPED",
    "ADC_SAMPLE_INVALID",
    "CLASSIFICATION_AMBIGUOUS",
]


def ideal_wave(kind, phase, duty):
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if kind == "square":
        return 1.0 if phase < duty else -1.0
    if kind == "triangle":
        if phase < 0.25:
            return 4.0 * phase
        if phase < 0.75:
            return 2.0 - 4.0 * phase
        return 4.0 * phase - 4.0
    return 1.0 - 2.0 * phase  # descending sawtooth


def generate(kind, freq, vpp, duty, noise_ratio, rng):
    center = 1.65 / VDDA * ADC_MAX
    vpp_counts = vpp / VDDA * ADC_MAX
    # Scale one finite Gaussian record so its observed peak-to-peak equals
    # noise_ratio * signal Vpp. This is the competition-style white-noise case.
    noise = [rng.gauss(0.0, 1.0) for _ in range(N)]
    lo, hi = min(noise), max(noise)
    scale = (noise_ratio * vpp_counts) / (hi - lo) if hi > lo else 0.0
    noise = [(x - 0.5 * (lo + hi)) * scale for x in noise]
    phase0 = rng.random()
    samples = []
    for i in range(N):
        phase = (phase0 + freq * i / FS) % 1.0
        value = center + 0.5 * vpp_counts * ideal_wave(kind, phase, duty) + noise[i]
        samples.append(max(0, min(4095, int(round(value)))))
    return samples


def analyze(dll, samples):
    arr = (ctypes.c_uint16 * N)(*samples)
    result = WaveformResult()
    valid = bool(dll.waveform_analyze(arr, ctypes.byref(result)))
    kind = TYPE_NAMES[result.type] if 0 <= result.type < len(TYPE_NAMES) else "unknown"
    status = STATUS_NAMES[result.status] if 0 <= result.status < len(STATUS_NAMES) else "UNKNOWN"
    return valid, result, kind, status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=25,
                        help="cases per waveform and noise level")
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--dll", default=str(Path(__file__).with_name("waveform_analyzer.dll")))
    parser.add_argument("--json", default="tests/results/host_noise_sweep.json")
    args = parser.parse_args()

    dll = ctypes.CDLL(args.dll)
    dll.waveform_analyze.argtypes = [ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(WaveformResult)]
    dll.waveform_analyze.restype = ctypes.c_bool
    rng = random.Random(args.seed)
    levels = [0.00, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50]
    kinds = ["sine", "square", "triangle", "sawtooth"]
    edge_freqs = [100.0, 101.0, 200.0, 399.0, 400.0]
    report = {"seed": args.seed, "cases_per_waveform": args.cases, "levels": []}

    for level in levels:
        rows = []
        failures = []
        for kind in kinds:
            for index in range(args.cases):
                freq = edge_freqs[index % len(edge_freqs)] if index < len(edge_freqs) else rng.uniform(100.0, 400.0)
                duty = rng.uniform(0.20, 0.80) if kind == "square" else 0.50
                # Keep the stress cases inside 0-3.3 V even at 50% noise.
                max_vpp = min(2.6, 3.0 / (1.0 + level) - 0.02)
                vpp = rng.uniform(0.80, max_vpp)
                samples = generate(kind, freq, vpp, duty, level, rng)
                valid, result, got_kind, status = analyze(dll, samples)
                measured_freq = result.frequency_millihz / 1000.0
                measured_vpp = result.vpp_millivolts / 1000.0
                freq_err = abs(measured_freq - freq) / freq * 100.0 if valid else 100.0
                vpp_err = abs(measured_vpp - vpp) / vpp * 100.0 if valid else 100.0
                duty_err = (abs(result.duty_permille / 10.0 - duty * 100.0)
                            if kind == "square" and valid else 0.0)
                passed = (valid and status == "OK" and got_kind == kind and
                          freq_err <= 2.0 and vpp_err <= 8.0 and duty_err <= 3.0)
                row = {
                    "expected": kind, "got": got_kind, "status": status,
                    "freq_hz": freq, "measured_freq_hz": measured_freq,
                    "vpp_v": vpp, "measured_vpp_v": measured_vpp,
                    "duty_pct": duty * 100.0 if kind == "square" else None,
                    "measured_duty_pct": result.duty_permille / 10.0 if kind == "square" and valid else None,
                    "freq_err_pct": freq_err, "vpp_err_pct": vpp_err,
                    "duty_err_pp": duty_err, "margin_pct": (result.best_score_permille - result.second_score_permille) / 10.0,
                    "cycles": result.accepted_cycles, "passed": bool(passed),
                }
                rows.append(row)
                if not passed and len(failures) < 8:
                    failures.append(row)
        valid_rows = [r for r in rows if r["status"] == "OK"]
        level_summary = {
            "noise_pct": level * 100.0,
            "total": len(rows),
            "passes": sum(r["passed"] for r in rows),
            "class_correct": sum(r["expected"] == r["got"] for r in rows),
            "status_ok": len(valid_rows),
            "max_freq_err_pct": max(r["freq_err_pct"] for r in rows),
            "max_vpp_err_pct": max(r["vpp_err_pct"] for r in rows),
            "max_duty_err_pp": max(r["duty_err_pp"] for r in rows),
            "min_margin_pct": min(r["margin_pct"] for r in rows),
            "failures": failures,
        }
        report["levels"].append(level_summary)
        print("noise={noise_pct:>4.0f}%  pass={passes:>3}/{total}  class={class_correct:>3}/{total}  "
              "max_err(freq/vpp/duty)={max_freq_err_pct:.3f}%/{max_vpp_err_pct:.3f}%/{max_duty_err_pp:.2f}pp  "
              "min_margin={min_margin_pct:.1f}%".format(**level_summary))

    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved={output}")


if __name__ == "__main__":
    main()
