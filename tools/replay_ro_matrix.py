#!/usr/bin/env python3
"""Replay a complete historical RO matrix using the current host algorithm."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path, PurePosixPath
import statistics
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def _analyze_frame(payload, metadata, config):
    from tools.host_waveform import Metadata, analyze

    started = time.perf_counter()
    line, details = analyze(None, payload, Metadata(**metadata), config=config)
    return {"line": line, "details": details, "elapsed_s": time.perf_counter() - started}


def _load_case_frames(row, data_root):
    from tools.host_waveform import Metadata, fnv1a

    captures = row.get("captures", [])
    if len(captures) != 3:
        raise ValueError(f"{row.get('case_id')}: expected three recorded frames")
    frames = []
    for reference in captures:
        relative = PurePosixPath(reference.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe capture reference: {reference}")
        directory = (data_root / Path(*relative.parts)).resolve()
        if not directory.is_relative_to(data_root.resolve()):
            raise ValueError(f"capture escapes historical session: {reference}")
        info = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
        if info.get("validation") != "valid" or info.get("analysis_payload_file") != "samples.adc.bin":
            raise ValueError(f"{reference}: original capture is not a validated ADC frame")
        payload = (directory / "samples.adc.bin").read_bytes()
        metadata = info.get("analysis_metadata")
        if not metadata:
            raise ValueError(f"{reference}: missing calibrated analysis metadata")
        meta = Metadata(**metadata)
        if (len(payload) != meta.payload_bytes or fnv1a(payload) != meta.checksum or
                hashlib.sha256(payload).hexdigest() != info.get("analysis_sha256")):
            raise ValueError(f"{reference}: ADC payload length/checksum/SHA mismatch")
        frames.append((payload, metadata, reference))
    return frames


def _numeric_leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("sequence", "algorithm_version"):
                continue
            yield from _numeric_leaves(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        yield prefix, float(value)


def _percentile(values, fraction):
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    weight = position - low
    return values[low] * (1.0 - weight) + values[high] * weight


def _distribution_rows(metrics):
    rows = []
    for scope, series in sorted(metrics.items()):
        for name, values in sorted(series.items()):
            ordered = sorted(values)
            rows.append({
                "scope": scope,
                "parameter": name,
                "count": len(ordered),
                "mean": statistics.fmean(ordered),
                "stddev": statistics.pstdev(ordered),
                "min": ordered[0],
                "p05": _percentile(ordered, .05),
                "median": _percentile(ordered, .50),
                "p95": _percentile(ordered, .95),
                "max": ordered[-1],
            })
    return rows


def _add_metrics(metrics, scopes, prefix, value):
    for name, number in _numeric_leaves(value):
        key = f"{prefix}.{name}" if prefix else name
        for scope in scopes:
            metrics.setdefault(scope, {}).setdefault(key, []).append(number)


def _run_shard(shard_index, rows, data_root, config, progress, progress_lock):
    from tools.host_waveform import NAMES
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"

    shard_rows = []
    shard_started = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=3, mp_context=mp.get_context("spawn")
    ) as pool:
        for row in rows:
            case_started = time.perf_counter()
            case_record = {"case_id": row["case_id"], "case": row["case"],
                           "recorded_pass": row.get("result", {}).get("passed"),
                           "captures": row.get("captures", [])}
            try:
                frames = _load_case_frames(row, data_root)
                analysis_started = time.perf_counter()
                futures = [pool.submit(_analyze_frame, payload, metadata, config)
                           for payload, metadata, _ in frames]
                frame_results = []
                for (payload, metadata, reference), future in zip(frames, futures):
                    try:
                        analyzed = future.result()
                        analyzed.update(capture=reference, ok=bool(analyzed["details"].get("ok")))
                    except Exception as error:
                        analyzed = {"capture": reference, "ok": False,
                                    "error": f"{type(error).__name__}: {error}"}
                    frame_results.append(analyzed)
                frame_analysis_elapsed = time.perf_counter() - analysis_started

                case_record["frames"] = frame_results
                frame_failures = [f"frame_{i + 1}_status_{frame.get('details', {}).get('status', 'ERROR')}"
                                  for i, frame in enumerate(frame_results) if not frame.get("ok")]
                if frame_failures:
                    reasons = []
                    for frame in frame_results:
                        details = frame.get("details", {})
                        reason = details.get("rejection_reason") or frame.get("error")
                        if reason:
                            reasons.append(reason)
                    case_record.update(passed=False, verdict="FAIL", failure_reasons=frame_failures,
                                       rejection_reasons=sorted(set(reasons)), measured=None,
                                       batch_error="one or more frames were rejected")
                else:
                    import numpy as np
                    from tools.precision_batch import _reconcile_joint_vpp
                    from tools.ro_test import evaluate_ro
                    from tools.self_test import Case
                    from tools.vpp_spectral import joint_frame_amplitude
                    from tools.host_waveform import Metadata

                    details_list = [frame["details"] for frame in frame_results]
                    kind_set = {details["type"] for details in details_list}
                    if len(kind_set) != 1:
                        case_record.update(passed=False, verdict="FAIL",
                            failure_reasons=["waveform classification changed between R blocks"],
                            measured=None, batch_error="waveform classification changed between R blocks")
                    else:
                        kind = details_list[0]["type"]
                        fields = ("frequency_hz", "vpp_v", "duty_fraction", "dc_offset_v")
                        values = np.asarray([[details[field] for field in fields]
                                             for details in details_list], dtype=float)
                        mean = values.mean(axis=0)
                        metas = [Metadata(**metadata) for _, metadata, _ in frames]
                        spectra = [details.get("spectral_vpp", {}) for details in details_list]
                        highly_consistent = all(
                            spectrum.get("accepted") and
                            float(spectrum.get("relative_rms", float("inf"))) <= 2e-4
                            for spectrum in spectra
                        )
                        use_joint_vpp = kind == 3 or not highly_consistent
                        batch_vpp = None
                        elapsed = frame_analysis_elapsed
                        if (use_joint_vpp and kind in (0, 1, 3) and
                                all(meta.vpp_transfer is not None for meta in metas)):
                            joint_frames = []
                            for (payload, _, _), meta, details in zip(frames, metas, details_list):
                                diagnostic = details.get("unified_solver", {})
                                joint_frames.append({
                                    "samples": np.frombuffer(payload, dtype="<u2"),
                                    "transfer": meta.vpp_transfer,
                                    "fs": meta.sample_rate_hz_exact or meta.sample_rate_millihz / 1000,
                                    "frequency": details["frequency_hz"],
                                    "duty": details["duty_fraction"],
                                    "rise": diagnostic.get("square_rise_fraction", 1e-5),
                                    "fall": diagnostic.get("square_fall_fraction", 1e-5),
                                    "vpp": details["vpp_v"],
                                })
                            fit_started = time.perf_counter()
                            try:
                                batch_vpp = joint_frame_amplitude(joint_frames, kind)
                            except (ValueError, np.linalg.LinAlgError) as error:
                                batch_vpp = {"accepted": False, "rejection": str(error)}
                            elapsed += time.perf_counter() - fit_started
                            if batch_vpp is not None:
                                selected, consensus = _reconcile_joint_vpp(
                                    values[:, 1], batch_vpp.get("vpp_v"),
                                    batch_vpp.get("relative_rms") if kind in (1, 3) else None)
                                batch_vpp.update(consensus, selected_vpp_v=selected)
                                if selected is not None:
                                    mean[1] = selected

                        if (np.ptp(values[:, 0]) / mean[0] > .002 or
                                np.ptp(values[:, 1]) / mean[1] > .10 or
                                (kind == 1 and np.ptp(values[:, 2]) > .01)):
                            case_record.update(passed=False, verdict="FAIL",
                                failure_reasons=["R blocks are not stationary"], measured=None,
                                batch_error="R blocks are not stationary")
                        else:
                            line = (f"{NAMES[kind]},{mean[0]:.9f},{mean[1]:.9f},"
                                    f"{mean[2] * 100:.7f},{mean[3]:.9f}")
                            aggregate = dict(details_list[-1])
                            aggregate.update(ok=True, status=0, type=kind,
                                frequency_hz=float(mean[0]), vpp_v=float(mean[1]),
                                duty_fraction=float(mean[2]), dc_offset_v=float(mean[3]))
                            case = Case(**row["case"])
                            truth = row.get("generator", {}).get("truth", {})
                            if truth:
                                case = replace(case, vpp_v=truth.get("vpp_v", case.vpp_v),
                                               dc_v=truth.get("dc_v", case.dc_v))
                            meta = metas[-1]
                            case = replace(case,
                                fs_hz=meta.sample_rate_hz_exact or meta.sample_rate_millihz / 1000,
                                vref_v=meta.reference_microvolts / 1e6,
                                gain=meta.gain_ppm / 1e6)
                            checked = evaluate_ro(case, line, aggregate, elapsed,
                                                 row.get("generator", {}))
                            case_record.update(result=checked, passed=checked["passed"],
                                verdict=checked["verdict"], failure_reasons=checked["failure_reasons"],
                                output=line, measured=checked["measured"], truth=checked["truth"],
                                elapsed_s=elapsed, block_standard_deviation=dict(zip(
                                    fields, map(float, np.std(values, axis=0, ddof=1)))),
                                batch_vpp=batch_vpp, algorithm=aggregate)
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                case_record.update(passed=False, verdict="ERROR",
                    failure_reasons=[message], error=message)
            case_record["wall_elapsed_s"] = time.perf_counter() - case_started
            shard_rows.append(case_record)
            with progress_lock:
                progress[0] += 1
                if progress[0] % 25 == 0 or progress[0] == 296:
                    print(f"offline replay: {progress[0]}/296 cases complete", file=sys.stderr, flush=True)
    return shard_index, shard_rows, time.perf_counter() - shard_started


def replay(report_path: Path, output_path: Path):
    report_path = report_path.resolve()
    data_root = report_path.parent.parent
    rows = []
    with report_path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    if len(rows) != 296 or len({row.get("case_id") for row in rows}) != 296:
        raise ValueError(f"expected 296 unique historical cases; found {len(rows)}")
    if any(len(row.get("captures", [])) != 3 for row in rows):
        raise ValueError("each historical case must contain exactly three recorded ADC frames")
    output_path = output_path.resolve()
    output_path.mkdir(parents=True, exist_ok=False)

    from tools.unified_solver import SolverConfig

    config = SolverConfig(frequency_min_hz=0., frequency_max_hz=0., starts=1, max_seconds=10.)
    progress, progress_lock = [0], threading.Lock()
    groups = [rows[index::4] for index in range(4)]
    completed = []
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as coordinator:
        futures = [coordinator.submit(_run_shard, index, shard, data_root, config,
                                      progress, progress_lock)
                   for index, shard in enumerate(groups)]
        timings = {}
        for future in concurrent.futures.as_completed(futures):
            shard_index, shard_rows, elapsed = future.result()
            timings[str(shard_index)] = elapsed
            completed.extend(shard_rows)
    completed.sort(key=lambda item: next(i for i, row in enumerate(rows)
                                         if row["case_id"] == item["case_id"]))

    metrics = {}
    outcomes = {"pass": 0, "fail": 0, "error": 0}
    failure_counts = {}
    for record in completed:
        case = record["case"]
        scopes = ["overall", f"wave:{case['wave']}", f"group:{case['group']}"]
        _add_metrics(metrics, scopes, "input", case)
        for index, frame in enumerate(record.get("frames", []), 1):
            _add_metrics(metrics, scopes, f"frame{index}", frame.get("details", {}))
        if record.get("result"):
            _add_metrics(metrics, scopes, "result", record["result"])
            _add_metrics(metrics, scopes, "measured", record.get("measured") or {})
            _add_metrics(metrics, scopes, "truth", record.get("truth") or {})
        if record.get("algorithm"):
            _add_metrics(metrics, scopes, "batch_algorithm", record["algorithm"])
        label = "pass" if record.get("passed") else "error" if record.get("verdict") == "ERROR" else "fail"
        outcomes[label] += 1
        for reason in record.get("failure_reasons", []):
            failure_counts[reason] = failure_counts.get(reason, 0) + 1

    distribution_rows = _distribution_rows(metrics)
    with (output_path / "cases.jsonl").open("w", encoding="utf-8") as stream:
        for record in completed:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    case_columns = [
        "case_id", "wave", "group", "verdict", "passed", "failure_reasons",
        "truth_frequency_hz", "truth_vpp_v", "truth_duty_fraction", "truth_dc_v",
        "noise_pp_ratio", "bandwidth_ratio", "frequency_hz", "frequency_bias_pct",
        "frequency_error_pct", "vpp_v", "vpp_bias_pct", "vpp_error_pct",
        "duty_fraction", "duty_bias_pp", "duty_error_pp", "dc_v", "dc_bias_v",
        "dc_error_v", "algorithm_elapsed_s", "block_frequency_sd_hz", "block_vpp_sd_v",
        "block_duty_sd_fraction", "block_dc_sd_v", "frame_statuses", "frame_rejections",
    ]
    with (output_path / "cases.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=case_columns)
        writer.writeheader()
        for record in completed:
            source, result = record["case"], record.get("result", {})
            measured, truth = record.get("measured") or {}, record.get("truth") or {}
            scatter = record.get("block_standard_deviation", {})
            writer.writerow({
                "case_id": record["case_id"], "wave": source["wave"], "group": source["group"],
                "verdict": record.get("verdict"), "passed": record.get("passed"),
                "failure_reasons": ";".join(record.get("failure_reasons", [])),
                "truth_frequency_hz": truth.get("frequency_hz", source.get("frequency_hz")),
                "truth_vpp_v": truth.get("vpp_v", source.get("vpp_v")),
                "truth_duty_fraction": source.get("duty") if source.get("wave") == "square" else 0.,
                "truth_dc_v": truth.get("dc_v", source.get("dc_v")),
                "noise_pp_ratio": source.get("noise_pp_ratio"), "bandwidth_ratio": source.get("bandwidth_ratio"),
                "frequency_hz": measured.get("frequency_hz"),
                "frequency_bias_pct": result.get("frequency_bias_pct"),
                "frequency_error_pct": result.get("frequency_error_pct"),
                "vpp_v": measured.get("vpp_v"), "vpp_bias_pct": result.get("vpp_bias_pct"),
                "vpp_error_pct": result.get("vpp_error_pct"),
                "duty_fraction": measured.get("duty_pct", 0.) / 100 if measured else None,
                "duty_bias_pp": result.get("duty_bias_pp"), "duty_error_pp": result.get("duty_error_pp"),
                "dc_v": measured.get("dc_v"), "dc_bias_v": result.get("dc_bias_v"),
                "dc_error_v": result.get("dc_error_v"),
                "algorithm_elapsed_s": record.get("elapsed_s"),
                "block_frequency_sd_hz": scatter.get("frequency_hz"),
                "block_vpp_sd_v": scatter.get("vpp_v"),
                "block_duty_sd_fraction": scatter.get("duty_fraction"),
                "block_dc_sd_v": scatter.get("dc_offset_v"),
                "frame_statuses": ";".join(str(frame.get("details", {}).get("status", "ERROR"))
                                             for frame in record.get("frames", [])),
                "frame_rejections": ";".join(str(frame.get("details", {}).get("rejection_reason", frame.get("error", "")))
                                               for frame in record.get("frames", [])),
            })
    with (output_path / "distributions.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["scope", "parameter", "count", "mean", "stddev",
            "min", "p05", "median", "p95", "max"])
        writer.writeheader()
        writer.writerows(distribution_rows)

    sources = ["tools/host_waveform.py", "tools/unified_solver.py", "tools/vpp_spectral.py",
               "tools/precision_batch.py", "tools/ro_test.py", "tools/self_test.py"]
    source_hashes = {name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() for name in sources}
    summary = {
        "source_results": str(report_path), "source_session": str(data_root),
        "output_dir": str(output_path), "cases": len(completed), "frames": sum(
            len(record.get("frames", [])) for record in completed),
        "parallelism": {"large_shards": 4, "frame_workers_per_shard": 3,
                        "case_assignment": "round-robin"},
        "solver_config": asdict(config), "counts": outcomes,
        "failure_reasons": dict(sorted(failure_counts.items())),
        "original_recorded_passes": sum(bool(row.get("result", {}).get("passed")) for row in rows),
        "wall_time_s": time.perf_counter() - started, "shard_wall_time_s": timings,
        "current_source_sha256": source_hashes,
        "distribution_scopes": sorted(metrics),
        "distribution_parameters_per_scope": {scope: len(series) for scope, series in metrics.items()},
        "artifacts": ["summary.json", "cases.csv", "cases.jsonl", "distributions.csv"],
    }
    (output_path / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-results", required=True, type=Path,
                        help="complete historical RO results.jsonl")
    parser.add_argument("--output", required=True, type=Path,
                        help="new directory for replay results and distributions")
    args = parser.parse_args()
    replay(args.source_results, args.output)


if __name__ == "__main__":
    main()
