"""Offline boundary for real synchronous DAQ data; no hardware is fabricated.

python -m tools.vpp_reference_io calibrate independent-calibration.npz profile.json
python -m tools.vpp_reference_io measure synchronous-frame.npz profile.json result.json

See docs/VPP_REFERENCE_ZH.md for required acquisition and NPZ fields.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import numpy as np
from tools import host_waveform as host
from tools.vpp_reference import (CodeCalibration, ReferenceCalibration,
    SynchronousObservation, calibrate)

IDENTITIES = {"sample_rate_hz", "clock_id", "frontend_id", "main_channel", "reference_channel"}
CALIBRATION = IDENTITIES | {"main_dc_codes", "main_dc_volts", "reference_dc_codes",
    "reference_dc_volts", "dc_evidence", "noise_main_codes", "noise_reference_codes",
    "standard_main_codes", "standard_input_v", "source_off_evidence", "standard_evidence"}
MEASUREMENT = IDENTITIES | {"main_codes", "reference_codes", "main_start_tick",
    "reference_start_tick", "reference_is_source_free", "metadata_json"}


def read_npz(path, fields):
    with np.load(path, allow_pickle=False) as source:
        if set(source.files) != fields:
            raise ValueError(f"NPZ field mismatch: missing={sorted(fields-set(source.files))}, "
                f"unexpected={sorted(set(source.files)-fields)}")
        return {key: source[key].copy() for key in source.files}


def scalar(data, key):
    a = data[key]
    if a.ndim != 0:
        raise ValueError(f"{key} must be a scalar")
    return a.item()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("input", type=Path)
    c.add_argument("output", type=Path)
    c.add_argument("--validation-rms-limit-v", type=float, default=.002)
    m = sub.add_parser("measure")
    m.add_argument("input", type=Path)
    m.add_argument("calibration", type=Path)
    m.add_argument("output", type=Path)
    m.add_argument("--seconds", type=float, default=14.)
    a = p.parse_args()
    if a.action == "measure" and not 0 < a.seconds <= 14:
        p.error("--seconds must be (0, 14]")
    if a.action == "calibrate":
        d = read_npz(a.input, CALIBRATION)
        profile = calibrate(**{k: scalar(d, k) for k in IDENTITIES},
            main_adc=CodeCalibration(tuple(d["main_dc_codes"]), tuple(d["main_dc_volts"]), scalar(d, "dc_evidence")),
            reference_adc=CodeCalibration(tuple(d["reference_dc_codes"]), tuple(d["reference_dc_volts"]), scalar(d, "dc_evidence")),
            **{k: d[k] for k in ("noise_main_codes", "noise_reference_codes", "standard_main_codes", "standard_input_v")},
            source_off_evidence=scalar(d, "source_off_evidence"), standard_evidence=scalar(d, "standard_evidence"),
            validation_rms_limit_v=a.validation_rms_limit_v)
        write_json(a.output, profile.to_dict())
        print("Independent calibration saved:", a.output)
        return 0
    d = read_npz(a.input, MEASUREMENT)
    profile = ReferenceCalibration.from_dict(json.loads(a.calibration.read_text(encoding="utf-8")))
    o = SynchronousObservation(**{k: scalar(d, k) for k in IDENTITIES},
        reference_codes=d["reference_codes"],
        main_start_tick=scalar(d, "main_start_tick"), reference_start_tick=scalar(d, "reference_start_tick"),
        reference_is_source_free=scalar(d, "reference_is_source_free"))
    # A raw integer ADC vector is required; reject before encoding to avoid
    # uint16 wraparound, rounding invalid inputs, or silently hiding saturation.
    raw = d["main_codes"]
    profile.main_adc.convert(raw)
    payload = raw.astype("<u2").tobytes()
    meta = host.Metadata(**json.loads(scalar(d, "metadata_json")))
    if host.fnv1a(payload) != meta.checksum:
        raise ValueError("main payload checksum mismatch")
    from tools.unified_solver import SolverConfig
    line, result = host.analyze(None, payload, meta,
        config=SolverConfig(max_seconds=a.seconds), vpp_observation=o, vpp_calibration=profile)
    # Fail closed: no file with a normal result if the original classifier fails.
    if not result["ok"] or "vpp_reference" not in result:
        raise ValueError("original classification rejected this acquisition")
    write_json(a.output, {"output": line, "details": result,
        "metadata": asdict(meta), "input_sha256": hashlib.sha256(a.input.read_bytes()).hexdigest(),
        "accuracy_0p1_percent_certified": False})
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
