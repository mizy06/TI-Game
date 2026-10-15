"""Verify that a complete RT report retains every verdict and ADC payload."""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.host_waveform import fnv1a


def verify(report):
    report = Path(report).resolve()
    summary = json.loads((report/"summary.json").read_text(encoding="utf-8"))
    bundle = json.loads((report/"results.json").read_text(encoding="utf-8"))
    rows = bundle["results"]
    journal = [json.loads(line) for line in (report/"results.jsonl").read_text(encoding="utf-8").splitlines()]
    with (report/"results.csv").open(newline="", encoding="utf-8") as stream:
        csv_rows = list(csv.DictReader(stream))
    cases = json.loads((report/"matrix.json").read_text(encoding="utf-8"))["cases"]
    if not summary["complete"] or not bundle["complete"]:
        raise ValueError("RT was not completed; consult not_run and the saved journal")
    if not len(rows) == len(csv_rows) == len(journal) == len(cases) == summary["selected"]:
        raise ValueError("result counts do not match the selected matrix")
    if summary["counts"].get("observed", 0):
        raise ValueError("unevaluated observe cases remain")
    if rows != journal:
        raise ValueError("JSON bundle and durable journal disagree")
    failed = []
    for case, row, flat in zip(cases, rows, csv_rows):
        if case["id"] != row["case_id"] or row["case_id"] != flat["case_id"]:
            raise ValueError("case order or identity mismatch")
        result = row["result"]
        if not isinstance(result["passed"], bool):
            raise ValueError("case lacks a boolean pass/fail verdict")
        if flat["verdict"] != result["verdict"] or flat["passed"] != str(result["passed"]):
            raise ValueError("CSV and JSON verdicts disagree")
        relative = Path(row["capture"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("capture path must stay within its session")
        directory = report.parent/relative
        capture = json.loads((directory/"capture.json").read_text(encoding="utf-8"))
        if "capture_info" in row and capture != row["capture_info"]:
            raise ValueError("per-frame file differs from its complete durable snapshot")
        if capture.get("self_test") != result:
            raise ValueError("per-frame and aggregate verdicts disagree")
        truth = capture.get("context", {}).get("generated", {}).get("truth", {})
        if truth.get("dc_definition") == "configured source offset (clean envelope midpoint)":
            if truth["dc_v"] != case["dc_v"]:
                raise ValueError("DC truth differs from the configured source offset")
            if "truth" in result and result["truth"]["dc_v"] != case["dc_v"]:
                raise ValueError("aggregate DC truth differs from the configured source offset")
            if flat.get("truth_dc_v") and float(flat["truth_dc_v"]) != case["dc_v"]:
                raise ValueError("CSV DC truth differs from the configured source offset")
        payload = (directory/"samples.adc.bin").read_bytes()
        if row["metadata"]:
            if len(payload) != 8192 or fnv1a(payload) != row["metadata"]["checksum"]:
                raise ValueError("ADC length or checksum mismatch: "+row["case_id"])
        if not result["passed"]:
            failed.append(row["case_id"])
    if len(failed) != summary["counts"].get("failed", 0):
        raise ValueError("failure total disagrees with per-case results")
    if failed != [row["case_id"] for row in summary["failures"]]:
        raise ValueError("failure list is incomplete")
    if summary["all_passed"] != (not failed):
        raise ValueError("aggregate pass/fail verdict is inconsistent")
    return {"records": len(rows), "passed": len(rows)-len(failed), "failed": len(failed),
            "payloads_and_checksums_verified": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.report_dir), ensure_ascii=False))


if __name__ == "__main__":
    main()
