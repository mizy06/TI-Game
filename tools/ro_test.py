"""RO: replay identifiable weak/strong finals cases and acquire through R."""
from __future__ import annotations

import csv
from functools import lru_cache
import hashlib
import io
import json
import math
import os
import re
import socket
import struct
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import atomic_json, atomic_text
from tools.self_test import (Case, clean_signal, distorted_shape, evaluate, generate, matrix,
                             TEST_FREQUENCY_MIN_HZ, TEST_FREQUENCY_MAX_EXCLUSIVE_HZ,
                             TEST_NOISE_MAX_EXCLUSIVE)

PROJECT = Path(__file__).resolve().parents[1]
FINALS_SUITE_SPECS = (
    ("new48", "matrix_new48.json", 98131029, 48),
    ("fresh96", "matrix_fresh96.json", 72014391, 96),
    ("paired221", "matrix_221.json", 20261012, 221),
)


def _finals_waveform(kind, p, duty=.5, rise=0., fall=0.):
    import numpy as np
    from scipy.signal import sawtooth
    p = np.asarray(p, dtype=float) % 1.0
    if kind == "sine":
        return np.sin(2.0 * np.pi * p)
    if kind == "triangle":
        return sawtooth(2.0 * np.pi * (p + 0.25), width=0.5)
    if kind == "sawtooth":
        return -sawtooth(2.0 * np.pi * p)
    r = max(float(rise), 1e-12)
    f = max(float(fall), 1e-12)
    return np.interp(p, [-r / 2.0, r / 2.0, duty - f / 2.0, duty + f / 2.0, 1.0 - r / 2.0, 1.0 + r / 2.0],
                     [-1.0, 1.0, 1.0, -1.0, -1.0, 1.0])


def _generate_finals_row(row, seed):
    """Exact reference generator for the 48+96+221 finals matrix set."""
    import numpy as np
    nominal_fs = float(row.get("fs", 20000.0))
    fs = nominal_fs * (1.0 + float(row.get("clock_error_ppm", 0.0)) * 1e-6)
    n = int(row["samples"])
    f = float(row["frequency"])
    duty = float(row.get("duty", 0.5))
    rise, fall = row.get("edges", (row.get("rise", 0.0), row.get("fall", 0.0)))
    p = np.arange(n, dtype=float) * f / fs + float(row["phase"])
    clean = _finals_waveform(row["kind"], p, duty, rise, fall)
    rng = np.random.default_rng(int(seed))
    noise = np.zeros(n, dtype=float)
    noise_audit = None
    if row.get("noise"):
        if row.get("noise_model") == "gaussian_notched_replay":
            from tools.identifiable_finals import noise_samples
            noise, noise_audit = noise_samples(row, seed)
        elif row.get("noise_model") == "multitone":
            frequencies = np.asarray(row["interference_frequencies_hz"], dtype=float)
            phases = rng.uniform(0.0, 2.0 * np.pi, len(frequencies))
            weights = rng.uniform(0.4, 1.0, len(frequencies))
            noise = np.sin(2.0 * np.pi * np.arange(n, dtype=float)[:, None] / fs * frequencies + phases) @ weights
            noise *= float(row["noise"]) * float(row["vpp"]) / float(np.ptp(noise))
        elif row.get("noise_model") == "white":
            noise = rng.normal(size=n)
            noise *= float(row["noise"]) * float(row["vpp"]) / float(np.ptp(noise))
        elif row.get("replay"):
            loop = float(row["replay"])
            size = 4096
            table = rng.normal(size=size)
            spec = np.fft.rfft(table)
            spec[np.fft.rfftfreq(size, 1.0 / size) > float(row["bandwidth"]) * loop] = 0
            spec[0] = 0
            table = np.fft.irfft(spec, n=size)
            table *= float(row["noise"]) * float(row["vpp"]) / float(np.ptp(table))
            noise = np.interp((p / loop) % 1.0, np.arange(size + 1, dtype=float) / size, np.r_[table, table[0]])
        else:
            spec = np.fft.rfft(rng.normal(size=n))
            spec[np.fft.rfftfreq(n, 1.0 / fs) > f * float(row["bandwidth"])] = 0
            spec[0] = 0
            noise = np.fft.irfft(spec, n=n)
            noise *= float(row["noise"]) * float(row["vpp"]) / float(np.ptp(noise))
    baseline = 0.5 * float(row["vpp"]) * clean + noise
    room = 3.3 - 0.08 - float(np.ptp(baseline))
    if room < 0:
        raise ValueError("test envelope exceeds ADC")
    dc = 0.04 - float(baseline.min()) + float(row.get("dc_position", 0.5)) * room
    adc = (dc + baseline) * 4095.0 / 3.3
    if row.get("adc_model") == "inl":
        adc = adc + 0.35 * np.sin(2.0 * np.pi * adc / 37.0)
    codes = np.rint(adc).astype("<u2")
    if np.any(codes > 4095):
        raise ValueError("test has invalid ADC code")
    payload = codes.tobytes()
    meta = host.Metadata(
        int(seed),
        int(round(nominal_fs * 1000.0)),
        3300000,
        1000000,
        4095,
        7,
        host.fnv1a(payload),
        sample_rate_hz_exact=nominal_fs,
        payload_bytes=len(payload),
        protocol_version=3 if n == 8192 else 2,
    )
    cutoff = min(f * float(row.get("bandwidth", 5.0)), nominal_fs / 2.0)
    truth = {
        "frequency_hz": f,
        "vpp_v": float(row["vpp"]),
        "dc_v": float(dc),
        "duty_pct": float(duty * 100.0) if row["kind"] == "square" else 0.0,
        "scope": str(row.get("scope") or row.get("tag") or "finals_365"),
        "vpp_definition": "clean signal peak-to-peak before noise/INL/quantization",
        "dc_definition": "configured source offset (clean envelope midpoint)",
    }
    if row["kind"] == "sine" and row.get("replay"):
        volts = codes.astype(float) * 3.3 / 4095.0
        basis = np.column_stack((np.sin(2.0 * np.pi * p), np.cos(2.0 * np.pi * p), np.ones_like(p)))
        coeffs, _, _, _ = np.linalg.lstsq(basis, volts, rcond=None)
        truth["wire_carrier_vpp_v"] = float(2.0 * math.hypot(coeffs[0], coeffs[1]))
    facts = {
        "noise_vpp_v": float(np.ptp(noise)),
        "noise_mean_v": float(noise.mean()),
        "requested_noise_vpp_v": float(row["vpp"]) * float(row.get("noise", 0.0)),
        "noise_upper_hz": float(cutoff),
        "adc_min": int(codes.min()),
        "adc_max": int(codes.max()),
        "prequantization_clipped_samples": 0,
        "truth": truth,
    }
    if noise_audit is not None:
        facts["noise_identifiability"] = noise_audit
    return payload, meta, facts


def _fallback_finals_rows(group):
    """Deterministic fallback if a validation/*.json file is absent."""
    import numpy as np
    if group == "paired221":
        from tools.finals_matrix import covering_rows
        rows = []
        for kind in host.NAMES[:4]:
            cases, _ = covering_rows(kind)
            rows.extend(cases)
        base = dict(kind="square", frequency=200.0, vpp=1.5, phase=0.003, noise=0.0,
                    bandwidth=5.0, replay=1, samples=8192, duty=0.2, edges=(0.0, 0.0))
        rows.extend([
            dict(base, tag="locked_unobservable_edges"),
            dict(base, frequency=237.193, phase=0.999999, edges=(0.037, 0.081), duty=0.63173, tag="unequal_edges"),
            dict(base, kind="sawtooth", frequency=100.0, phase=0.0, tag="reset_on_sample"),
            dict(base, kind="sine", frequency=237.193, fs=21317.173, tag="fractional_clock"),
        ])
        rng = np.random.default_rng(8173159)
        for kind in host.NAMES[:4]:
            for index in range(20):
                rows.append(dict(
                    kind=kind, tag="continuous_holdout",
                    frequency=float(rng.uniform(70, 750)),
                    vpp=float(rng.uniform(0.08, 2.0)),
                    phase=float(rng.random()),
                    noise=float(rng.uniform(0.0, 0.5)),
                    bandwidth=float(rng.uniform(1.5, 10)),
                    replay=(1, 8, 0)[index % 3],
                    samples=(4096, 8192)[index % 2],
                    dc_position=float(rng.random()),
                    fs=float(rng.uniform(19000, 23500)),
                    duty=float(rng.uniform(0.2, 0.8)),
                    rise=float(rng.uniform(0, 0.1)),
                    fall=float(rng.uniform(0, 0.1)),
                ))
        return rows
    from checks.reference import stress_rows
    rows = stress_rows(617391 if group == "fresh96" else 918273)
    return rows[:48] if group == "new48" else rows


