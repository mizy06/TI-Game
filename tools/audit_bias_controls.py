"""Repeat all 96 distorted RT inputs with noise and INL disabled, saving all data."""
import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from tools.capture_store import CaptureStore, atomic_json
from tools.host_waveform import analyze
from tools.self_test import evaluate, generate, matrix
from tools.waveform_server import build_api


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT/"validation"/"bias_controls")
    parser.add_argument("--seed", type=int, default=20261005)
    args = parser.parse_args()
    api = build_api()
    store = CaptureStore(args.output)
    rows, groups = [], {}
    for original in matrix(args.seed):
        if original.group != "final_strong":
            continue
        case = replace(original, noise_pp_ratio=0., inl_lsb=0.)
        record = store.begin("controls", {"case": asdict(case),
            "control": "noise_and_inl_disabled", "original_case": asdict(original)})
        payload, metadata, facts = generate(case)
        record.info["context"]["generated"] = facts
        record.set_metadata(metadata)
        record.complete(payload)
        started = time.perf_counter()
        line, details = analyze(api, payload, metadata)
        elapsed = time.perf_counter()-started
        check = evaluate(case, line, details, elapsed, facts)
        record.result(line, details, elapsed)
        record.info["self_test"] = check
        record.save()
        rows.append({"case_id": case.id, "case": asdict(case), "result": check,
            "capture": str(record.path.relative_to(store.path)),
            "capture_info": record.info.copy()})
        groups.setdefault(case.impairment, []).append(check)
    summary = {"algorithm_version": "2.1.2", "control": "noise_and_inl_disabled",
        "dc_policy": "configured_source_offset", "count": len(rows),
        "passed": sum(row["result"]["passed"] for row in rows),
        "groups": {name: {"vpp_signed_mean_pct": sum(r["vpp_bias_pct"] for r in group)/len(group),
            "vpp_max_error_pct": max(r["vpp_error_pct"] for r in group),
            "frequency_max_error_pct": max(r["frequency_error_pct"] for r in group),
            "dc_max_error_v": max(r["dc_error_v"] for r in group)} for name, group in groups.items()}}
    for row in rows:
        atomic_json(store.path/row["capture"]/"capture.json", row["capture_info"])
    atomic_json(store.path/"results.json", {"complete": True, "results": rows})
    atomic_json(store.path/"summary.json", summary)
    with (store.path/"results.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["case_id", "impairment", "wave", "passed", "verdict", "output",
            "frequency_bias_pct", "vpp_bias_pct", "dc_bias_v", "frequency_error_pct",
            "vpp_error_pct", "dc_error_v"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: (row["case_id"] if name == "case_id" else
                row["case"][name] if name in ("impairment", "wave") else row["result"][name]) for name in fields})
    print(json.dumps({"path": str(store.path), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["passed"] == summary["count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
