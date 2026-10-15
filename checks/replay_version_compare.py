"""Replay one saved RO acquisition matrix through two isolated algorithm revisions.

Mode ``replay`` runs a single revision over the selected raw UART captures and
writes ``raw.jsonl`` (algorithm output only, no evaluation).  Mode ``compare``
evaluates two ``raw.jsonl`` files with the *current* RO evaluator so both
revisions are judged by one identical yardstick, then writes the comparison.

Revision A is the last revision pushed before Sunday 2026-10-11 (commit
5ecf88b, pushed 2026-10-09 20:33:58 +0800).  It predates DUMP protocol v3 and
its host pipeline accepts only 4096-sample frames, so it is replayed on the
legacy 4096-sample prefix of each capture and needs the acquisition
calibration applied explicitly.  Revision B is the current working tree.

Truth, generator settings and pass limits are never given to the replay worker.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
_worker = {}


def fnv1a(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def select_rows(report: Path, frequency_min: float, frequency_max: float,
                noise_below: float):
    rows = json.loads((report / "results.json").read_text(encoding="utf-8"))["results"]
    keep = [r for r in rows
            if frequency_min <= r["case"]["frequency_hz"] <= frequency_max
            and r["case"]["noise_pp_ratio"] < noise_below]
    return rows, keep


def load_capture(session: Path, row):
    cap = session / row["capture"]
    payload = (cap / "samples.adc.bin").read_bytes()
    info = json.loads((cap / "capture.json").read_text(encoding="utf-8"))
    meta = row["metadata"]
    if (info["validation"] != "valid" or len(payload) != meta["payload_bytes"] or
            fnv1a(payload) != meta["checksum"] or
            hashlib.sha256(payload).hexdigest() != info["analysis_sha256"]):
        raise ValueError(f"raw capture integrity failure: {cap}")
    return payload, hashlib.sha256(payload).hexdigest()


def initialize_worker(version: str, snapshot: str | None, seconds: float,
                      window: str = "head") -> None:
    for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS",
                 "BLIS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    _worker["window"] = window
    if version == "A":
        sys.path.insert(0, snapshot)
        import tools.host_waveform as host
        assert Path(host.__file__).resolve() == Path(snapshot) / "tools/host_waveform.py"
        from tools.acquisition_calibration import AcquisitionCalibration
        _worker["host"] = host
        _worker["calibration_class"] = AcquisitionCalibration
        _worker["dll"] = host.load_api(str(Path(snapshot) / "tools/waveform_analyzer_host.dll"))
    else:
        sys.path.insert(0, str(ROOT))
        import tools.host_waveform as host
        assert Path(host.__file__).resolve() == ROOT / "tools/host_waveform.py"
        from tools.unified_solver import SolverConfig
        _worker["host"] = host
        _worker["config"] = SolverConfig(max_seconds=seconds)
    _worker["version"] = version


def run_case(job):
    case_id, payload, row = job
    host = _worker["host"]
    started = time.perf_counter()
    exception = None
    try:
        if _worker["version"] == "A":
            wire = row["wire_metadata"]
            prefix = (payload[:host.PAYLOAD_BYTES] if _worker["window"] == "head"
                      else payload[-host.PAYLOAD_BYTES:])
            # Revision A predates the exact-rate field; keep only the fields its
            # calibration dataclass actually declares.
            meta = host.Metadata(sequence=wire["sequence"],
                                 sample_rate_millihz=wire["sample_rate_millihz"],
                                 reference_microvolts=wire["reference_microvolts"],
                                 gain_ppm=wire["gain_ppm"],
                                 adc_full_scale=wire["adc_full_scale"],
                                 flags=wire["flags"], checksum=fnv1a(prefix))
            calibrated, meta = _worker["calibration_class"](**_worker["calibration_kwargs"]).apply(
                prefix, meta)
            line, details = host.analyze(_worker["dll"], calibrated, meta)
            fed_samples = len(calibrated) // 2
        else:
            meta = host.Metadata(**row["metadata"])
            line, details = host.analyze(None, payload, meta, config=_worker["config"])
            fed_samples = len(payload) // 2
    except Exception as error:
        exception = traceback.format_exc()
        line, details, fed_samples = "unknown,0,0,0,0", dict(
            ok=False, status=8, error=f"{type(error).__name__}: {error}"), 0
    return dict(case_id=case_id, output=line, algorithm=details,
                elapsed_s=time.perf_counter() - started, fed_samples=fed_samples,
                worker_pid=os.getpid(), exception=exception)


def replay(args) -> None:
    report = args.report.resolve()
    session = report.parent
    rows, selected = select_rows(report, args.frequency_min, args.frequency_max,
                                 args.noise_below)
    if args.limit:
        selected = selected[:args.limit]
    calibration = json.loads((report / "calibration.json").read_text(encoding="utf-8"))["calibration"]
    jobs, captures = [], []
    for row in selected:
        payload, sha = load_capture(session, row)
        jobs.append((row["case_id"], payload, row))
        captures.append(dict(case_id=row["case_id"], capture=row["capture"], sha256=sha,
                             bytes=len(payload), case=row["case"]))
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = dict(version=args.version, snapshot=str(args.snapshot.resolve()) if args.snapshot else None,
                    report=str(report), seconds=args.seconds if args.version == "B" else None,
                    frequency_min_hz=args.frequency_min, frequency_max_hz=args.frequency_max,
                    noise_pp_ratio_below=args.noise_below,
                    matrix_cases=len(rows), selected_cases=len(selected),
                    excluded_cases=len(rows) - len(selected),
                    calibration=calibration,
                    frames=f"legacy 4096-sample {args.legacy_window} window "
                           f"(revision A accepts only 8192-byte payloads)"
                    if args.version == "A" else "full 8192-sample v3 frame",
                    captures=captures)
    write_json(out / "run_manifest.json", manifest)
    started = time.perf_counter()
    results = []
    with (out / "raw.jsonl").open("w", encoding="utf-8") as log:
        if args.version == "A":
            initialize_worker("A", str(args.snapshot.resolve()), args.seconds, args.legacy_window)
            fields = set(_worker["calibration_class"].__dataclass_fields__)
            _worker["calibration"] = calibration
            _worker["calibration_kwargs"] = {k: v for k, v in calibration.items()
                                             if k in fields}
            for job in jobs:
                result = run_case(job)
                results.append(result)
                log.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                if len(results) % 20 == 0:
                    print("A", len(results), "/", len(jobs), flush=True)
        else:
            with ProcessPoolExecutor(max_workers=args.workers,
                                     initializer=initialize_worker,
                                     initargs=("B", None, args.seconds, args.legacy_window)) as pool:
                futures = [pool.submit(run_case, job) for job in jobs]
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    log.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
                    log.flush()
                    print("B", len(results), "/", len(jobs), result["case_id"], flush=True)
    order = {r["case_id"]: i for i, r in enumerate(selected)}
    results.sort(key=lambda r: order[r["case_id"]])
    (out / "raw.jsonl").write_text("".join(
        json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in results), encoding="utf-8")
    summary = dict(version=args.version, complete=len(results) == len(selected),
                   selected_cases=len(selected), elapsed_s=time.perf_counter() - started,
                   exceptions=sum(r["exception"] is not None for r in results),
                   unknown_outputs=sum(r["output"].startswith("unknown") for r in results),
                   wall_time_s=dict(mean=sum(r["elapsed_s"] for r in results) / max(1, len(results)),
                                    max=max((r["elapsed_s"] for r in results), default=0.)))
    write_json(out / "replay_summary.json", summary)
    print(json.dumps(summary), flush=True)


def compare(args) -> None:
    sys.path.insert(0, str(ROOT))
    from tools.ro_test import evaluate_ro
    from tools.self_test import Case

    report = args.report.resolve()
    session = report.parent
    rows, selected = select_rows(report, args.frequency_min, args.frequency_max,
                                 args.noise_below)
    if args.limit:
        selected = selected[:args.limit]
    by_id = {r["case_id"]: r for r in selected}

    def load(path):
        target = path / "raw.jsonl" if path.is_dir() else path
        return {json.loads(line)["case_id"]: json.loads(line)
                for line in target.read_text(encoding="utf-8").splitlines() if line.strip()}

    runs = {name: load(Path(path).resolve()) for name, path in
            (item.split("=", 1) for item in args.runs)}
    missing = {name: sorted(set(by_id) - set(run)) for name, run in runs.items()}
    if any(missing.values()):
        raise ValueError(f"runs are missing selected cases: {missing}")

    entries, verdicts = [], {}
    for name, run in runs.items():
        verdicts[name] = Counter()
        for case_id, row in by_id.items():
            result = run[case_id]
            meta = row["metadata"]
            case = replace(Case(**row["case"]),
                           fs_hz=meta["sample_rate_hz_exact"] or meta["sample_rate_millihz"] / 1000,
                           vref_v=meta["reference_microvolts"] / 1e6,
                           gain=meta["gain_ppm"] / 1e6,
                           vpp_v=row["generator"]["truth"]["vpp_v"],
                           dc_v=row["generator"]["truth"]["dc_v"])
            check = evaluate_ro(case, result["output"], result["algorithm"],
                                result["elapsed_s"], row["generator"])
            verdicts[name][check["verdict"]] += 1
            entries.append(dict(version=name, case_id=case_id, case=row["case"],
                                recorded=row["result"], check=check,
                                elapsed_s=result["elapsed_s"],
                                exception=result["exception"],
                                algorithm=result["algorithm"], output=result["output"]))
    names = list(runs)
    index = {(e["version"], e["case_id"]): e for e in entries}

    def summarize(name):
        mine = [e for e in entries if e["version"] == name]
        valid = [e for e in mine if e["algorithm"].get("ok")]
        groups = {}
        for field in ("group", "wave"):
            groups[field] = {value: dict(total=len(sub),
                                         passed=sum(e["check"]["passed"] for e in sub))
                             for value in sorted({e["case"][field] for e in mine})
                             for sub in [[e for e in mine if e["case"][field] == value]]}
        return dict(version=name, total=len(mine),
                    passed=sum(e["check"]["passed"] for e in mine),
                    failed=sum(not e["check"]["passed"] for e in mine),
                    classified=sum(e["check"]["class_correct"] for e in mine),
                    valid_outputs=len(valid),
                    exceptions=sum(e["exception"] is not None for e in mine),
                    failures=dict(Counter(k for e in mine for k in e["check"]["failure_reasons"])),
                    failed_ids=[e["case_id"] for e in mine if not e["check"]["passed"]],
                    by_group=groups, by_wave=groups["wave"], groups=groups["group"],
                    wall_time_s=dict(mean=sum(e["elapsed_s"] for e in mine) / max(1, len(mine)),
                                     max=max((e["elapsed_s"] for e in mine), default=0.)))

    left, right = names[0], names[1]
    transitions = Counter()
    per_case = []
    for case_id, row in by_id.items():
        a, b = index[(left, case_id)], index[(right, case_id)]
        transitions[f"{a['check']['verdict']}->{b['check']['verdict']}"] += 1
        per_case.append(dict(
            case_id=case_id, group=row["case"]["group"], wave=row["case"]["wave"],
            frequency_hz=row["case"]["frequency_hz"], noise_pp_ratio=row["case"]["noise_pp_ratio"],
            recorded_verdict=row["result"]["verdict"],
            left_verdict=a["check"]["verdict"], right_verdict=b["check"]["verdict"],
            left_reasons=a["check"]["failure_reasons"], right_reasons=b["check"]["failure_reasons"],
            left_class=a["output"].split(",")[0], right_class=b["output"].split(",")[0],
            left_errors={k: a["check"][k] for k in ("frequency_error_pct", "vpp_error_pct",
                                                   "duty_error_pp", "dc_error_v")},
            right_errors={k: b["check"][k] for k in ("frequency_error_pct", "vpp_error_pct",
                                                    "duty_error_pp", "dc_error_v")},
            left_elapsed_s=a["elapsed_s"], right_elapsed_s=b["elapsed_s"],
            verdicts={name: dict(verdict=index[(name, case_id)]["check"]["verdict"],
                                 passed=index[(name, case_id)]["check"]["passed"],
                                 reasons=index[(name, case_id)]["check"]["failure_reasons"],
                                 classification=index[(name, case_id)]["output"].split(",")[0],
                                 elapsed_s=index[(name, case_id)]["elapsed_s"],
                                 errors={k: index[(name, case_id)]["check"][k] for k in
                                         ("frequency_error_pct", "vpp_error_pct",
                                          "duty_error_pp", "dc_error_v")})
                         for name in names}))
    summary = dict(report=str(report), left=left, right=right,
                   frequency_min_hz=args.frequency_min, frequency_max_hz=args.frequency_max,
                   noise_pp_ratio_below=args.noise_below,
                   selected_cases=len(selected), transitions=dict(transitions),
                   versions={name: summarize(name) for name in names},
                   per_case=per_case)
    write_json(args.output.resolve() / "comparison.json", summary)
    print(json.dumps({k: summary[k] for k in ("selected_cases", "transitions", "versions")},
                     ensure_ascii=False, indent=1), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("replay", "compare"), required=True)
    parser.add_argument("--report", type=Path, required=True, help="RO report directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", choices=("A", "B"))
    parser.add_argument("--snapshot", type=Path, help="revision A snapshot directory")
    parser.add_argument("--runs", nargs="+", help="NAME=DIR pairs for compare mode")
    parser.add_argument("--frequency-min", type=float, default=200.)
    parser.add_argument("--frequency-max", type=float, default=600.)
    parser.add_argument("--noise-below", type=float, default=.5)
    parser.add_argument("--seconds", type=float, default=10., help="revision B solver budget")
    parser.add_argument("--legacy-window", choices=("head", "tail"), default="head",
                        help="which 4096-sample half revision A is replayed on")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.mode == "replay":
        if args.version == "A" and not args.snapshot:
            parser.error("--snapshot is required for version A")
        replay(args)
    else:
        if not args.runs:
            parser.error("--runs is required for compare mode")
        compare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