@lru_cache(maxsize=1)
def _cached_legacy_finals_bundle():
    cases = []
    by_id = {}
    for group, filename, seed0, expected_count in FINALS_SUITE_SPECS:
        path = PROJECT / "validation" / filename
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))
        else:
            rows = _fallback_finals_rows(group)
        if len(rows) != expected_count:
            raise ValueError(f"RO finals matrix {group} expected {expected_count} cases, got {len(rows)}")
        for index, row in enumerate(rows):
            seed = int(seed0 + index)
            payload, meta, facts = _generate_finals_row(row, seed)
            rise, fall = row.get("edges", (row.get("rise", 0.0), row.get("fall", 0.0)))
            noise_model = row.get("noise_model")
            if noise_model == "multitone":
                mode = "multitone"
            elif noise_model == "white":
                mode = "gaussian_white"
            elif row.get("replay", 0):
                mode = "gaussian_replay"
            else:
                mode = "gaussian_lowpass"
            case_id = f"{group}-{index + 1:04d}"
            case = Case(
                id=case_id,
                suite="competition",
                group=group,
                wave=row["kind"],
                frequency_hz=float(row["frequency"]),
                vpp_v=float(row["vpp"]),
                dc_v=float(facts["truth"]["dc_v"]),
                duty=float(row.get("duty", 0.5)),
                rise=float(rise),
                fall=float(fall),
                phase=float(row["phase"]),
                noise_pp_ratio=float(row.get("noise", 0.0)),
                bandwidth_ratio=float(row.get("bandwidth", 5.0)),
                noise_mode=mode,
                fs_hz=float(row.get("fs", 20000.0)),
                vref_v=3.3,
                gain=1.0,
                flags=7,
                expectation="measure",
                source=json.dumps(row, sort_keys=True, separators=(",", ":")),
                impairment=str(row.get("scenario") or row.get("tag") or "finals_365"),
                inl_lsb=0.35 if row.get("adc_model") == "inl" else 0.0,
                seed=seed,
            )
            cases.append(case)
            by_id[case_id] = (case, row, payload, meta, facts)
    return tuple(cases), by_id


@lru_cache(maxsize=1)
def _cached_finals_bundle():
    from tools.identifiable_finals import load_rows
    cases, by_id = [], {}
    ordinals = Counter()
    for row in load_rows():
        group = "final_" + row["group"]
        ordinals[group] += 1
        case_id = f"{group}-{ordinals[group]:04d}"
        seed = int(row["seed"])
        payload, meta, facts = _generate_finals_row(row, seed)
        case = Case(id=case_id, suite="finals", group=group, wave=row["kind"],
                    frequency_hz=row["frequency"], vpp_v=row["vpp"], dc_v=facts["truth"]["dc_v"],
                    phase=row["phase"], duty=row["duty"], rise=row["rise"], fall=row["fall"],
                    noise_pp_ratio=row["noise"], bandwidth_ratio=row["bandwidth"],
                    noise_mode=row["noise_model"], fs_hz=row["fs"], seed=seed,
                    source=json.dumps(row, sort_keys=True, separators=(",", ":")),
                    impairment=row["tag"])
        cases.append(case)
        by_id[case_id] = case, row, payload, meta, facts
    return tuple(cases), by_id


def ro_matrix(seed=20261005, random_count=512):
    """Return both finals noise groups, including the strong 50% boundary."""
    del seed, random_count
    cases, _ = _cached_finals_bundle()
    return (case for case in cases
            if (100. <= case.frequency_hz <= 600. and case.noise_pp_ratio <= .5))


def _is_finals_case(case):
    return (isinstance(getattr(case, "source", None), str) and
            case.source.startswith("{") and
            getattr(case, "group", "") in ("new48", "fresh96", "paired221", "final_weak", "final_strong"))


def generate_ro(case):
    """Generate ADC payload, metadata and ground-truth facts for an RO case."""
    if not _is_finals_case(case):
        return generate(case)
    _, by_id = (_cached_finals_bundle() if case.group.startswith("final_")
                else _cached_legacy_finals_bundle())
    cached = by_id.get(case.id)
    if cached is not None and cached[0] == case and not case.fault:
        return cached[2], cached[3], cached[4]
    row = json.loads(case.source)
    payload, meta, facts = _generate_finals_row(row, case.seed)
    if case.fault == "invalid_code":
        import numpy as np
        samples = np.frombuffer(payload, dtype="<u2").copy()
        samples[111] = 4096
        payload = samples.tobytes()
    return payload, meta, facts


def close_record(case, adc):
    """Keep all RT/RO samples; append the remainder of the carrier's last cycle."""
    import numpy as np
    if _is_finals_case(case):
        row = json.loads(case.source)
        if row.get("noise_model") == "gaussian_notched_replay":
            # This record already closes source *and* interference. Appending
            # a carrier-only tail would reintroduce a spectral seam.
            return adc
        actual_fs = float(row.get("fs", case.fs_hz)) * (1.0 + float(row.get("clock_error_ppm", 0.0)) * 1e-6)
        f = float(case.frequency_hz)
        n = len(adc)
        cycles = math.ceil(n * f / actual_fs)
        length = round(cycles * actual_fs / f)
        extra = length - n
        if extra <= 0:
            return adc
        p = np.arange(n, dtype=float) * f / actual_fs + float(case.phase)
        clean = _finals_waveform(case.wave, p, case.duty, case.rise, case.fall)
        tail_p = np.arange(n, length, dtype=float) * f / actual_fs + float(case.phase)
        tail = _finals_waveform(case.wave, tail_p, case.duty, case.rise, case.fall)
        if case.inverted:
            clean, tail = -clean, -tail
        to_adc = case.gain * 4095.0 / case.vref_v
        baseline = (case.dc_v + 0.5 * case.vpp_v * clean) * to_adc
        tail_baseline = (case.dc_v + 0.5 * case.vpp_v * tail) * to_adc
        # Use the phase-aligned slice one carrier period earlier so periodic
        # replay=1 interference and INL remain continuous across the loop seam.
        period_samples = max(1, round(actual_fs / f))
        ref_start = max(0, n - period_samples)
        if ref_start + extra > n:
            ref_start = 0
        residual = adc[ref_start:ref_start + extra] - baseline[ref_start:ref_start + extra]
        tail_codes = np.clip(tail_baseline + residual, 1.0, 4094.0)
        return np.r_[adc, np.rint(tail_codes)]
    cycles = math.ceil(len(adc) * case.frequency_hz / case.fs_hz)
    length = round(cycles * case.fs_hz / case.frequency_hz)
    extra = length - len(adc)
    if extra <= 0:
        return adc
    t = np.arange(len(adc)) / case.fs_hz
    raw = distorted_shape(case, (case.phase + t * case.frequency_hz) % 1, t)
    clean, _, _ = clean_signal(case)
    slope = float(np.ptp(clean) / np.ptp(raw))
    intercept = float(clean.min() - slope * raw.min())
    tail_t = np.arange(len(adc), length) / case.fs_hz
    tail = distorted_shape(case, (case.phase + tail_t * case.frequency_hz) % 1, tail_t)
    tail = slope * tail + intercept
    if case.inverted:
        clean, tail = -clean, -tail
    to_adc = case.gain * 4095 / case.vref_v
    baseline = (case.dc_v + .5 * case.vpp_v * clean) * to_adc
    tail_baseline = (case.dc_v + .5 * case.vpp_v * tail) * to_adc
    if np.any((tail_baseline < 1) | (tail_baseline > 4094)):
        raise ValueError(f"RO cannot close {case.id} within the ADC envelope")
    # Carry the original noise/INL residual into the short appended tail.
    # Only the appended residual is bounded; original RT codes remain exact.
    residual = adc[:extra] - baseline[:extra]
    residual = np.clip(residual, 1 - tail_baseline, 4094 - tail_baseline)
    return np.r_[adc, np.rint(tail_baseline + residual)]


def board_levels():
    """Read this project's calibration without changing firmware or RT."""
    text = (Path(__file__).resolve().parents[1] / "acquisition_config.h").read_text()
    values = []
    for name in ("ACQ_REFERENCE_MICROVOLTS", "ACQ_FRONTEND_GAIN_PPM"):
        match = re.search(r"^\s*#define\s+" + name + r"\s+\(?([0-9]+)[uUlL]*\)?\s*$", text, re.M)
        if not match or int(match[1]) <= 0:
            raise ValueError(f"RO cannot read {name} from acquisition_config.h")
        values.append(int(match[1]) / 1e6)
    return tuple(values)


