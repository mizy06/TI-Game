"""Deterministic board-era competition matrix and host-side signal generator.

Official amplitudes/DC/final limits are not published in the supplied manual.
Here their safe ADC envelopes and engineering tolerances are explicit test policy.
"""
from __future__ import annotations

import csv
import json
import io
import math
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import atomic_json, atomic_text

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
    expectation: str = "measure"  # measure / reject; every case is evaluated
    source: str = ""
    impairment: str = ""
    inl_lsb: float = 0.
    harmonic_2: float = 0.
    harmonic_3: float = 0.
    clip_top: float = 0.
    clip_bottom: float = 0.
    crossover: float = 0.
    am_depth: float = 0.
    am_rate_hz: float = 0.
    interferer_ratio: float = 0.
    interferer_frequency_ratio: float = 0.
    seed: int = 1


def safe_center(vpp, noise, position, vref=3.3, gain=1., headroom=.035):
    half = .5 * vpp * (1 + noise)
    low, high = half + headroom / gain, vref / gain - half - headroom / gain
    if high < low:
        raise ValueError("test envelope exceeds ADC rails")
    return low + position * (high - low)


TEST_FREQUENCY_MAX_EXCLUSIVE_HZ = 600.


def matrix(seed=20261005, random_count=512):
    """Return the final board-side 296-case matrix.

    Source IDs and payloads stay stable: 200 preliminary gate cases followed
    by 96 challenge cases, filtered to frequencies strictly below 600 Hz.
    ``random_count`` remains for command-line compatibility and is ignored.
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
        mode = {"white": "gaussian_white", "lowpass_5f": "gaussian_lowpass",
                "highband": "gaussian_highpass"}[old.noise_mode]
        case = Case(
            id=f"competition-{ordinal:06d}", suite="competition", group=group,
            wave=old.kind, frequency_hz=old.frequency_hz, vpp_v=vpp,
            dc_v=safe_center(vpp, old.noise_ratio, position, vref, gain),
            duty=old.duty, rise=old.rise_width, fall=old.fall_width,
            phase=old.phase, noise_pp_ratio=old.noise_ratio,
            bandwidth_ratio=5.25 if old.noise_mode == "lowpass_5f" else old.sample_rate_hz/(2*old.frequency_hz),
            noise_mode=mode, fs_hz=old.sample_rate_hz, vref_v=vref, gain=gain,
            source=old.source, impairment=old.impairment,
            inl_lsb=old.inl_lsb, harmonic_2=old.harmonic_2,
            harmonic_3=old.harmonic_3, clip_top=old.clip_top,
            clip_bottom=old.clip_bottom, crossover=old.crossover,
            am_depth=old.am_depth, am_rate_hz=old.am_rate_hz,
            interferer_ratio=old.interferer_ratio,
            interferer_frequency_ratio=old.interferer_frequency_ratio,
            expectation="measure", seed=(seed + ordinal * 104729) & 0xffffffff)
        # Distorted asymmetric shapes need their actual envelope, not a
        # symmetric half-Vpp approximation, when positioning near ADC rails.
        shape, _, _ = clean_signal(case)
        half_noise = .5*vpp*old.noise_ratio
        low = -.5*vpp*float(shape.min()) + half_noise + .035/gain
        high = vref/gain - .5*vpp*float(shape.max()) - half_noise - .035/gain
        if high < low:
            raise ValueError("test envelope exceeds ADC rails")
        return replace(case, dc_v=low+position*(high-low))

    old_cases = gate_cases(seed, 50) + challenge_cases(seed, 12)
    return (convert(old, ordinal, ordinal > 200)
            for ordinal, old in enumerate(old_cases, 1)
            if old.frequency_hz < TEST_FREQUENCY_MAX_EXCLUSIVE_HZ)


def ideal_shape(case, p):
    import numpy as np
    if case.wave == "sine":
        return np.sin(2*np.pi*p)
    if case.wave == "triangle":
        return 1.-4.*np.abs((p+.25)%1.-.5)
    if case.wave == "sawtooth":
        return 1.-2.*p
    if case.wave != "square":
        raise ValueError(case.wave)
    wave = np.where(p < case.duty, 1., -1.)
    rise_distance = (p+.5)%1.-.5
    fall_distance = (p-case.duty+.5)%1.-.5
    if case.rise:
        wave = np.where(np.abs(rise_distance) <= case.rise/2,
                        2.*rise_distance/case.rise, wave)
    if case.fall:
        wave = np.where(np.abs(fall_distance) <= case.fall/2,
                        -2.*fall_distance/case.fall, wave)
    return wave


def distorted_shape(case, p, t):
    import numpy as np
    wave = ideal_shape(case, p)
    if case.wave == "sine":
        wave += case.harmonic_2*np.sin(4*np.pi*p+.37)
        wave += case.harmonic_3*np.sin(6*np.pi*p-.23)
        if case.crossover:
            wave = np.sign(wave)*np.maximum(np.abs(wave)-case.crossover, 0)/(1-case.crossover)
    # Retained for callers of the old public Case API; normalize this too.
    if case.harmonic_ratio:
        wave += 2*case.harmonic_ratio*np.sin(6*np.pi*p)
    if case.clip_top:
        wave = np.minimum(wave, 1-case.clip_top)
    if case.clip_bottom:
        wave = np.maximum(wave, -1+case.clip_bottom)
    if case.am_depth:
        wave *= 1+case.am_depth*np.sin(2*np.pi*case.am_rate_hz*t+.41)
    if case.interferer_ratio:
        wave += case.interferer_ratio*np.sin(
            2*np.pi*case.frequency_hz*case.interferer_frequency_ratio*t+1.13)
    return wave


def clean_signal(case):
    """Separate source truth from noise, INL, quantization and the estimator.

    Periodic distortion is normalized over a dense complete cycle. AM and
    noninteger-frequency interference use the clean record's actual envelope.
    DC truth is exactly the configured source offset, the envelope midpoint.
    """
    import numpy as np
    t = np.arange(host.SAMPLES)/case.fs_hz
    p = (case.phase+t*case.frequency_hz)%1.
    wave = distorted_shape(case, p, t)
    impaired = any((case.harmonic_ratio, case.harmonic_2, case.harmonic_3,
                    case.clip_top, case.clip_bottom, case.crossover,
                    case.am_depth, case.interferer_ratio))
    nonperiodic = bool(case.am_depth or (case.interferer_ratio and
        abs(case.interferer_frequency_ratio-round(case.interferer_frequency_ratio)) > 1e-9))
    if not impaired:
        clean_mean = 2*case.duty-1 if case.wave == "square" else 0.
        return wave, clean_mean, "clean_period"
    if nonperiodic:
        reference = wave
    else:
        q = np.arange(65536)/65536.
        reference = distorted_shape(case, q, (q-case.phase)/case.frequency_hz)
    if impaired:
        low, high = float(reference.min()), float(reference.max())
        half = .5*(high-low)
        if half <= 0:
            raise ValueError("degenerate clean waveform")
        midpoint = .5*(low+high)
        wave = (wave-midpoint)/half
        reference = (reference-midpoint)/half
    mean = float(reference.mean())
    return wave, mean, "clean_record" if nonperiodic else "clean_period"


def generate(case):
    import numpy as np
    rng = np.random.default_rng(case.seed)
    n = np.arange(host.SAMPLES)
    t = n/case.fs_hz
    p = (case.phase+t*case.frequency_hz)%1.
    wave, _, truth_scope = clean_signal(case)
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
    if case.spikes:
        indices = rng.choice(host.SAMPLES, case.spikes, replace=False)
        volts[indices] += rng.choice([-1., 1.], case.spikes) * case.vpp_v * case.spike_ratio
    analog_codes = volts*case.gain*4095/case.vref_v
    if case.inl_lsb:
        analog_codes += case.inl_lsb*np.sin(2*np.pi*analog_codes/257.)
    codes = np.rint(analog_codes)
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
        "prequantization_clipped_samples": int(np.count_nonzero((codes < 0) | (codes > 4095))),
        "truth": {"frequency_hz": case.frequency_hz, "vpp_v": case.vpp_v,
                  "dc_v": case.dc_v,
                  "duty_pct": ((1-case.duty) if case.inverted else case.duty)*100
                              if case.wave == "square" else 0.,
                  "scope": truth_scope,
                  "vpp_definition": "clean signal peak-to-peak before noise/INL/quantization",
                  "dc_definition": "configured source offset (clean envelope midpoint)"}}


def evaluate(case, line, details, elapsed, facts=None):
    """Evaluate every case using internal precision, never rounded stdout."""
    truth = (facts or {}).get("truth", {"frequency_hz": case.frequency_hz,
        "vpp_v": case.vpp_v, "dc_v": case.dc_v,
        "duty_pct": case.duty*100 if case.wave == "square" else 0.})
    measured = {"frequency_hz": details.get("frequency_hz", details["frequency_millihz"]/1000),
        "vpp_v": details.get("vpp_v", details["vpp_microvolts"]/1e6),
        "duty_pct": details.get("duty_fraction", details["duty_ppm"]/1e6)*100 if case.wave == "square" else 0.,
        "dc_v": details.get("dc_offset_v", details["dc_offset_counts"]*case.vref_v/(4095*case.gain))}
    signed = {"frequency_bias_pct": (measured["frequency_hz"]/truth["frequency_hz"]-1)*100,
        "vpp_bias_pct": (measured["vpp_v"]/truth["vpp_v"]-1)*100,
        "duty_bias_pp": measured["duty_pct"]-truth["duty_pct"],
        "dc_bias_v": measured["dc_v"]-truth["dc_v"]}
    errors = {"frequency_error_pct": abs(signed["frequency_bias_pct"]),
        "vpp_error_pct": abs(signed["vpp_bias_pct"]),
        "duty_error_pp": abs(signed["duty_bias_pp"]),
        "dc_error_v": abs(signed["dc_bias_v"])}
    limits = {"frequency_pct": 1., "vpp_pct": 10. if case.group == "final_strong" else 8.,
              "duty_pp": 2.5, "dc_v": max(.02, case.vpp_v*.05),
              "algorithm_time_s": 15. if case.group == "final_strong" else 10.}
    class_correct = line.split(",")[0] == case.wave
    reasons = []
    if case.expectation == "reject":
        if details["ok"] or line != "unknown,0,0,0,0":
            reasons.append("expected_rejection")
    else:
        if not details["ok"] or details["status"] != 0:
            reasons.append("algorithm_status_"+str(details["status"]))
        if not class_correct:
            reasons.append("classification")
        for error, limit in (("frequency_error_pct", "frequency_pct"),
                ("vpp_error_pct", "vpp_pct"), ("duty_error_pp", "duty_pp"),
                ("dc_error_v", "dc_v")):
            if errors[error] > limits[limit]:
                reasons.append(error)
        if elapsed > limits["algorithm_time_s"]:
            reasons.append("algorithm_time_s")
    return {**errors, **signed, "passed": not reasons,
        "verdict": "FAIL" if reasons else "PASS", "failure_reasons": reasons,
        "limits": limits, "class_correct": class_correct, "output": line,
        "measured": measured, "truth": truth, "algorithm_elapsed_s": elapsed,
        "dc_sampling_uncertainty_v": (case.vpp_v*case.frequency_hz/(2*case.fs_hz)
                                     if case.wave == "sawtooth" else 0.)}


def finalize_tables(report_dir, entries, columns):
    """Replace both streamed tables with closed snapshots of every saved entry."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for entry in entries:
        check, details = entry["result"], entry["algorithm"]
        row = {"case_id": entry["case_id"], **entry["case"],
            **{k: check[k] for k in columns if k in check},
            "status": details.get("status", "ERROR"), "capture": entry["capture"],
            "failure_reasons": ";".join(check["failure_reasons"])}
        row.update({"measured_"+k: v for k,v in check.get("measured", {}).items()})
        row.update({"truth_"+k: v for k,v in check.get("truth", {}).items()
                    if "truth_"+k in columns})
        writer.writerow(row)
    atomic_text(report_dir/"results.csv", stream.getvalue())
    atomic_text(report_dir/"results.jsonl", "".join(
        json.dumps(entry, ensure_ascii=False, allow_nan=False)+"\n" for entry in entries))


