"""Replay isolated historical host pipelines on verified, saved RO ADC frames.

Snapshots and snapshot_manifest.json must already exist under --output. Workers
receive acquisition metadata and ADC bytes only; truth stays in the parent.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
import re
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
REFS = ("e2fa070", "b4ea26d", "2e7dc18", "c253acb", "bacdc15")
_worker_ref = None


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def initialize_worker():
    for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS",
                 "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    # Keep dependency import/startup time outside the algorithm timer.
    import numpy
    import scipy.optimize
    from threadpoolctl import threadpool_limits
    global _thread_limit
    _thread_limit = threadpool_limits(limits=1)


def run_case(snapshot, ref, case_id, payload, metadata, seconds):
    global _worker_ref, _host, _config, _dll
    if _worker_ref != ref:
        for name in list(sys.modules):
            if name == "tools" or name.startswith("tools."):
                del sys.modules[name]
        sys.path.insert(0, snapshot)
        importlib.invalidate_caches()
        _host = importlib.import_module("tools.host_waveform")
        assert Path(_host.__file__).resolve() == Path(snapshot) / "tools/host_waveform.py"
        _dll, _config = None, None
        if ref == "e2fa070":
            _dll = _host.load_api(str(Path(snapshot) / "tools/waveform_analyzer_host.dll"))
            importlib.import_module("tools.envelope_measurement")
            importlib.import_module("tools.precision_measurement")
        else:
            solver = importlib.import_module("tools.unified_solver")
            assert Path(solver.__file__).resolve() == Path(snapshot) / "tools/unified_solver.py"
            server = (Path(snapshot) / "tools/waveform_server.py").read_text(encoding="utf-8")
            native_seconds = float(re.search(r'"--solver-seconds", type=float, default=([0-9.]+)', server)[1])
            _config = solver.SolverConfig(max_seconds=seconds or native_seconds)
        _worker_ref = ref
    meta = _host.Metadata(**metadata)
    started, cpu_started = time.perf_counter(), time.process_time()
    exception = None
    try:
        if _config is None:
            line, details = _host.analyze(_dll, payload, meta)
        else:
            line, details = _host.analyze(None, payload, meta, config=_config)
    except Exception as error:
        exception = traceback.format_exc()
        line = "unknown,0,0,0,0"
        details = dict(ok=False, status=8, type=4, frequency_millihz=0,
                       vpp_microvolts=0, duty_ppm=0, dc_offset_counts=0,
                       error=f"{type(error).__name__}: {error}")
    elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
    return dict(ref=ref, case_id=case_id, algorithm=details, output=line,
                elapsed_s=elapsed, cpu_s=cpu, worker_pid=os.getpid(),
                solver_max_seconds=getattr(_config, "max_seconds", None),
                host_path=str(_host.__file__), exception=exception)


def summarize(ref, results, expected):
    import numpy as np

    def distribution(values):
        if not values:
            return None
        return dict(count=len(values), mean=float(np.mean(values)),
                    p50=float(np.percentile(values, 50)),
                    p95=float(np.percentile(values, 95)),
                    p99=float(np.percentile(values, 99)), max=float(max(values)))

    valid = [r for r in results if r["algorithm"].get("ok")]
    errors = {key: distribution([r["result"][key] for r in valid
                                if key != "duty_error_pp" or r["case"]["wave"] == "square"])
              for key in ("frequency_error_pct", "vpp_error_pct", "duty_error_pp", "dc_error_v")}
    groups = {}
    for field in ("wave", "group"):
        groups[field] = {}
        for value in sorted({r["case"][field] for r in results}):
            rows = [r for r in results if r["case"][field] == value]
            groups[field][value] = dict(total=len(rows), passed=sum(r["result"]["passed"] for r in rows),
                classified=sum(r["result"]["class_correct"] for r in rows))
    return dict(ref=ref, complete=len(results) == expected, total=len(results),
        passed=sum(r["result"]["passed"] for r in results),
        failed=sum(not r["result"]["passed"] for r in results),
        classified=sum(r["result"]["class_correct"] for r in results),
        valid_outputs=len(valid), exceptions=sum(r["exception"] is not None for r in results),
        strict_source_passed=sum(r["result"]["passed"] and
            r["result"]["vpp_error_pct"] <= r["result"]["limits"]["vpp_pct"] for r in results),
        wire_vpp_rescued=[r["case_id"] for r in results if r["result"]["passed"] and
            r["result"]["vpp_error_pct"] > r["result"]["limits"]["vpp_pct"]],
        failures=dict(Counter(k for r in results for k in r["result"]["failure_reasons"])),
        failed_ids=[r["case_id"] for r in results if not r["result"]["passed"]],
        errors_on_valid_outputs=errors,
        wall_time_s=distribution([r["elapsed_s"] for r in results]),
        cpu_time_s=distribution([r["cpu_s"] for r in results]),
        worker_pids=sorted({r["worker_pid"] for r in results}), by=groups)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--frequency-below", type=float, default=600.)
    parser.add_argument("--seconds", type=float, help="Optional override; defaults to each revision's native budget")
    parser.add_argument("--smoke", action="store_true", help="Only the first eligible case per revision")
    parser.add_argument("--reuse", type=Path, help="Reuse completed non-timeout cases; retain provenance")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    initialize_worker()
    sys.path.insert(0, str(ROOT))
    from tools import host_waveform as current_host
    from tools.ro_test import evaluate_ro
    from tools.self_test import Case

    report, output = args.report.resolve(), args.output.resolve()
    manifest = json.loads((output / "snapshot_manifest.json").read_text(encoding="utf-8"))
    for ref in REFS:
        for name, sha in manifest["refs"][ref]["files"].items():
            if hashlib.sha256((output / "snapshots" / ref / name).read_bytes()).hexdigest() != sha:
                raise ValueError(f"Snapshot changed: {ref}/{name}")
    source = json.loads((report / "results.json").read_text(encoding="utf-8"))["results"]
    rows = [r for r in source if r["case"]["frequency_hz"] < args.frequency_below]
    if args.smoke:
        rows = rows[:1]
    selected = {r["case_id"]: r for r in rows}
    if len(selected) != len(rows):
        raise ValueError("Duplicate case IDs")
    raw = {}
    captures = []
    for row in rows:
        path = report.parent / row["capture"]
        payload = (path / "samples.adc.bin").read_bytes()
        info = json.loads((path / "capture.json").read_text(encoding="utf-8"))
        sha = hashlib.sha256(payload).hexdigest()
        meta = current_host.Metadata(**row["metadata"])
        if (info["validation"] != "valid" or len(payload) != meta.payload_bytes or
                current_host.fnv1a(payload) != meta.checksum or sha != info["analysis_sha256"]):
            raise ValueError(f"Invalid raw capture: {path}")
        raw[row["case_id"]] = payload
        captures.append(dict(case_id=row["case_id"], capture=row["capture"], sha256=sha,
                             bytes=len(payload), metadata=row["metadata"], case=row["case"]))
    dest = output / ("smoke" if args.smoke else "replay")
    dest.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if os.name == "nt":
        # Keep the active measurement host awake without keeping the display on.
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    run = dict(started_at=datetime.now(timezone.utc).isoformat(), report=str(report),
               source_results_sha256=hashlib.sha256((report / "results.json").read_bytes()).hexdigest(),
               frequency_below_hz=args.frequency_below, total_source_cases=len(source),
               selected_cases=len(rows), excluded_frequency_cases=sum(
                   r["case"]["frequency_hz"] >= args.frequency_below for r in source),
               workers=args.workers, blas_threads_per_worker=1,
               budget="native server defaults: 5/10/10/10 seconds" if not args.seconds else args.seconds,
               evaluator="current tools.ro_test.evaluate_ro; final_strong; 15 s wall limit",
               evaluator_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                   for name in ("tools/ro_test.py", "tools/self_test.py")},
               truth_excluded_from_worker_inputs=True, refs=list(REFS),
               equivalent_refs=manifest["equivalent_refs"], captures=captures)
    write_json(dest / "run_manifest.json", run)
    results = {ref: [] for ref in REFS}
    reused = {}
    if args.reuse:
        for ref in REFS:
            previous = args.reuse / ref / "results.jsonl"
            if not previous.exists():
                continue
            for line in previous.read_text(encoding="utf-8").splitlines():
                result = json.loads(line)
                if result["case_id"] not in selected:
                    continue
                if result["exception"] or result["elapsed_s"] > 15:
                    continue
                if ref in ("c253acb", "bacdc15") and result["solver_max_seconds"] != 10:
                    continue
                result["reused_from"] = str(previous.resolve())
                reused[(ref, result["case_id"])] = result
    journals = {}
    try:
        for ref in REFS:
            (dest / ref).mkdir(exist_ok=True)
            journals[ref] = (dest / ref / "results.jsonl").open("w", encoding="utf-8")
            for (saved_ref, _), result in reused.items():
                if saved_ref == ref:
                    results[ref].append(result)
                    journals[ref].write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
            journals[ref].flush()
        with ProcessPoolExecutor(max_workers=args.workers, initializer=initialize_worker) as pool:
            futures = [pool.submit(run_case, str(output / "snapshots" / ref), ref, row["case_id"],
                                   raw[row["case_id"]], row["metadata"], args.seconds)
                       for ref in REFS for row in rows if (ref, row["case_id"]) not in reused]
            for future in as_completed(futures):
                result = future.result()
                ref, case_id = result["ref"], result["case_id"]
                row = selected[case_id]
                meta = current_host.Metadata(**row["metadata"])
                case = replace(Case(**row["case"]),
                    fs_hz=meta.sample_rate_hz_exact or meta.sample_rate_millihz / 1000,
                    vref_v=meta.reference_microvolts / 1e6, gain=meta.gain_ppm / 1e6,
                    vpp_v=row["generator"]["truth"]["vpp_v"], dc_v=row["generator"]["truth"]["dc_v"])
                result.update(result=evaluate_ro(case, result["output"], result["algorithm"],
                                               result["elapsed_s"], row["generator"]),
                    case=row["case"], capture=row["capture"],
                    sha256=hashlib.sha256(raw[case_id]).hexdigest(), recorded_result=row["result"])
                results[ref].append(result)
                journals[ref].write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                journals[ref].flush()
                counts = {r: dict(completed=len(rs), passed=sum(x["result"]["passed"] for x in rs))
                          for r, rs in results.items()}
                write_json(dest / "progress.json", dict(elapsed_s=time.perf_counter() - started, versions=counts))
                print(ref, f"{len(results[ref])}/{len(rows)}", case_id, result["result"]["verdict"],
                      ";".join(result["result"]["failure_reasons"]), flush=True)
    finally:
        for stream in journals.values():
            stream.close()
    summaries = {}
    for ref, entries in results.items():
        entries.sort(key=lambda r: list(selected).index(r["case_id"]))
        (dest / ref / "results.jsonl").write_text("".join(
            json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in entries), encoding="utf-8")
        summaries[ref] = summarize(ref, entries, len(rows))
        write_json(dest / ref / "summary.json", summaries[ref])
    # Verify every historical source again after the run.
    for ref in REFS:
        for name, sha in manifest["refs"][ref]["files"].items():
            if hashlib.sha256((output / "snapshots" / ref / name).read_bytes()).hexdigest() != sha:
                raise ValueError(f"Snapshot changed during replay: {ref}/{name}")
    write_json(dest / "summary.json", dict(complete=True, elapsed_s=time.perf_counter() - started,
        selected_cases=len(rows), version_count=len(REFS), total_replays=sum(map(len, results.values())),
        source_and_snapshots_verified=True, versions=summaries))
    print("COMPLETE", dest / "summary.json", flush=True)
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__ == "__main__":
    main()