def encode(case, vref, gain, level_mode="adc", oversample=1):
    """Invert RT/RO ADC quantization, then encode signed little-endian DAC words."""
    import numpy as np
    if level_mode not in ("adc", "source"):
        raise ValueError("RO level mode must be adc or source")
    if not all(math.isfinite(x) and x > 0 for x in (vref, gain, case.fs_hz)):
        raise ValueError("RO calibration/sample rate must be positive and finite")
    payload, meta, facts = generate_ro(case)  # Includes original noise, distortion and INL.
    adc = np.frombuffer(payload, dtype="<u2").astype(float)
    n_original = len(adc)
    if n_original not in (host.SAMPLES, host.MAX_PAYLOAD_BYTES // 2) or np.any(adc > meta.adc_full_scale):
        raise ValueError(f"RO cannot replay invalid ADC samples: {case.id}")
    adc = close_record(case, adc)
    full_scale = vref / gain if level_mode == "adc" else case.vref_v / case.gain
    volts = adc * full_scale / meta.adc_full_scale
    if float(volts.min()) < 0 or float(volts.max()) > vref / gain:
        raise ValueError(f"RO {case.id} exceeds the board input range; use --ro-level-mode adc")
    dac = np.rint((2 * adc / meta.adc_full_scale - 1) * 32767).astype("<i2")
    # Round-trip check prevents a sign, byte order or factor-of-two mistake.
    restored = .5 * full_scale * (dac.astype(float) / 32767 + 1)
    error = float(np.max(np.abs(restored - volts)))
    if error > full_scale / (4 * 32767) + 1e-12:
        raise ValueError("RO DAC round-trip check failed")
    if oversample not in (1,8,16,32):
        raise ValueError('RO oversample must be 1, 8, 16 or 32')
    source_rate=case.fs_hz
    waveform_samples=len(adc)
    if oversample>1:
        if case.group not in ('final_weak','final_strong'):
            raise ValueError('high resolution playback requires the identifiable finals source')
        from tools.identifiable_finals import noise_samples
        row=json.loads(case.source)
        waveform_samples=int(row['samples'])*oversample
        source_rate=case.fs_hz*oversample
        phase=np.arange(waveform_samples)*case.frequency_hz/source_rate+row['phase']
        clean=_finals_waveform(row['kind'],phase,row['duty'],row['rise'],row['fall'])
        noise=noise_samples(row,case.seed)[0] if row['noise'] else np.zeros(int(row['samples']))
        # Interpolate only the explicitly bandlimited interference. Evaluate
        # the clean continuous source directly, retaining its original truth.
        noise=np.fft.irfft(np.fft.rfft(noise),n=waveform_samples)*oversample
        if row['noise']:
            noise*=row['noise']*row['vpp']/np.ptp(noise)
        source=(facts['truth']['dc_v']+.5*row['vpp']*clean+noise)*full_scale/(case.vref_v/case.gain)
        if np.any((source<0)|(source>vref/gain)):
            raise ValueError('high resolution source exceeds ADC input range')
        dac=np.rint((2*source/full_scale-1)*32767).astype('<i2')
        error=float(np.max(np.abs(.5*full_scale*(dac.astype(float)/32767+1)-source)))
        volts=source
    scale = full_scale / (case.vref_v / case.gain)
    truth = dict(facts["truth"])
    truth.update(vpp_v=truth["vpp_v"] * scale, dc_v=truth["dc_v"] * scale)
    if "wire_carrier_vpp_v" in truth:
        truth["wire_carrier_vpp_v"] = truth["wire_carrier_vpp_v"] * scale
    return payload, dac.tobytes(), {
        "rt_metadata": asdict(meta), "rt_generated": facts, "truth": truth,
        "level_mode": level_mode, "voltage_scale": scale,
        "amplitude_v": full_scale, "offset_v": .5 * full_scale,
        "sample_rate_hz": source_rate, "loop_frequency_hz": source_rate / waveform_samples,
        "waveform_samples": waveform_samples, "seam_bridge_samples": len(adc) - n_original,
        "source_oversample": oversample,
        "original_samples": n_original,
        "input_min_v": float(volts.min()), "input_max_v": float(volts.max()),
        "dac_roundtrip_error_v": error, "dac_encoding": "signed int16 little-endian",
        "note": ("Continuous clean source and periodic bandlimited noise evaluated at a finer DAC rate; "
                 "the original RT payload and clean truth are retained; R acquisition is asynchronous."
                 if oversample>1 else
                 "Original RT/RO codes are unchanged. Closed periodic source is replayed asynchronously.")}


def evaluate_ro(case, line, details, elapsed, facts=None):
    """Evaluate against the clean source truth; carrier fit is diagnostic only."""
    eval_case = replace(case, group="final_strong") if _is_finals_case(case) else case
    check = evaluate(eval_case, line, details, elapsed, facts)
    if case.group in ("final_weak", "final_strong"):
        check["limits"]["vpp_pct"] = .1
        # Strictly below 0.1%, using unrounded internal measurements.
        if check["vpp_error_pct"] >= .1 and "vpp_error_pct" not in check["failure_reasons"]:
            check["failure_reasons"].append("vpp_error_pct")
        check["passed"] = not check["failure_reasons"]
        check["verdict"] = "PASS" if check["passed"] else "FAIL"
    truth = (facts or {}).get("truth", {})
    if "wire_carrier_vpp_v" in truth and truth["wire_carrier_vpp_v"] > 0:
        wire_vpp = truth["wire_carrier_vpp_v"]
        wire_err_pct = abs(check["measured"]["vpp_v"] / wire_vpp - 1.0) * 100.0
        check["wire_carrier_vpp_error_pct"] = wire_err_pct
    return check


def select_ro_cases(args):
    """Select 100..600-Hz Finals cases, or explicitly selected historical IDs."""
    suite = getattr(args, "suite", "all")
    case_ids = list(getattr(args, "case_id", None) or [])
    seed = getattr(args, "seed", 20261005)
    random_count = getattr(args, "random_count", 512)
    wants_legacy = (
        suite == "preliminary" or
        "preliminary" in case_ids or
        any(cid.startswith("competition-") for cid in case_ids)
    )
    finals_pool = list(ro_matrix(seed, random_count))
    if any(token in ("new48", "fresh96", "paired221") or
           token.startswith(("new48-", "fresh96-", "paired221-")) for token in case_ids):
        # Historical counterexamples are only explicitly selectable.
        finals_pool += [c for c in _cached_legacy_finals_bundle()[0]
                        if TEST_FREQUENCY_MIN_HZ <= c.frequency_hz < TEST_FREQUENCY_MAX_EXCLUSIVE_HZ
                        and c.noise_pp_ratio < .5]
    if suite == "preliminary" or case_ids == ["preliminary"]:
        pool = list(matrix(seed, random_count))
    elif wants_legacy:
        pool = finals_pool + list(matrix(seed, random_count))
    else:
        pool = finals_pool

    def matches_suite(c):
        if suite in ("all", "competition", "finals"):
            return True
        if suite == "preliminary":
            return not _is_finals_case(c)
        return c.suite == suite or c.group == suite

    def matches_case_id(c):
        if not case_ids:
            return True
        if c.id in case_ids or c.group in case_ids:
            return True
        if "preliminary" in case_ids and not _is_finals_case(c):
            return True
        return False

    selected = [c for c in pool if matches_suite(c) and matches_case_id(c)]
    if case_ids:
        matched_tokens = ({c.id for c in selected} |
                          {c.group for c in selected} |
                          ({"preliminary"} if any(not _is_finals_case(c) for c in selected) else set()))
        if set(case_ids) - matched_tokens:
            raise ValueError("unknown case ID or case outside selected suite")
    if getattr(args, "max_cases", None) is not None:
        selected = selected[:args.max_cases]
    if not selected:
        raise ValueError("empty RO selection")
    return selected


def parameters(response, command):
    """Accept the instrument's normal headers or COMM_HEADER OFF replies."""
    response = response.strip()
    response = re.sub(r"^" + re.escape(command) + r"\s+", "", response, flags=re.I)
    return [word.strip().upper() for word in response.split(",")]


def numeric(words, key):
    try:
        value = words[words.index(key) + 1]
        match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)(?:V|HZ|SA/S)?", value)
        if not match:
            raise ValueError(value)
        result = float(match[1])
        if not math.isfinite(result):
            raise ValueError(value)
        return result
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"RO invalid {key} readback: {','.join(words)}") from error


WAVEFORM_RESPONSE_LIMIT = 64 * 1024 * 1024 + 4096


def waveform_header(prefix):
    """Read the instrument's header, never infer its LENGTH from the upload."""
    marker = re.search(rb"\bWAVEDATA\s*,", prefix, re.I)
    if marker is None:
        return None
    header = prefix[:marker.start()].decode("ascii", errors="replace")
    length = re.search(r"(?:^|,|\s)\s*LENGTH\s*,\s*([0-9]+)\s*(KB|MB|B)?\s*(?:,|$)",
                       header, re.I)
    declared = None
    if length:
        declared = int(length[1]) * {None: 1, "B": 1, "KB": 1024, "MB": 1024**2}[length[2].upper() if length[2] else None]
    # Keep the returned file identity. A rejected USER argument can be echoed
    # verbatim in POS while WVNM is empty, followed by unrelated default data.
    def field(key):
        match = re.search(r"(?:^|,|\s)" + key + r"\s*,\s*([^,]*)", header, re.I)
        return match[1].strip().strip('"') if match else None
    return {"header": header, "header_bytes": marker.end(), "declared_bytes": declared,
            "position": field("POS"), "waveform_name": field("WVNM"),
            "waveform_type": field("TYPE")}