def run(api, store, process, *, suite="all", seed=20261005,
        random_count=512, case_ids=(), max_cases=None, stop_event=None, stderr=None):
    import sys
    stderr = stderr or sys.stderr
    selected = [c for c in matrix(seed, random_count)
                if (suite == "all" or c.suite == suite) and
                (not case_ids or c.id in case_ids)]
    if case_ids and set(case_ids)-{c.id for c in selected}:
        raise ValueError("unknown case ID or case outside selected suite")
    if max_cases is not None:
        selected = selected[:max_cases]
    if not selected:
        raise ValueError("empty self-test selection")
    report_dir = store.path/("rt-"+str(time.time_ns()))
    report_dir.mkdir()
    atomic_json(report_dir/"matrix.json", {"schema": 2, "seed": seed,
        "cases": [asdict(c) for c in selected]})
    counters = Counter()
    groups, results, failures = {}, [], []
    started = time.perf_counter()
    interrupted = False
    columns = ["case_id", *Case.__dataclass_fields__, "passed", "verdict", "status",
        "output", "measured_frequency_hz", "measured_vpp_v", "measured_duty_pct",
        "measured_dc_v", "truth_vpp_v", "truth_dc_v", "frequency_error_pct",
        "vpp_error_pct", "duty_error_pp", "dc_error_v", "frequency_bias_pct",
        "vpp_bias_pct", "duty_bias_pp", "dc_bias_v", "dc_sampling_uncertainty_v",
        "failure_reasons", "algorithm_elapsed_s", "capture"]
    print(f"RT: {len(selected)} cases; reports: {report_dir}", file=stderr, flush=True)
    try:
        with (report_dir/"results.csv").open("w", newline="", encoding="utf-8") as stream, \
             (report_dir/"results.jsonl").open("w", encoding="utf-8") as journal:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            for case in selected:
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    break
                record = store.begin("rt", {"case": asdict(case)})
                facts, details, check = {}, {}, {}
                case_started = time.perf_counter()
                try:
                    payload, meta, facts = generate(case)
                    record.info["context"]["generated"] = facts
                    record.set_metadata(meta)
                    record.complete(payload)
                    line, details, elapsed = process(payload, meta, record)
                    check = evaluate(case, line, details, elapsed, facts)
                except (KeyboardInterrupt, OSError):
                    # An interrupt or failed durable write must not be masked.
                    raise
                except Exception as error:
                    record.info["case_error"] = f"{type(error).__name__}: {error}"
                    record.save()
                    elapsed = time.perf_counter()-case_started
                    check = {"passed": False, "verdict": "ERROR", "class_correct": False,
                        "failure_reasons": [record.info["case_error"]],
                        "algorithm_elapsed_s": elapsed, "output": record.info.get("output", "")}
                    counters["errors"] += 1
                record.info["self_test"] = check
                record.save()
                label = "passed" if check["passed"] else "failed"
                counters[label] += 1
                counters["completed"] += 1
                group = groups.setdefault(f"{case.suite}/{case.group}", Counter())
                group[label] += 1
                for key in ("frequency_error_pct", "vpp_error_pct", "duty_error_pp", "dc_error_v"):
                    if key in check:
                        group["worst_"+key] = max(group["worst_"+key], check[key])
                entry = {"case_id": case.id, "case": asdict(case), "generated": facts,
                    "metadata": record.info.get("metadata", {}), "algorithm": details,
                    "result": check, "capture": record.path.relative_to(store.path).as_posix(),
                    "capture_info": record.info.copy()}
                row = {"case_id": case.id, **asdict(case),
                    **{k: check[k] for k in columns if k in check},
                    "status": details.get("status", "ERROR"), "capture": entry["capture"],
                    "failure_reasons": ";".join(check["failure_reasons"])}
                row.update({"measured_"+k: v for k,v in check.get("measured", {}).items()})
                row.update({"truth_"+k: v for k,v in check.get("truth", {}).items()
                            if "truth_"+k in columns})
                writer.writerow(row)
                stream.flush(); os.fsync(stream.fileno())
                journal.write(json.dumps(entry, ensure_ascii=False, allow_nan=False)+"\n")
                journal.flush(); os.fsync(journal.fileno())
                results.append(entry)
                if not check["passed"]:
                    failures.append(entry)
                if counters["completed"] % 100 == 0:
                    print(f"RT: {counters['completed']}/{len(selected)}; failures={counters['failed']}",
                          file=stderr, flush=True)
    except BaseException:
        interrupted = True
        raise
    finally:
        complete = counters["completed"] == len(selected) and not interrupted
        # Finalize every completed frame from its authoritative in-memory
        # snapshot. Keep this snapshot in the durable aggregate/journal too;
        # recovery never needs to rerun the estimator or reconstruct a verdict.
        for entry in results:
            atomic_json(store.path/entry["capture"]/"capture.json", entry["capture_info"])
        finalize_tables(report_dir, results, columns)
        atomic_json(report_dir/"results.json", {"schema": 2, "seed": seed,
            "selected": len(selected), "complete": complete, "results": results})
        summary = {"schema": 2, "seed": seed, "suite": suite, "selected": len(selected),
            "complete": complete, "all_passed": complete and not counters["failed"],
            "counts": dict(counters), "groups": {k: dict(v) for k,v in groups.items()},
            "full_default_matrix": suite == "all" and not case_ids and max_cases is None,
            "not_run": [c.id for c in selected[len(results):]],
            "wall_time_s": time.perf_counter()-started, "failures": failures,
            "result_files": ["results.csv", "results.jsonl", "results.json"],
            "note": "All 296 cases are evaluated; signed errors use internal precision. "
                    "Limits are engineering policy. Raw ADC and complete diagnostics are retained."}
        atomic_json(report_dir/"summary.json", summary)
    if summary["complete"]:
        from tools.verify_rt_archive import verify
        try:
            verify(report_dir)
        except Exception as error:
            summary["all_passed"] = False
            summary["archive_verified"] = False
            summary["archive_error"] = f"{type(error).__name__}: {error}"
            atomic_json(report_dir/"summary.json", summary)
            raise
        summary["archive_verified"] = True
        atomic_json(report_dir/"summary.json", summary)
    print(f"RT done: {dict(counters)}; summary: {report_dir/'summary.json'}", file=stderr, flush=True)
    return 0 if summary["all_passed"] else 1
