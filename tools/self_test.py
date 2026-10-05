"""Deterministic board-era competition matrix and host-side signal generator.

Official amplitudes/DC/final limits are not published in the supplied manual.
Here their safe ADC envelopes and engineering tolerances are explicit test policy.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import atomic_json

FREQUENCIES = (100., 101., 137., 200., 237., 300., 399., 400.)
AMPLITUDES = (.08, .5, 1.5, 2.)
DUTIES = (.2, .3, .4, .5, .6, .7, .8)
EDGES = (0., .025, .05, .075, .1)
PROFILES = (("preliminary", 0., 5.), ("preliminary", .1, 8.),
            ("preliminary", .2, 5.), ("preliminary", .2, 12.5),
            ("final_strong", .3, 1.5), ("final_strong", .4, 2.),
            ("final_strong", .5, 1.5), ("final_strong", .5, 3.))


@dataclass(frozen=True)
class Case:
    id: str = ""
    suite: str = "competition"
    group: str = "preliminary"
    wave: str = "sine"
    frequency_hz: float = 237.
    vpp_v: float = 1.5
    dc_v: float = 1.65
    duty: float = .5
    rise: float = 0.
    fall: float = 0.
    phase: float = .173
    noise_pp_ratio: float = 0.
    bandwidth_ratio: float = 5.
    noise_mode: str = "gaussian_lowpass"
    fs_hz: float = 20000.
    vref_v: float = 3.3
    gain: float = 1.
    flags: int = 7
    spikes: int = 0
    spike_ratio: float = 0.
    lowpass_hz: float = 0.
    harmonic_ratio: float = 0.
    inverted: bool = False
    fault: str = ""
    expectation: str = "measure"  # measure / reject / observe
    seed: int = 1


def safe_center(vpp, noise, position, vref=3.3, gain=1., headroom=.035):
    half = .5 * vpp * (1 + noise)
    low, high = half + headroom / gain, vref / gain - half - headroom / gain
    if high < low:
        raise ValueError("test envelope exceeds ADC rails")
    return low + position * (high - low)


def matrix(seed=20261005, random_count=512):
    """Return the final board-side 296-case matrix.

    The board-era matrix is intentionally kept stable: 200 preliminary gate
    cases followed by 96 non-gating challenge cases. ``random_count`` remains
    in the signature for command-line compatibility and is ignored.
    """
    del random_count
    from tools.competition_stress_suite import gate_cases, challenge_cases

    def convert(old, ordinal, challenge=False):
        vref = old.reference_uv / 1_000_000.0
        gain = old.gain_ppm / 1_000_000.0
        vpp = old.vpp_counts * vref / 4095.0 / gain
        if old.center_mode == "low":
            position = 0.0
        elif old.center_mode == "high":
            position = 1.0
        elif old.center_mode == "middle":
            position = .5
        else:
            position = ((seed + ordinal * 104729) & 0xffff) / 65535.0
        group = "final_strong" if challenge else "preliminary"
        mode = {"highband": "gaussian_highpass"}.get(
            old.noise_mode, "gaussian_lowpass")
        return Case(
            id=f"competition-{ordinal:06d}", suite="competition", group=group,
            wave=old.kind, frequency_hz=old.frequency_hz, vpp_v=vpp,
            dc_v=safe_center(vpp, old.noise_ratio, position, vref, gain),
            duty=old.duty, rise=old.rise_width, fall=old.fall_width,
            phase=old.phase, noise_pp_ratio=old.noise_ratio,
            bandwidth_ratio=5.0 if old.noise_mode != "highband" else 2.0,
            noise_mode=mode, fs_hz=old.sample_rate_hz, vref_v=vref, gain=gain,
            harmonic_ratio=max(old.harmonic_2, old.harmonic_3),
            expectation="observe" if challenge else "measure",
            seed=(seed + ordinal * 104729) & 0xffffffff)

    old_cases = gate_cases(seed, 50) + challenge_cases(seed, 12)
    return (convert(old, ordinal, ordinal > 200)
            for ordinal, old in enumerate(old_cases, 1))


def generate(case):
    """Gaussian FFT filtering has an explicit upper frequency, unlike 1-pole tails."""
    import numpy as np
    rng = np.random.default_rng(case.seed)
    n = np.arange(host.SAMPLES)
    t = n / case.fs_hz
    p = (case.phase + t * case.frequency_hz) % 1.
    if case.wave == "sine":
        wave = np.sin(2 * np.pi * p)
    elif case.wave == "triangle":
        wave = 1. - 4. * np.abs((p + .25) % 1. - .5)
    elif case.wave == "sawtooth":
        wave = 1. - 2. * p  # prescribed descending saw, quick upward reset
    elif case.wave == "square":
        wave = np.where(p < case.duty, 1., -1.)
        rise_distance = (p + .5) % 1. - .5
        fall_distance = (p - case.duty + .5) % 1. - .5
        if case.rise:
            wave = np.where(np.abs(rise_distance) <= case.rise / 2,
                            2. * rise_distance / case.rise, wave)
        if case.fall:
            wave = np.where(np.abs(fall_distance) <= case.fall / 2,
                            -2. * fall_distance / case.fall, wave)
    else:
        raise ValueError(case.wave)
    if case.inverted:
        wave = -wave
    if case.lowpass_hz:
        # Optional analogue one-pole response; pre-roll prevents start-up bias.
        alpha = 1 - math.exp(-2 * math.pi * case.lowpass_hz / case.fs_hz)
        state = float(wave[0])
        for _ in range(3):
            filtered = []
            for value in wave:
                state += alpha * (float(value) - state)
                filtered.append(state)
        wave = np.array(filtered)
    cutoff = min(case.frequency_hz * case.bandwidth_ratio, case.fs_hz / 2)
    if case.noise_mode == "gaussian_replay":
        # Same noise on each signal period: averaging cannot remove this component.
        phases = np.arange(1024) / 1024
        spectrum = np.fft.rfft(rng.normal(size=1024))
        spectrum[np.arange(len(spectrum)) > case.bandwidth_ratio] = 0
        template = np.fft.irfft(spectrum, n=1024)
        noise = np.interp(p, np.r_[phases, 1.], np.r_[template, template[0]])
    elif case.noise_mode == "coherent":
        noise = np.sin(2 * np.pi * 2 * p) + .5 * np.sin(2 * np.pi * 3 * p + .4)
    else:
        spectrum = np.fft.rfft(rng.normal(size=host.SAMPLES))
        frequencies = np.fft.rfftfreq(host.SAMPLES, 1 / case.fs_hz)
        spectrum[frequencies > cutoff] = 0
        if case.noise_mode == "gaussian_highpass":
            spectrum[frequencies < case.frequency_hz] = 0
        spectrum[0] = 0
        noise = np.fft.irfft(spectrum, n=host.SAMPLES)
    # Keep zero sample mean. Mid-extrema centering would secretly add a DC term
    # to the Gaussian noise and make the advertised offset truth incorrect.
    noise -= noise.mean()
    span = float(np.ptp(noise))
    noise = (noise * (case.vpp_v * case.noise_pp_ratio / span)) if span else noise * 0
    # At near-rail offsets, asymmetric Gaussian extrema may exceed the symmetric
    # safety envelope. Scale the noise, never shift the ground-truth DC or clip.
    baseline = case.dc_v + .5 * case.vpp_v * wave
    if not case.fault:
        room = .005 / case.gain
        positive, negative = noise > 0, noise < 0
        scale = 1.
        if positive.any():
            scale = min(scale, float(np.min((case.vref_v/case.gain-room-baseline[positive]) / noise[positive])))
        if negative.any():
            scale = min(scale, float(np.min((room-baseline[negative]) / noise[negative])))
        noise *= max(0., scale)
    volts = case.dc_v + .5 * case.vpp_v * wave + noise
    volts += case.harmonic_ratio * case.vpp_v * np.sin(2 * np.pi * 3 * p)
    if case.spikes:
        indices = rng.choice(host.SAMPLES, case.spikes, replace=False)
        volts[indices] += rng.choice([-1., 1.], case.spikes) * case.vpp_v * case.spike_ratio
    codes = np.rint(volts * case.gain * 4095 / case.vref_v)
    if case.fault == "low_clip":
        codes[:host.SAMPLES // 4] = 0
    elif case.fault == "high_clip":
        codes[:host.SAMPLES // 4] = 4095
    elif case.fault == "both_clip":
        codes[:host.SAMPLES // 4] = 0
        codes[host.SAMPLES // 4:host.SAMPLES // 2] = 4095
    elif case.fault == "flat":
        codes[:] = 2048
    elif case.fault == "too_small":
        codes = 2048 + np.rint(wave * 2)
    samples = np.clip(codes, 0, 4095).astype("<u2")
    if case.fault == "invalid_code":
        samples[111] = 4096
    payload = samples.tobytes()
    meta = host.Metadata(case.seed, int(round(case.fs_hz * 1000)),
        int(round(case.vref_v * 1e6)), int(round(case.gain * 1e6)), 4095,
        case.flags, host.fnv1a(payload))
    if case.fault == "not_contiguous":
        meta = replace(meta, flags=0)
    elif case.fault == "bad_fs":
        meta = replace(meta, sample_rate_millihz=0)
    elif case.fault == "bad_gain":
        meta = replace(meta, gain_ppm=0)
    return payload, meta, {"noise_vpp_v": float(np.ptp(noise)),
        "noise_mean_v": float(noise.mean()),
        "requested_noise_vpp_v": case.vpp_v * case.noise_pp_ratio,
        "noise_upper_hz": cutoff, "adc_min": int(samples.min()),
        "adc_max": int(samples.max()),
        "prequantization_clipped_samples": int(np.count_nonzero((codes < 0) | (codes > 4095)))}


def evaluate(case, line, details, elapsed):
    parts = line.split(",")
    f, vpp, duty, dc = map(float, parts[1:])
    errors = {"frequency_error_pct": abs(f - case.frequency_hz) / case.frequency_hz * 100,
              "vpp_error_pct": abs(vpp - case.vpp_v) / case.vpp_v * 100,
              "duty_error_pp": abs(duty - case.duty * 100) if case.wave == "square" else 0.,
              "dc_error_v": abs(dc - case.dc_v)}
    # Local regression gates, not unpublished official scoring thresholds.
    limits = {"frequency_pct": 1., "vpp_pct": 10. if case.group == "final_strong" else 8.,
              "duty_pp": 2.5, "dc_v": max(.02, case.vpp_v * .05),
              "algorithm_time_s": 15. if case.group == "final_strong" else 10.}
    passed = (bool(details["ok"]) and parts[0] == case.wave and
              errors["frequency_error_pct"] <= limits["frequency_pct"] and
              errors["vpp_error_pct"] <= limits["vpp_pct"] and
              errors["duty_error_pp"] <= limits["duty_pp"] and
              errors["dc_error_v"] <= limits["dc_v"] and
              elapsed <= limits["algorithm_time_s"])
    if case.expectation == "reject":
        passed = not details["ok"] and line == "unknown,0,0,0,0"
    elif case.expectation == "observe":
        passed = None
    return {**errors, "passed": passed, "limits": limits,
            "class_correct": parts[0] == case.wave, "output": line,
            "algorithm_elapsed_s": elapsed}


def run(api, store, process, *, suite="all", seed=20261005,
        random_count=512, case_ids=(), max_cases=None, stop_event=None, stderr=None):
    import sys
    stderr = stderr or sys.stderr
    selected = [c for c in matrix(seed, random_count)
                if (suite == "all" or c.suite == suite) and
                (not case_ids or c.id in case_ids)]
    if case_ids and set(case_ids) - {c.id for c in selected}:
        raise ValueError("unknown case ID or case outside selected suite")
    if max_cases is not None:
        selected = selected[:max_cases]
    if not selected:
        raise ValueError("empty self-test selection")
    report_dir = store.path / ("rt-" + str(time.time_ns()))
    report_dir.mkdir()
    atomic_json(report_dir / "matrix.json", {"seed": seed, "cases": [asdict(c) for c in selected]})
    counters = Counter()
    groups = {}
    rows = []
    started = time.perf_counter()
    completed = False
    print(f"RT: {len(selected)} cases; reports: {report_dir}", file=stderr, flush=True)
    columns = ["case_id", "suite", "group", "expectation", "passed", "wave", "frequency_hz",
               "vpp_v", "dc_v", "duty", "rise", "fall", "noise_pp_ratio", "bandwidth_ratio",
               "output", "status", "frequency_error_pct", "vpp_error_pct", "duty_error_pp",
               "dc_error_v", "algorithm_elapsed_s", "capture"]
    try:
        with (report_dir / "results.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for i, case in enumerate(selected):
                if stop_event is not None and stop_event.is_set():
                    break
                payload, meta, facts = generate(case)
                record = store.begin("rt", {"case": asdict(case), "generated": facts})
                record.set_metadata(meta)
                record.complete(payload)
                line, details, elapsed = process(payload, meta, record)
                check = evaluate(case, line, details, elapsed)
                record.info["self_test"] = check
                record.save()
                label = "observed" if check["passed"] is None else "passed" if check["passed"] else "failed"
                counters[label] += 1
                counters["completed"] += 1
                group_key = f"{case.suite}/{case.group}"
                group = groups.setdefault(group_key, Counter())
                group[label] += 1
                if details["ok"] and check["class_correct"]:
                    for key in ("frequency_error_pct", "vpp_error_pct", "duty_error_pp", "dc_error_v"):
                        group["worst_" + key] = max(group["worst_" + key], check[key])
                row = {key: getattr(case, key) for key in columns if hasattr(case, key)}
                row.update({key: check[key] for key in columns if key in check})
                row.update(case_id=case.id, status=details["status"],
                           capture=str(record.path.relative_to(store.path)))
                writer.writerow(row)
                stream.flush()
                if not check["passed"] and case.expectation != "observe":
                    rows.append({"case": asdict(case), "result": check, "status": details["status"]})
                if (i + 1) % 100 == 0:
                    os.fsync(stream.fileno())
                    print(f"RT: {i+1}/{len(selected)}; failures={counters['failed']}", file=stderr, flush=True)
            os.fsync(stream.fileno())
            completed = counters["completed"] == len(selected)
    finally:
        summary = {"seed": seed, "suite": suite, "selected": len(selected),
            "complete": completed, "counts": dict(counters),
            "full_default_matrix": suite == "all" and not case_ids and max_cases is None and random_count == 512,
            "groups": {key: dict(value) for key, value in groups.items()},
            "wall_time_s": time.perf_counter() - started, "failures": rows,
            "note": "Restored board-era gate plus observed challenge cases; not a proof for all continuous signals. Engineering tolerances, not official scoring. Algorithm time excludes physical UART/ADC latency."}
        atomic_json(report_dir / "summary.json", summary)
    print(f"RT done: {dict(counters)}; summary: {report_dir/'summary.json'}", file=stderr, flush=True)
    return 0 if completed and not counters["failed"] else 1