def waveform_name(value):
    """Compare a returned filename, not an echoed query containing commas."""
    value = (value or "").strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1]
    if "," in value:
        return None
    for suffix in (".bin", ".arb"):
        if value.lower().endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def waveform_payload(raw):
    """Separate LENGTH bytes from optional CR/LF; keep binary trailing LF intact."""
    info = waveform_header(raw[:4096])
    if info is None:
        raise RuntimeError("RO WVDT readback has no WAVEDATA header in its first 4096 bytes")
    begin, declared = info["header_bytes"], info["declared_bytes"]
    if declared is None or declared <= 0:
        raise RuntimeError(f"RO WVDT readback has no positive byte LENGTH; header={info['header']!r}")
    body = raw[begin:]
    info.update(response_bytes=len(raw), received_body_bytes=len(body), framing="raw")
    # A leading '#' can also be a real DAC byte. Prefer exact raw LENGTH framing.
    if len(body) >= declared and body[declared:] in (b"", b"\n", b"\r\n"):
        data = body[:declared]
    else:
        # Also accept an explicit IEEE definite-length block, without ever stripping
        # arbitrary binary bytes or treating their embedded LF as message ends.
        digits = int(chr(body[1])) if len(body) > 1 and body[:1] == b"#" and 49 <= body[1] <= 57 else 0
        field = body[2:2 + digits]
        size = int(field) if digits and len(field) == digits and field.isdigit() else None
        start = 2 + digits
        if size != declared or len(body) < start + declared or body[start + declared:] not in (b"", b"\n", b"\r\n"):
            raise RuntimeError(f"RO WVDT readback length/framing differs: LENGTH={declared}, "
                               f"received_body={len(body)}; header={info['header']!r}")
        data = body[start:start + declared]
        info["framing"] = "IEEE definite-length block"
    info.update(payload_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return data, info


def verify_waveform(raw, expected, expected_name=None):
    data, info = waveform_payload(raw)
    info.update(uploaded_bytes=len(expected), uploaded_sha256=hashlib.sha256(expected).hexdigest(),
                payload_prefix_hex=data[:32].hex(), uploaded_prefix_hex=expected[:32].hex(),
                verified=False)
    if expected_name is not None and not any(
            waveform_name(info[key]) == expected_name for key in ("waveform_name", "position")):
        error = RuntimeError(f"RO WVDT did not resolve uploaded waveform {expected_name!r}: "
                             f"POS={info['position']!r}, WVNM={info['waveform_name']!r}, "
                             f"LENGTH={info['declared_bytes']}; query syntax/name must be resolved "
                             "before comparing waveform bytes")
        error.readback_info, error.unresolved_waveform = info, True
        raise error
    # An integer number of byte-exact repeats gives the identical infinite
    # sample stream in TrueArb at the same sample rate. Padding/resampling does not.
    repetitions = len(data) // len(expected) if expected and len(data) % len(expected) == 0 else 0
    if not repetitions or any(data[i:i + len(expected)] != expected
                              for i in range(0, len(data), len(expected))):
        first = next((i for i, (a, b) in enumerate(zip(data, expected)) if a != b), None)
        error = RuntimeError(f"RO waveform readback differs: uploaded={len(expected)} bytes, "
                             f"LENGTH={info['declared_bytes']}, payload={len(data)} bytes, "
                             f"first_different_byte={first}; header={info['header']!r}")
        error.readback_info = info
        raise error
    info.update(bytes=len(expected), readback_bytes=len(data), repetitions=repetitions,
                uploaded_sha256=hashlib.sha256(expected).hexdigest(), verified=True)
    return info


def xdr_words(*values):
    return struct.pack(f">{len(values)}I", *values)


def xdr_bytes(data):
    return xdr_words(len(data)) + data + b"\0" * (-len(data) % 4)


class XdrReader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ConnectionError("RO truncated VXI-11/XDR reply")
        data = self.data[self.offset:end]
        self.offset = end
        return data

    def word(self):
        return struct.unpack(">I", self.take(4))[0]

    def opaque(self):
        size = self.word()
        value = self.take(size)
        self.take(-size % 4)
        return value


class RpcConnection:
    """ONC RPC v2 over TCP, including TCP record fragments (RFC 5531)."""
    def __init__(self, address, port, program, timeout):
        self.program, self.timeout = program, timeout
        self.xid = int.from_bytes(os.urandom(4), "big")
        self.healthy = True
        self.sock = socket.create_connection((address, port), timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def receive(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC reply deadline exceeded")
            self.sock.settimeout(remaining)
            part = self.sock.recv(size - len(data))
            if not part:
                raise ConnectionError("VXI-11 RPC connection closed")
            data.extend(part)
        return bytes(data)

    def call(self, procedure, arguments=b"", version=1):
        self.xid = (self.xid + 1) & 0xffffffff
        # CALL, RPC version 2, program/version/procedure, AUTH_NULL credentials/verifier.
        payload = xdr_words(self.xid, 0, 2, self.program, version, procedure, 0, 0, 0, 0) + arguments
        deadline = time.monotonic() + self.timeout
        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(xdr_words(0x80000000 | len(payload)) + payload)
            result = bytearray()
            while True:
                marker = struct.unpack(">I", self.receive(4, deadline))[0]
                size = marker & 0x7fffffff
                if len(result) + size > 2 * 1024 * 1024:
                    raise ConnectionError("RO VXI-11 RPC reply exceeds 2 MiB")
                result.extend(self.receive(size, deadline))
                if marker & 0x80000000:
                    break
            reply = XdrReader(bytes(result))
            if reply.word() != self.xid or reply.word() != 1:
                raise ConnectionError("RO unexpected VXI-11 RPC transaction")
            if reply.word() != 0:
                raise ConnectionError("RO VXI-11 RPC request denied")
            reply.word()  # Verifier flavor.
            reply.opaque()
            status = reply.word()
            if status != 0:
                raise ConnectionError(f"RO VXI-11 RPC accept status {status}")
            return reply
        except (OSError, ValueError, struct.error) as error:
            self.healthy = False
            detail = f"VXI-11 RPC program=0x{self.program:x} procedure={procedure}: {error}"
            if isinstance(error, TimeoutError):
                raise TimeoutError(detail) from error
            raise ConnectionError(detail) from error

    def close(self):
        self.sock.close()


class Vxi11:
    """Small dependency-free VXI-11 core client for the instrument's inst0 link.

    Wire definitions: VXI-11 core program 0x0607af, create_link=10,
    device_write=11, device_read=12, destroy_link=23; END flag=8.
    No xdrlib dependency (removed in Python 3.13).
    """
    def __init__(self, address, port, timeout):
        self.address, self.port, self.timeout = address, port, timeout
        self.core, self.link, self.abort_port = None, None, 0
        self.io_ms = max(1, math.ceil(timeout * 1000))

    @staticmethod
    def check(reply, operation):
        error = reply.word()
        if error:
            if error == 15:
                raise TimeoutError(f"VXI-11 {operation}: instrument I/O timeout (15)")
            raise RuntimeError(f"VXI-11 {operation}: instrument error {error}")

    def open(self):
        mapper = RpcConnection(self.address, self.port, 100000, self.timeout)
        try:
            port = mapper.call(3, xdr_words(0x0607af, 1, 6, 0), version=2).word()
        finally:
            mapper.close()
        if not 1 <= port <= 65535:
            raise ConnectionError("RO VXI-11 core service is not registered on portmapper")
        self.core = RpcConnection(self.address, port, 0x0607af, self.timeout)
        try:
            client_id = int.from_bytes(os.urandom(4), "big") & 0x7fffffff
            reply = self.core.call(10, xdr_words(client_id, 0, 0) + xdr_bytes(b"inst0"))
            self.check(reply, "create_link(inst0)")
            self.link, self.abort_port, size = reply.word(), reply.word(), reply.word()
            if size <= 0:
                raise ConnectionError("RO invalid VXI-11 maxRecvSize")
            self.write_size = min(size, 1024 * 1024)
            return self
        except BaseException:
            self.close()
            raise

    def write(self, data):
        for offset in range(0, len(data), self.write_size):
            part = data[offset:offset + self.write_size]
            flags = 8 if offset + len(part) == len(data) else 0
            reply = self.core.call(11, xdr_words(self.link, self.io_ms, 0, flags) + xdr_bytes(part))
            self.check(reply, "device_write")
            size = reply.word()
            if size != len(part):
                raise ConnectionError(f"RO incomplete VXI-11 write: {size}/{len(part)} bytes")

    def read(self, limit=4096, text=False, on_chunk=None):
        result = bytearray()
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RO VXI-11 read deadline exceeded")
            capacity = limit - len(result)
            if capacity <= 0:
                raise RuntimeError(f"RO VXI-11 response exceeds {limit} bytes")
            # TermChar is enabled only for ASCII. Binary readback relies on RX_END.
            reply = self.core.call(12, xdr_words(self.link, min(capacity, 65536),
                max(1, math.ceil(remaining * 1000)), 0, 128 if text else 0, 10 if text else 0))
            self.check(reply, "device_read")
            reason, part = reply.word(), reply.opaque()
            if len(part) > capacity:
                raise ConnectionError("RO oversized VXI-11 device_read response")
            result.extend(part)
            if on_chunk is not None:
                on_chunk(part, len(result), reason)
            if reason & 4 or (text and reason & 2):
                return bytes(result)
            if not part:
                raise ConnectionError("RO VXI-11 read returned no data and no END")

    def close(self):
        if self.core is not None:
            try:
                if self.link is not None and self.core.healthy:
                    self.core.call(23, xdr_words(self.link))
            finally:
                self.core.close()
                self.core, self.link = None, None

    def discard(self):
        # A timed-out reply must never be consumed as a later query's answer.
        if self.core is not None:
            self.core.close()
            self.core = None


class VisaTransport:
    """SCPI transport for VISA USBTMC resources.

    VISA owns the USBTMC framing, including bulk transfers and the device END
    indication.  The RO layer only needs the same byte-oriented interface as
    :class:`Vxi11`, so binary WVDT responses remain unmodified.
    """

    def __init__(self, resource, timeout, backend=None):
        if not resource:
            raise ValueError("RO VISA transport requires --ro-resource")
        self.resource_name = resource
        self.timeout = timeout
        self.backend = backend
        self.rm = None
        self.instrument = None

    def open(self):
        try:
            import pyvisa
        except ImportError as error:
            raise RuntimeError("RO USB requires pyvisa; install pyvisa and a VISA/USBTMC backend") from error
        try:
            self.rm = pyvisa.ResourceManager(self.backend) if self.backend else pyvisa.ResourceManager()
            self.instrument = self.rm.open_resource(self.resource_name)
            self.instrument.timeout = max(1, math.ceil(self.timeout * 1000))
            # The generator's SCPI commands are LF terminated.  Raw reads are
            # used deliberately because arbitrary waveform bytes may contain LF.
            self.instrument.write_termination = "\n"
            self.instrument.read_termination = None
            self.instrument.send_end = True
            return self
        except Exception:
            self.close()
            raise

    def write(self, data):
        if self.instrument is None:
            raise ConnectionError("RO VISA resource is not open")
        written = self.instrument.write_raw(data)
        if written != len(data):
            raise ConnectionError(f"RO incomplete VISA write: {written}/{len(data)} bytes")

    def read(self, limit=4096, text=False, on_chunk=None):
        if self.instrument is None:
            raise ConnectionError("RO VISA resource is not open")
        from pyvisa.constants import StatusCode
        from pyvisa.errors import VisaIOError
        result = bytearray()
        deadline = time.monotonic() + self.timeout
        original_timeout = self.instrument.timeout
        try:
            with self.instrument.ignore_warning(StatusCode.success_max_count_read):
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("RO VISA read deadline exceeded")
                    capacity = limit - len(result)
                    if capacity <= 0:
                        raise RuntimeError(f"RO VISA response exceeds {limit} bytes")
                    self.instrument.timeout = max(1, math.ceil(remaining * 1000))
                    part, status = self.instrument.visalib.read(
                        self.instrument.session, min(capacity, 65536))
                    if len(part) > capacity:
                        raise ConnectionError("RO oversized VISA read response")
                    result.extend(part)
                    ended = status != StatusCode.success_max_count_read
                    if on_chunk is not None:
                        on_chunk(part, len(result), 4 if ended else 1)
                    if ended:
                        return bytes(result)
                    if not part:
                        raise ConnectionError("RO VISA read returned no data and no END")
        except VisaIOError as error:
            if error.error_code == StatusCode.error_timeout:
                raise TimeoutError(f"VISA read timed out for {self.resource_name}") from error
            raise
        finally:
            self.instrument.timeout = original_timeout

    def close(self):
        instrument, manager = self.instrument, self.rm
        self.instrument = self.rm = None
        if instrument is not None:
            try:
                instrument.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.close()
            except Exception:
                pass


class Generator:
    """SDG6022X-E SCPI generator over VXI-11 or VISA USBTMC."""
    def __init__(self, address, port=111, channel=1, timeout=10.,
                 transport="vxi11", resource=None, visa_backend=None):
        if channel not in (1, 2) or not 1 <= port <= 65535:
            raise ValueError("RO invalid generator channel/port")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RO timeout must be positive and finite")
        if transport not in ("vxi11", "visa"):
            raise ValueError("RO transport must be vxi11 or visa")
        self.address, self.port, self.timeout = address, port, timeout
        self.transport_name = transport
        self.resource = resource
        self.visa_backend = visa_backend
        self.channel = f"C{channel}"
        self.name = f"RO_C{channel}"
        if transport == "vxi11" and port in (5024, 5025):
            raise ValueError("RO now uses VXI-11: remove --ro-port 5025/5024 or use --ro-port 111")
        self.transport = None
        self.controlled = False
        self.shutdown_error = ""
        self.stage, self.command = "connect", ("VISA USBTMC" if transport == "visa" else "VXI-11 inst0")
        self.failure = None
        self.trace = None
        self.shutting_down = False
        self.readback_info = None
        self.readback_command = None
        self.readback_supported = None
        self.readback_attempts = []
        self.stderr = None
        self.playback_calibration = None

    @property
    def transport_label(self):
        return "VISA USBTMC" if self.transport_name == "visa" else "VXI-11"

    @property
    def resource_label(self):
        return self.resource if self.transport_name == "visa" else f"TCPIP::{self.address}::inst0::INSTR"

    def record(self, event, **details):
        if self.trace is not None:
            self.trace.write(json.dumps({"time_ns": time.time_ns(), "stage": self.stage,
                "command": self.command, "event": event, **details}, ensure_ascii=False) + "\n")
            self.trace.flush()

    def operation(self, command, action):
        self.command = command
        self.record("start")
        start = time.perf_counter()
        try:
            result = action()
            details = {"duration_s": time.perf_counter() - start}
            if isinstance(result, bytes):
                details.update(bytes=len(result), sha256=hashlib.sha256(result).hexdigest())
            elif isinstance(result, str):
                details["response"] = result
            self.record("ok", **details)
            return result
        except Exception as error:
            detail = f"RO {self.stage}; command={command}; {error}"
            if self.failure is None and not self.shutting_down:
                self.failure = {"stage": self.stage, "command": command, "error": detail}
            self.record("error", error=detail, duration_s=time.perf_counter() - start)
            if isinstance(error, TimeoutError):
                raise TimeoutError(detail) from error
            raise RuntimeError(detail) from error

    def send(self, command):
        self.operation(command, lambda: self.transport.write(command.encode("ascii") + b"\n"))

    def query(self, command):
        def execute():
            self.transport.write(command.encode("ascii") + b"\n")
            return self.transport.read(text=True).decode("ascii").strip()
        return self.operation(command, execute)

    def query_binary(self, command, limit=WAVEFORM_RESPONSE_LIMIT, record_path=None):
        def execute():
            prefix = bytearray()
            self.readback_info = None
            with (Path(record_path).open("wb") if record_path else nullcontext()) as sink:
                def observe(part, received, reason):
                    if sink is not None:
                        sink.write(part)
                        sink.flush()
                    if len(prefix) < 4096:
                        prefix.extend(part[:4096 - len(prefix)])
                    if self.readback_info is None:
                        info = waveform_header(bytes(prefix))
                        if info is not None:
                            self.readback_info = info
                            self.record("waveform_header", **info)
                    if self.readback_info is not None:
                        self.readback_info.update(received_bytes=received, vxi_reason=reason)
                self.transport.write(command.encode("ascii") + b"\n")
                return self.transport.read(limit=limit, on_chunk=observe)
        return self.operation(command, execute)

    def ready(self):
        if self.query("*OPC?") != "1":
            raise RuntimeError("RO generator did not acknowledge operation completion")

    def read_waveform(self, data, readback_dir=None):
        """Use the vendor's Python spelling; resolve compatibility once per run."""
        if self.readback_supported is False:
            return self.verify_uploaded_file(data, readback_dir)
        commands = [self.readback_command] if self.readback_command else [
            f"WVDT? user,{self.name}",
            f'WVDT? user,"Local",{self.name}',
            f'WVDT? USER,"Local",{self.name}',
            f"WVDT? USER,{self.name}",
        ]
        attempts, last_error, raw = [], None, None
        path = readback_dir / "generator.readback.bin" if readback_dir else None
        try:
            for index, command in enumerate(commands):
                if index and path is not None and path.exists():
                    path.replace(readback_dir / f"generator.readback.attempt-{index:02d}.bin")
                raw = None
                raw = self.query_binary(command, record_path=path)
                try:
                    info = verify_waveform(raw, data, expected_name=self.name)
                except RuntimeError as error:
                    self.readback_info = getattr(error, "readback_info", self.readback_info)
                    attempts.append({"query": command, "error": str(error),
                                     "readback": self.readback_info})
                    self.record("waveform_query_rejected", query=command, error=str(error),
                                readback=self.readback_info)
                    if not getattr(error, "unresolved_waveform", False):
                        raise
                    last_error = error
                    if self.stderr is not None and index + 1 < len(commands):
                        print(f"RO: {command} did not resolve {self.name}; trying explicit query",
                              file=self.stderr, flush=True)
                    continue
                info.update(query=command, query_attempts=attempts)
                self.readback_command, self.readback_info = command, info
                self.readback_supported = True
                self.record("waveform_verified", **info)
                return info
            if (self.identity.rsplit(",", 1)[-1].strip() == "6.01.01.37R6" and
                    attempts and all(a["readback"] and
                        a["readback"]["position"] == "USER" and
                        not a["readback"]["waveform_name"] for a in attempts)):
                self.readback_supported = False
                self.readback_attempts = attempts
                if self.stderr is not None:
                    print("RO: this firmware does not resolve USER waveform byte queries; "
                          "verifying acknowledged upload, user file and playback settings "
                          "(byte readback is unavailable)", file=self.stderr, flush=True)
                return self.verify_uploaded_file(data, readback_dir)
            raise last_error
        except BaseException as error:
            self.readback_info = getattr(error, "readback_info", self.readback_info)
            if self.readback_info is not None:
                self.readback_info = dict(self.readback_info, query=self.command,
                                          query_attempts=attempts)
            self.record("waveform_verification_error", error=f"{type(error).__name__}: {error}",
                        readback=self.readback_info)
            raise
        finally:
            if readback_dir is not None:
                atomic_json(readback_dir / "generator.readback.json", {
                    "complete": raw is not None, "uploaded_bytes": len(data),
                    "response_limit_bytes": WAVEFORM_RESPONSE_LIMIT, "readback": self.readback_info})

    def verify_uploaded_file(self, data, readback_dir=None):
        """Firmware compatibility path; explicitly distinguish it from byte equality."""
        words = parameters(self.query("STL? USER"), "STL")
        if not words or words[0] != "WVNM" or not any(
                waveform_name(name) == self.name for name in words[1:]):
            raise RuntimeError(f"RO uploaded waveform {self.name!r} is missing from user files")
        info = {"query": "STL? USER", "waveform_name": self.name, "user_files": words[1:],
                "uploaded_bytes": len(data), "uploaded_sha256": hashlib.sha256(data).hexdigest(),
                "verified": False, "byte_readback_supported": False,
                "configuration_verified": True,
                "verification": f"{self.transport_label} write acknowledgement, OPC, user file and playback settings",
                "query_attempts": self.readback_attempts}
        self.readback_info = info
        self.record("waveform_upload_confirmed", **info)
        if readback_dir is not None:
            atomic_json(readback_dir / "generator.readback.json", {
                "complete": True, "uploaded_bytes": len(data), "readback": info})
        return info

    def __enter__(self):
        try:
            if self.transport_name == "visa":
                self.transport = VisaTransport(self.resource, self.timeout, self.visa_backend)
                self.operation(f"VISA resource {self.resource}", self.transport.open)
            else:
                self.transport = Vxi11(self.address, self.port, self.timeout)
                self.operation(f"{self.address}:{self.port} portmapper / inst0", self.transport.open)
            self.stage = "identify instrument"
            self.identity = self.query("*IDN?")
            if not re.search(r"\bSDG6022X(?:-E)?\b", self.identity, re.I):
                raise RuntimeError(f"RO unexpected generator: {self.identity}")
            self.controlled = True
            self.stage = "initialize output"
            for command in ("OUTP OFF", "OUTP LOAD,HZ,PLRT,NOR", "MDWV STATE,OFF",
                            "SWWV STATE,OFF", "BTWV STATE,OFF", "NOISE_ADD STATE,OFF",
                            "CMBN OFF"):
                self.send(f"{self.channel}:{command}")
            self.ready()
            # Waveform combining adds the other channel even when its BNC
            # output is OFF. It must be disabled before independent references.
            if parameters(self.query(f"{self.channel}:CMBN?"), f"{self.channel}:CMBN") != ["OFF"]:
                raise RuntimeError("RO generator waveform combining is not disabled")
            return self
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def configure(self, data, config, readback_dir=None):
        c = self.channel
        self.readback_info = None
        self.stage = "upload waveform"
        self.send(f"{c}:OUTP OFF")
        self.send(f"{c}:BSWV WVTP,ARB")
        # Select point-by-point playback before loading and querying the wave.
        self.send(f"{c}:SRATE MODE,TARB,VALUE,{config['sample_rate_hz']:.12g},INTER,HOLD")
        # Vendor WVDT/write_raw example: ASCII header + signed int16 LE data.
        # No extra LF, TYPE or IEEE # block is part of this binary payload.
        header = (f"{c}:WVDT WVNM,{self.name},"
                  f"FREQ,{config['loop_frequency_hz']:.12g},"
                  f"AMPL,{config['amplitude_v']:.12g},OFST,{config['offset_v']:.12g},"
                  "PHASE,0,WAVEDATA,")
        self.record("binary_upload", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.operation(header + f"<{len(data)} binary bytes>",
                       lambda: self.transport.write(header.encode("ascii") + data))
        self.ready()
        self.stage = "configure TrueArb"
        self.send(f"{c}:ARWV NAME,{self.name}")
        self.send(f"{c}:SRATE MODE,TARB,VALUE,{config['sample_rate_hz']:.12g},INTER,HOLD")
        self.send(f"{c}:BSWV AMP,{config['amplitude_v']:.12g},OFST,{config['offset_v']:.12g}")
        self.ready()
        arb = parameters(self.query(f"{c}:ARWV?"), f"{c}:ARWV")
        rate = parameters(self.query(f"{c}:SRATE?"), f"{c}:SRATE")
        wave = parameters(self.query(f"{c}:BSWV?"), f"{c}:BSWV")
        if "NAME" not in arb or waveform_name(arb[arb.index("NAME") + 1]) != self.name:
            raise RuntimeError("RO generator did not select the uploaded waveform")
        if "MODE" not in rate or rate[rate.index("MODE") + 1] != "TARB":
            raise RuntimeError("RO generator did not enter TrueArb mode")
        if "INTER" not in rate or rate[rate.index("INTER") + 1] != "HOLD":
            raise RuntimeError("RO generator did not select zero-order hold")
        if "WVTP" not in wave or wave[wave.index("WVTP") + 1] != "ARB":
            raise RuntimeError("RO generator did not select ARB output")
        for words, key, expected in ((rate, "VALUE", config["sample_rate_hz"]),
                (wave, "AMP", config["amplitude_v"]), (wave, "OFST", config["offset_v"])):
            if not math.isclose(numeric(words, key), expected, rel_tol=1e-5, abs_tol=1e-6):
                raise RuntimeError(f"RO generator {key} differs from requested value")
        output = parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")
        if "LOAD" not in output or output[output.index("LOAD") + 1] != "HZ":
            raise RuntimeError("RO generator load is not Hi-Z")
        if "PLRT" not in output or output[output.index("PLRT") + 1] != "NOR":
            raise RuntimeError("RO generator polarity is not normal")
        self.stage = "verify waveform bytes"
        readback_dir = Path(readback_dir) if readback_dir is not None else None
        waveform = self.read_waveform(data, readback_dir)
        self.stage = "enable output"
        self.send(f"{c}:OUTP ON")
        self.ready()
        if parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")[0] != "ON":
            raise RuntimeError("RO generator output is not enabled")
        return {"arb": arb, "rate": rate, "wave": wave, "output": output,
                "waveform": waveform,
                "transport": self.transport_label}

    def reference(self, wave, offset_v, amplitude_v=0., frequency_hz=0.):
        """Configure independent built-in references, never an RT matrix case."""
        if wave not in ("dc", "sine"):
            raise ValueError("unsupported calibration reference")
        c, kind = self.channel, "DC" if wave == "dc" else "SINE"
        self.stage = "configure calibration reference"
        self.send(f"{c}:OUTP OFF")
        self.send(f"{c}:SRATE MODE,DDS")
        command = f"{c}:BSWV WVTP,{kind},OFST,{offset_v:.12g}"
        if wave == "sine":
            command += f",FRQ,{frequency_hz:.12g},AMP,{amplitude_v:.12g}"
        self.send(command)
        self.ready()
        words = parameters(self.query(f"{c}:BSWV?"), f"{c}:BSWV")
        if "WVTP" not in words or words[words.index("WVTP") + 1] != kind:
            raise RuntimeError("RO generator did not select the calibration reference")
        expected = [("OFST", offset_v)]
        if wave == "sine":
            expected += [("FRQ", frequency_hz), ("AMP", amplitude_v)]
        for key, value in expected:
            if not math.isclose(numeric(words, key), value, rel_tol=1e-5, abs_tol=1e-6):
                raise RuntimeError(f"RO calibration reference {key} readback differs")
        self.send(f"{c}:OUTP ON")
        self.ready()
        if parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")[0] != "ON":
            raise RuntimeError("RO calibration reference output is not enabled")
        return {"wave": wave, "offset_v": numeric(words, "OFST"),
                "amplitude_v": numeric(words, "AMP") if wave == "sine" else 0.,
                "frequency_hz": numeric(words, "FRQ") if wave == "sine" else 0.}

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None and self.failure is None:
            self.failure = {"stage": self.stage, "command": self.command,
                            "error": f"{type(exc).__name__}: {exc}"}
        if exc is not None:
            self.record("failure", failure=self.failure)
        self.shutting_down = True
        self.stage = "shutdown output"
        if self.transport is not None:
            try:
                if self.controlled:
                    if exc is not None:
                        # Reopen after failure and discard pending responses before OUTP OFF.
                        old = self.transport
                        old_link = getattr(old, "link", None)
                        if self.transport_name == "visa":
                            old.close()
                            self.transport = VisaTransport(self.resource, self.timeout, self.visa_backend)
                            self.operation("reconnect VISA USBTMC for OUTP OFF", self.transport.open)
                            self.operation("clear pending USBTMC responses", self.transport.instrument.clear)
                        else:
                            old.discard()
                            self.transport = Vxi11(self.address, self.port, self.timeout)
                            self.operation("reconnect VXI-11 for OUTP OFF", self.transport.open)
                    self.send(f"{self.channel}:OUTP OFF")
                    self.ready()
                    if parameters(self.query(f"{self.channel}:OUTP?"), f"{self.channel}:OUTP")[0] != "OFF":
                        raise RuntimeError("RO generator did not confirm output OFF")
                    if exc is not None and old_link is not None and self.transport_name == "vxi11":
                        # Release the old logical link on the healthy core connection.
                        self.transport.core.call(23, xdr_words(old_link))
            except Exception as error:
                self.shutdown_error = f"{type(error).__name__}: {error}"
                if exc is None:
                    raise
            finally:
                try:
                    self.transport.close()
                except Exception as error:
                    self.record("close_error", error=f"{type(error).__name__}: {error}")
                self.transport = None
        self.shutting_down = False


def calibrate_link(service, store, args, stderr, instrument, report, vref, gain):
    """Verify the analogue connection and calibrate before any matrix verdict."""
    from tools.acquisition_calibration import fit_calibration
    observations = []
    info = {"complete": False, "captures": [], "calibration": None,
            "error": "", "references_independent_of_matrix": True}
    path = report / "calibration.json"
    atomic_json(path, info)
    try:
        print("RO: checking board metadata and calibrating the ADC with independent references",
              file=stderr, flush=True)
        instrument.stage, instrument.command = "calibration metadata acquisition", "UART R (output OFF)"
        _, meta, record = service.capture(args.ro_timeout)
        record.info["context"]["ro_calibration"] = {"role": "metadata only; output OFF"}
        record.save()
        info["metadata_capture"] = record.path.relative_to(store.path).as_posix()
        if (not math.isclose(meta.reference_microvolts / 1e6, vref, abs_tol=1e-6) or
                not math.isclose(meta.gain_ppm / 1e6, gain, abs_tol=1e-6)):
            raise ValueError("RO board voltage/gain metadata differs from acquisition_config.h; "
                             "use matching board firmware before driving the matrix")
        full_scale = vref / gain
        references = [("dc", .15, 0., False), ("dc", .5, 0., False),
                      ("dc", .85, 0., False), ("sine", .5, 100., False),
                      ("sine", .5, 400., False), ("dc", .3, 0., True),
                      ("dc", .7, 0., True), ("sine", .5, 250., True)]
        for index, (wave, level, frequency, validation) in enumerate(references, 1):
            ref = instrument.reference(wave, level * full_scale, .3 * full_scale, frequency)
            print(f"RO calibration {index}/{len(references)}: {wave} {frequency:g} Hz, "
                  f"offset {ref['offset_v']:.6g} V", file=stderr, flush=True)
            time.sleep(max(.15, args.ro_settle))
            instrument.stage, instrument.command = "calibration reference acquisition", "UART R reference"
            payload, meta, record = service.capture(args.ro_timeout)
            context = {"reference": ref, "validation": validation,
                       "capture": record.path.relative_to(store.path).as_posix(), "metadata": asdict(meta)}
            record.info["context"]["ro_calibration"] = context
            record.save()
            observations.append({"payload": payload, "meta": meta, "reference": ref,
                                 "validation": validation})
            info["captures"].append(context)
            atomic_json(path, info)
        calibration, verification = fit_calibration(observations)
        info.update(complete=True, calibration=calibration.to_dict(), verification=verification,
                    algorithm_parameters_file=str(service.parameters_path))
        atomic_json(path, info)
        service.publish_calibration(path)
        exact_rate = calibration.sample_rate_hz_exact or (calibration.sample_rate_millihz / 1000)
        print(f"RO calibration OK: sample rate {exact_rate:.8g} Hz, "
              f"ADC scale {calibration.adc_scale:.8g}, offset {calibration.adc_offset_counts:.6g} counts",
              file=stderr, flush=True)
        print(f"RO calibration saved for R: {service.parameters_path}", file=stderr, flush=True)
        return info
    except BaseException as error:
        info["complete"] = False
        info["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        atomic_json(path, info)


def calibrate_playback(service, store, args, stderr, instrument, report, vref, gain):
    """Validate ARB separately from the ADC calibration used in competition R."""
    from tools.acquisition_calibration import counts
    from tools.generator_calibration import reference, fit_playback, verify_playback
    full_scale = vref / gain
    path = report / "generator_calibration.json"
    info = {"complete": False, "captures": [], "calibration": None, "error": "",
            "references_independent_of_matrix": True,
            "note": "ARB command transfer only; never applied to competition R parameters."}
    atomic_json(path, info)
    observations = []

    def capture_ref(wave, level, offset, validation, profile=None):
        data, config, expected = reference(full_scale, level, wave=wave, offset_v=offset)
        requested = dict(config)
        if profile is not None:
            config = profile.apply(config)
        directory = report / "generator-references" / f"{len(info['captures']) + 1:02d}"
        directory.mkdir(parents=True)
        print(f"RO ARB calibration {len(info['captures']) + 1}/8: "
              f"{'check' if validation else 'fit'} {wave}, expected DC {expected['dc_v']:.6g} V, "
              f"AMP {config['amplitude_v']:.6g} V, OFST {config['offset_v']:.6g} V",
              file=stderr, flush=True)
        (directory / "generator.i16.bin").write_bytes(data)
        config["readback"] = instrument.configure(data, config, readback_dir=directory)
        atomic_json(directory / "configuration.json", {"requested": requested, "sent": config,
                                                       "reference": expected, "validation": validation})
        time.sleep(max(.15, args.ro_settle))
        instrument.stage, instrument.command = "ARB calibration acquisition", "UART R reference"
        payload, meta, record = service.capture(args.ro_timeout)
        context = {"reference": expected, "configuration": config, "validation": validation,
                   "capture": record.path.relative_to(store.path).as_posix()}
        acquisition = service.calibration
        adjusted, effective = acquisition.apply(payload, meta)
        effective = replace(effective,
                            sample_rate_hz_exact=(acquisition.sample_rate_hz_exact or
                                                  acquisition.sample_rate_millihz / 1000))
        samples = counts(payload)
        # Fit the small OFST control slope from the unrounded calibrated mean.
        # Re-quantizing every sample first can bias a short voltage difference.
        measured_dc = ((float(samples.mean()) - acquisition.adc_offset_counts) / acquisition.adc_scale *
                       full_scale / effective.adc_full_scale)
        if acquisition.vpp_transfer is not None:
            from tools.amplitude_calibration import voltage_values
            measured_dc = float(voltage_values(samples,acquisition.vpp_transfer).mean())
        context["measured_dc_v"] = measured_dc
        context["adc_std_counts"] = float(samples.std()) / acquisition.adc_scale
        context["metadata"] = asdict(meta)
        record.info["context"]["generator_calibration"] = context
        record.save()
        info["captures"].append(context)
        atomic_json(path, info)
        return {"payload": adjusted, "meta": effective, "config": requested, "reference": expected,
                "measured_dc_v": measured_dc}

    try:
        print("RO: checking independent ARB voltage references", file=stderr, flush=True)
        # The SDG's OFST response changes over large excursions. Calibrate the
        # local control response around the 50%-scale offset used by the matrix,
        # rather than combining distant 30%/70% ranges into one global affine fit.
        references = [(.15, .5), (.5, .5), (.85, .5), (.5, .48), (.5, .52)]
        for level, offset in references:
            observations.append(capture_ref("dc", level, offset * full_scale, False))
        profile, verification = fit_playback(observations, full_scale)
        checks = []
        for wave, level in (("dc", .3), ("dc", .7), ("sine", .5)):
            check = capture_ref(wave, level, .5 * full_scale, True, profile)
            checks.append(verify_playback(check["payload"], check["meta"], check["reference"]))
        instrument.playback_calibration = profile
        info.update(complete=True, calibration=asdict(profile),
                    verification={**verification, "independent_checks": checks})
        print(f"RO ARB calibration OK: amplitude gain {profile.amplitude_gain:.8g}, "
              f"offset gain {profile.offset_gain:.8g}, offset bias {profile.offset_bias_v:.6g} V",
              file=stderr, flush=True)
    except BaseException as error:
        info["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        atomic_json(path, info)


def run(service, store, args, stderr, *, calibration_only=False):
    """Replay the identifiable weak/strong finals matrix and measure through R.

    ``service`` may be None.  The board link is only needed for the R leg, so
    without it RO still generates, encodes, uploads and readback-verifies every
    selected case and archives the full encoding, then reports mode=encode-only.
    """
    if not math.isfinite(args.ro_settle) or not 0 <= args.ro_settle <= 10:
        raise ValueError("RO settling time must be finite and between 0 and 10 seconds")
    measured = service is not None
    if calibration_only and not measured:
        raise ValueError("RS requires an attached ADC board (--port) and generator")
    generator = Generator(args.ro_host, args.ro_port, args.ro_channel, args.ro_timeout,
                          transport=getattr(args, "ro_transport", "vxi11"),
                          resource=getattr(args, "ro_resource", None),
                          visa_backend=getattr(args, "ro_visa_backend", None))
    generator.stderr = stderr
    selected = [] if calibration_only else select_ro_cases(args)
    vref, gain = board_levels()
    prepared = [encode(case, vref, gain, args.ro_level_mode,getattr(args,'ro_oversample',1)) for case in selected]
    report = store.path / (("rs-" if calibration_only else "ro-") + str(time.time_ns()))
    report.mkdir()
    if not calibration_only:
        from tools.identifiable_finals import DESIGN_SEED, REVISION
        atomic_json(report / "matrix.json", {"schema": 1, "seed": DESIGN_SEED, "revision": REVISION,
            "cases": [asdict(c) for c in selected]})
    results, counts = [], Counter()
    started, completed = time.perf_counter(), False
    error_text, failed_case = "", None
    calibration_snapshot = playback_snapshot = None
    previous_calibration_mode = service.use_saved_calibration if measured else None
    parameters_before = (service.parameters_path.read_text(encoding='utf-8')
                         if measured and service.parameters_path.exists() else None)
    profile_before = service.calibration_profile if measured else None
    if not measured:
        print("RO: no board attached; encoding and uploading the matrix, R is not sent",
              file=stderr, flush=True)
    try:
        with (report / "scpi.jsonl").open("w", encoding="utf-8", buffering=1) as trace:
            generator.trace = trace
            if generator.transport_name == "visa":
                print(f"RO: VISA USBTMC {args.ro_resource}", file=stderr, flush=True)
            else:
                print(f"RO: VXI-11 {args.ro_host}:{args.ro_port} / inst0", file=stderr, flush=True)
            with generator as instrument, (report / "results.jsonl").open("w", encoding="utf-8") as journal:
                if measured:
                    reuse_report = None if calibration_only else getattr(args, 'ro_reuse_reference_report', None)
                    calibration_mode = 'auto' if calibration_only else getattr(args, 'ro_calibrate', 'saved')
                    if calibration_mode == 'saved' and not reuse_report:
                        profile = service.refresh_calibration()
                        if profile is None:
                            raise ValueError('RO requires saved calibration; run RS first')
                        reuse_report = str(Path(profile.source['calibration_report']).parent)
                    if reuse_report:
                        import shutil
                        from tools.generator_calibration import GeneratorCalibration
                        reference_dir=Path(reuse_report).resolve()
                        calibration_snapshot=json.loads((reference_dir/'calibration.json').read_text())
                        playback_snapshot=json.loads((reference_dir/'generator_calibration.json').read_text())
                        amp_reference=json.loads((reference_dir/'amplitude_calibration.json').read_text())
                        arb_reference=json.loads((reference_dir/'arb_voltage_transfer.json').read_text())
                        if not all(x.get('complete') and not x.get('error') for x in
                                   (calibration_snapshot,playback_snapshot,amp_reference,arb_reference)):
                            raise ValueError('cannot reuse failed independent references')
                        for filename in ('calibration.json','generator_calibration.json',
                                         'amplitude_calibration.json','arb_voltage_transfer.json'):
                            shutil.copy2(reference_dir/filename,report/filename)
                        service.publish_calibration(report/'calibration.json')
                        service.use_saved_calibration=True
                        instrument.playback_calibration=GeneratorCalibration(**playback_snapshot['calibration'])
                        instrument.playback_transfer=arb_reference['transfer']
                        atomic_json(report/'reference_provenance.json',dict(source_report=str(reference_dir),
                                    note='Reused independent references; fresh matrix ADC samples follow'))
                    elif calibration_mode == "auto":
                        service.use_saved_calibration = True
                        calibration_snapshot = calibrate_link(service, store, args, stderr, instrument, report, vref, gain)
                        if getattr(args,'ro_vpp_calibrate',False):
                            from tools.dynamic_amplitude_calibration import calibrate_adc_transfer
                            calibrate_adc_transfer(service,store,args,stderr,instrument,report,
                                                   calibration_snapshot,vref/gain)
                        playback_snapshot = calibrate_playback(service, store, args, stderr, instrument, report, vref, gain)
                        if getattr(args,'ro_vpp_calibrate',False):
                            from tools.dynamic_amplitude_calibration import calibrate_dac_transfer
                            calibrate_dac_transfer(service,store,args,stderr,instrument,report,vref/gain)
                    else:
                        service.use_saved_calibration = False
                if not calibration_only:
                    _run_cases(service, store, args, stderr, instrument, journal, report,
                               selected, prepared, results, counts, vref, gain, measured)
            completed = True
    except BaseException as error:
        completed = False
        error_text = f"{type(error).__name__}: {error}"
        failed_case = (results[-1]['case_id'] if results and not results[-1]['result']['passed']
                       else selected[len(results)].id if len(results) < len(selected) else None)
        raise
    finally:
        if measured:
            service.use_saved_calibration = previous_calibration_mode
            if calibration_only and not completed:
                # A partially successful RS must not replace the working profile.
                if parameters_before is None:
                    service.parameters_path.unlink(missing_ok=True)
                else:
                    atomic_text(service.parameters_path, parameters_before)
                service.calibration_profile = profile_before
        generator.trace = None
        if isinstance(calibration_snapshot, dict):
            atomic_json(report / "calibration.json", calibration_snapshot)
        if isinstance(playback_snapshot, dict):
            atomic_json(report / "generator_calibration.json", playback_snapshot)
        columns = ["case_id", "wave", "verdict", "output", "frequency_error_pct",
                   "vpp_error_pct", "duty_error_pp", "dc_error_v", "capture"]
        table = io.StringIO(newline="")
        writer = csv.DictWriter(table, fieldnames=columns)
        writer.writeheader()
        for entry in results:
            writer.writerow({"case_id": entry["case_id"], "wave": entry["case"]["wave"],
                "capture": entry["capture"], **{k: entry["result"][k] for k in columns
                                                 if k in entry["result"]}})
        atomic_text(report / "results.csv", table.getvalue())
        atomic_json(report / "results.json", {"complete": completed, "results": results})
        summary = {"selected": len(selected), "complete": completed, "counts": dict(counts),
            "board": measured, "mode": "calibration-only" if calibration_only else "full" if measured else "encode-only",
            "frames_per_case": args.r_frames if measured else None,
            "all_passed": completed and not counts["failed"], "error": error_text,
            "shutdown_error": generator.shutdown_error,
            "failure": generator.failure, "failed_case": failed_case,
            "waveform_readback": generator.readback_info,
            "calibration": "calibration.json" if (report / "calibration.json").exists() else None,
            "generator_calibration": ("generator_calibration.json"
                if (report / "generator_calibration.json").exists() else None),
            "transport": generator.transport_label,
            "resource": generator.resource_label,
            "diagnostics": "scpi.jsonl",
            "not_run": [c.id for c in selected[len(results):]],
            "wall_time_s": time.perf_counter() - started,
            "note": ("Independent ADC/ARB calibration only; no matrix cases generated or measured."
                     if calibration_only else "Real instrument/UART test on identifiable weak/strong finals; adc mode scales "
                     "source volts to this board. Evaluation uses board metadata and, when enabled, "
                     "independent acquisition calibration; replay truth remains unchanged."
                     if measured else
                     "No board attached: every selected case was generated, encoded, uploaded and "
                     "readback-verified on the generator; R was not sent and nothing was measured.")}
        atomic_json(report / "summary.json", summary)
        print(f"{'RS' if calibration_only else 'RO'}: {dict(counts)}; summary: {report / 'summary.json'}", file=stderr, flush=True)
    return 0 if summary["all_passed"] else 1


def _run_cases(service, store, args, stderr, instrument, journal, report,
               selected, prepared, results, counts, vref, gain, measured):
    """Keep the RT/RO evaluation and optional UART behavior separate from transport."""
    atomic_json(report / "instrument.json", {"identity": instrument.identity,
        "host": args.ro_host, "port": args.ro_port, "channel": args.ro_channel,
        "transport": instrument.transport_label,
        "resource": instrument.resource_label,
        "vref_v": vref, "gain": gain, "level_mode": args.ro_level_mode,
        "board": measured, "mode": "full" if measured else "encode-only",
        "acquisition_calibration": (service.calibration.to_dict()
                                    if measured and service.calibration is not None else None),
        "algorithm_parameters_file": str(service.parameters_path) if measured else None,
        "acquisition_calibration_source": (service.calibration_profile.source
            if measured and service.calibration is not None else None),
        "generator_calibration": (asdict(instrument.playback_calibration)
            if instrument.playback_calibration is not None else None)})
    for index, (case, (adc, dac, config)) in enumerate(zip(selected, prepared), 1):
        transfer=getattr(instrument,'playback_transfer',None)
        if transfer is not None:
            from tools.amplitude_calibration import predistort_dac
            config=dict(config)
            config['uncompensated_dac_sha256']=hashlib.sha256(dac).hexdigest()
            dac=predistort_dac(dac,config,transfer)
            config['voltage_transfer_report']='arb_voltage_transfer.json'
        if instrument.playback_calibration is not None:
            config = instrument.playback_calibration.apply(config)
        source = report / case.id
        source.mkdir()
        (source / "rt.adc.bin").write_bytes(adc)
        (source / "generator.i16.bin").write_bytes(dac)
        atomic_json(source / "encoding.json", config)
        instrument.stage, instrument.command = "prepare waveform", case.id
        instrument.record("case_begin", case_id=case.id)
        print(f"RO {index}/{len(selected)} {case.id}: uploading and verifying waveform",
              file=stderr, flush=True)
        config["readback"] = instrument.configure(dac, config, readback_dir=source)
        atomic_json(source / "encoding.json", config)
        if not measured:
            entry = {"case_id": case.id, "case": asdict(case), "generator": config,
                "metadata": None, "algorithm": None, "capture": None,
                "result": {"verdict": "ENCODED", "passed": True,
                           "note": "generator encoding verified by readback; no board, R not sent"}}
            journal.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            results.append(entry)
            counts["encoded"] += 1
            print(f"RO {index}/{len(selected)} {case.id}: ENCODED", file=stderr, flush=True)
            continue
        if index == 1:
            print("开始测试", file=stderr, flush=True)
        instrument.stage, instrument.command = "R acquisition", "UART R"
        instrument.record("start", case_id=case.id)
        time.sleep(args.ro_settle)
        from tools.precision_batch import measure_batch
        line, details, elapsed, meta, record = measure_batch(
            service, frames=args.r_frames, timeout=args.ro_timeout, return_result=True)
        instrument.record("ok", case_id=case.id)
        instrument.stage, instrument.command = "evaluate R capture", case.id
        # Real R metadata governs measurement units; source truth stays fixed.
        measured_case = replace(case,
            fs_hz=meta.sample_rate_hz_exact or (meta.sample_rate_millihz / 1000),
            vref_v=meta.reference_microvolts / 1e6, gain=meta.gain_ppm / 1e6,
            vpp_v=config["truth"]["vpp_v"], dc_v=config["truth"]["dc_v"])
        check = evaluate_ro(measured_case, line, details, elapsed, config)
        for capture in record.info["r_batch"]["captures"]:
            capture_path = store.path / capture / "capture.json"
            info = (record.info if capture_path.parent == record.path else
                    json.loads(capture_path.read_text(encoding="utf-8")))
            info["context"].update(ro_case=asdict(case), generator=config,
                ro_report=report.relative_to(store.path).as_posix())
            info["self_test"] = check
            atomic_json(capture_path, info)
        entry = {"case_id": case.id, "case": asdict(case), "generator": config,
            "metadata": asdict(meta), "wire_metadata": record.info["metadata"],
            "result": check, "algorithm": details,
            "capture": record.path.relative_to(store.path).as_posix(),
            "captures": record.info["r_batch"]["captures"], "r_batch": record.info["r_batch"]}
        journal.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
        journal.flush()
        os.fsync(journal.fileno())
        results.append(entry)
        counts["completed"] += 1
        counts["passed" if check["passed"] else "failed"] += 1
        failure = "; ".join(check["failure_reasons"])
        if details["high_clip_samples"] or details["low_clip_samples"]:
            failure += f"; ADC rails high={details['high_clip_samples']}, low={details['low_clip_samples']}"
        print(f"RO {index}/{len(selected)} {case.id}: {check['verdict']}" +
              (f" ({failure})" if not check["passed"] else ""), file=stderr, flush=True)
        if not check["passed"] and getattr(args, "ro_stop_on_failure", False):
            raise RuntimeError(f"RO first failed verdict: {case.id}: {failure}")
