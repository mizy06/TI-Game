#!/usr/bin/env python3
"""按 2026 TI 杯赛道一手册口径执行严格的 W 串口噪声门禁。

该工具验证的是板上固件从 UART 注入到级联算法输出的完整数字链路。
它不替代信号发生器 + PA25 的模拟 ADC 验收；每个事务保存输入哈希、
FNV 回执、级联追踪、RESULT、TIMING 和原始串口行，便于复核。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import time

SAMPLE_COUNT = 4096
FS_HZ = 20_000.0
ADC_MAX = 4095
VDDA = 3.3
DC_V = 1.65
SIGNAL_VPP = 2.0
FRAME_BUDGET_S = 10.0

# 手册只规定噪声上限、频率范围和处理时限；以下数值是工程内最严格的
# 初赛派生门禁，不宣称为主办方公布的评分阈值。
FREQ_LIMIT_PCT = 0.50
VPP_LIMIT_PCT = 8.0
DUTY_LIMIT_PP = 2.0

KINDS = ("sine", "square", "triangle", "sawtooth")
FREQUENCIES = (100.0, 250.0, 400.0)
NOISE_MODES = ("white", "lowpass_5f", "highband")
NOISE_LEVELS = (0.0, 0.20)


def fnv1a(payload: bytes) -> int:
    value = 2166136261
    for byte in payload:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def wave(kind: str, phase: float, duty: float, edge: float) -> float:
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if kind == "triangle":
        if phase < 0.25:
            return 4.0 * phase
        if phase < 0.75:
            return 2.0 - 4.0 * phase
        return 4.0 * phase - 4.0
    if kind == "sawtooth":
        return 1.0 - 2.0 * phase

    # 方波上升沿固定在周期边界附近，下降沿位于 duty 相位附近。
    rise_distance = phase if phase <= 0.5 else phase - 1.0
    fall_distance = phase - duty
    if fall_distance > 0.5:
        fall_distance -= 1.0
    if fall_distance < -0.5:
        fall_distance += 1.0
    if edge > 0.0 and abs(rise_distance) <= edge / 2.0:
        return 2.0 * rise_distance / edge
    if edge > 0.0 and abs(fall_distance) <= edge / 2.0:
        return -2.0 * fall_distance / edge
    return 1.0 if 0.5 * edge < phase < duty - 0.5 * edge else -1.0


def shaped_noise(mode: str, frequency: float, rng: random.Random) -> list[float]:
    raw = [rng.gauss(0.0, 1.0) for _ in range(SAMPLE_COUNT)]
    if mode == "white":
        shaped = raw
    else:
        # 一阶离散滤波器足以构造可复现的带宽边界压力；5.25f 留出
        # 数字滤波器滚降余量，仍严格满足“带宽上限 >= 5f”。
        cutoff = (5.25 if mode == "lowpass_5f" else 4.25) * frequency
        alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff / FS_HZ)
        state = 0.0
        low = []
        for value in raw:
            state += alpha * (value - state)
            low.append(state)
        shaped = low if mode == "lowpass_5f" else [a - b for a, b in zip(raw, low)]
    lo, hi = min(shaped), max(shaped)
    midpoint = 0.5 * (lo + hi)
    return [(value - midpoint) for value in shaped] if hi <= lo else [
        (value - midpoint) / (hi - lo) for value in shaped
    ]


def generate(case: dict, seed: int) -> bytes:
    rng = random.Random(seed)
    phase0 = rng.random()
    noise_unit = shaped_noise(case["noise_mode"], case["freq"], rng)
    noise_half_v = case["noise"] * SIGNAL_VPP
    samples = []
    for index, unit in enumerate(noise_unit):
        phase = (phase0 + index * case["freq"] / FS_HZ) % 1.0
        voltage = DC_V + 0.5 * SIGNAL_VPP * wave(
            case["type"], phase, case["duty"] / 100.0, case["edge"]
        ) + unit * noise_half_v
        if not 0.0 < voltage < VDDA:
            raise ValueError(f"generated sample out of ADC range: {voltage}")
        samples.append(round(voltage * ADC_MAX / VDDA))
    return struct.pack("<4096H", *samples)


def make_cases() -> list[dict]:
    cases = []
    for noise_mode in NOISE_MODES:
        for noise in NOISE_LEVELS:
            for frequency in FREQUENCIES:
                for kind in KINDS:
                    settings = ((20, 0.0), (50, 0.0), (80, 0.0),
                                (20, 0.10), (50, 0.10), (80, 0.10)) \
                        if kind == "square" else ((50, 0.0),)
                    for duty, edge in settings:
                        cases.append({
                            "type": kind, "freq": frequency, "duty": duty,
                            "edge": edge, "noise": noise,
                            "noise_mode": noise_mode,
                        })
    return cases


def parse_result(line: str) -> dict:
    parts = line.split(",")
    if parts.pop(0) != "RESULT":
        raise ValueError("expected RESULT")
    fields = {}
    for item in parts:
        key, value = item.split("=", 1)
        if key in fields:
            raise ValueError(f"duplicate RESULT field {key}")
        fields[key] = value
    if fields.get("status") != "OK":
        if fields.get("type") != "unknown":
            raise ValueError("failure RESULT has non-unknown type")
        return fields
    if fields.get("type") not in KINDS:
        raise ValueError("invalid RESULT type")
    for key in ("freq_hz", "vpp_v", "score_pct", "margin_pct", "cycles"):
        value = float(fields[key])
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"invalid RESULT {key}")
        fields[key] = value
    if fields["type"] == "square":
        duty = float(fields["duty_pct"])
        if not math.isfinite(duty) or not 0.0 < duty < 100.0:
            raise ValueError("invalid RESULT duty_pct")
        fields["duty_pct"] = duty
    elif fields.get("duty_pct") != "NA":
        raise ValueError("non-square RESULT duty must be NA")
    return fields


def read_line(port, deadline: float, log: list[dict]) -> str:
    while time.monotonic() < deadline:
        raw = port.readline()
        if raw:
            text = raw.decode("ascii", "replace").strip()
            log.append({"elapsed_s": time.monotonic() - (deadline - FRAME_BUDGET_S),
                        "raw_hex": raw.hex(), "text": text})
            return text
    raise TimeoutError("10 second frame deadline exceeded")


def transaction(port, payload: bytes) -> tuple[dict, float, list[dict]]:
    started = time.monotonic()
    deadline = started + FRAME_BUDGET_S
    log: list[dict] = []
    if port.write(b"W") != 1:
        raise IOError("short W write")
    port.flush()
    if read_line(port, deadline, log) != "READY_FOR_DATA":
        raise RuntimeError("handshake failed; payload was not sent")
    for offset in range(0, len(payload), 256):
        block = payload[offset:offset + 256]
        if port.write(block) != len(block):
            raise IOError("short payload write")
    port.flush()
    receipt = read_line(port, deadline, log)
    expected = f"DATA,bytes={len(payload)},fnv1a={fnv1a(payload)}"
    if receipt != expected:
        raise RuntimeError(f"data integrity failure: {receipt!r} != {expected!r}")
    if read_line(port, deadline, log) != "PROCESSING":
        raise RuntimeError("missing PROCESSING")

    # 当前级联固件会先输出 CASCADE 追踪；兼容并保存该证据，直到 RESULT。
    cascade = None
    while True:
        line = read_line(port, deadline, log)
        if line.startswith("CASCADE,"):
            cascade = line
            continue
        if line.startswith("RESULT,"):
            result = parse_result(line)
            break
        raise RuntimeError(f"unexpected analysis line: {line!r}")
    timing = read_line(port, deadline, log)
    if not timing.startswith("TIMING,"):
        raise RuntimeError(f"missing TIMING: {timing!r}")
    if read_line(port, deadline, log) != "READY":
        raise RuntimeError("missing terminal READY")
    elapsed = time.monotonic() - started
    result["cascade_trace"] = cascade
    result["timing"] = timing
    return result, elapsed, log


def evaluate(case: dict, result: dict, elapsed: float) -> tuple[bool, dict]:
    errors = {"freq_pct": None, "vpp_pct": None, "duty_pp": None}
    passed = result.get("status") == "OK" and result.get("type") == case["type"]
    if result.get("status") == "OK":
        errors["freq_pct"] = abs(result["freq_hz"] / case["freq"] - 1.0) * 100.0
        errors["vpp_pct"] = abs(result["vpp_v"] / SIGNAL_VPP - 1.0) * 100.0
        passed = passed and errors["freq_pct"] <= FREQ_LIMIT_PCT
        passed = passed and errors["vpp_pct"] <= VPP_LIMIT_PCT
        if case["type"] == "square":
            errors["duty_pp"] = abs(result.get("duty_pct", -999.0) - case["duty"])
            passed = passed and errors["duty_pp"] <= DUTY_LIMIT_PP
    passed = passed and elapsed <= FRAME_BUDGET_S
    return bool(passed), errors


def main() -> int:
    import serial
    import serial.tools.list_ports

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM15")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-cases", type=int, default=0,
                        help="只跑前 N 例；默认完整 162 例")
    args = parser.parse_args()

    matches = [p for p in serial.tools.list_ports.comports()
               if p.device.upper() == args.port.upper()]
    if len(matches) != 1 or "XDS110" not in (matches[0].description or "") \
            or "Application" not in (matches[0].description or ""):
        raise RuntimeError("指定端口不是唯一的 XDS110 Application/User UART")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    cases = make_cases()
    if args.max_cases:
        cases = cases[:args.max_cases]
    metadata = {
        "port": args.port, "hwid": matches[0].hwid,
        "mode": "UART W injection; NOT analog ADC validation",
        "baud": 115200, "fs_hz": FS_HZ, "sample_count": SAMPLE_COUNT,
        "seed_base": 20260925, "planned_cases": len(cases),
        "manual_noise_limit": "noise Vpp <= signal Vpp * 20%; bandwidth upper limit >= 5*f",
        "noise_models": list(NOISE_MODES), "noise_levels": list(NOISE_LEVELS),
        "engineering_gate_not_official": {
            "freq_pct": FREQ_LIMIT_PCT, "vpp_pct": VPP_LIMIT_PCT,
            "duty_pp": DUTY_LIMIT_PP, "frame_budget_s": FRAME_BUDGET_S,
        },
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    (output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = 0
    rows = []
    with serial.Serial(args.port, 115200, timeout=0.05, write_timeout=2,
                       rtscts=False, dsrdtr=False) as port:
        port.dtr = False
        port.rts = False
        while port.in_waiting:
            port.read(port.in_waiting)
        with (output / "results.jsonl").open("w", encoding="utf-8") as report:
            for index, case in enumerate(cases, 1):
                payload = generate(case, 20260925 + index)
                (output / f"input_{index:03d}.bin").write_bytes(payload)
                row = {"index": index, "expected": case,
                       "input_sha256": hashlib.sha256(payload).hexdigest()}
                try:
                    result, elapsed, log = transaction(port, payload)
                    passed, errors = evaluate(case, result, elapsed)
                    row.update(result=result, elapsed_s=elapsed,
                               errors=errors, passed=passed, log=log)
                    if not passed:
                        failures += 1
                    print(json.dumps({k: v for k, v in row.items() if k != "log"}, ensure_ascii=False), flush=True)
                except Exception as error:
                    row.update(error=f"{type(error).__name__}: {error}", passed=False)
                    failures += 1
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    report.write(json.dumps(row, ensure_ascii=False) + "\n")
                    report.flush()
                    return 2
                report.write(json.dumps(row, ensure_ascii=False) + "\n")
                report.flush()
                rows.append(row)

    summary = {
        "cases": len(rows), "passed": sum(int(row["passed"]) for row in rows),
        "failed": failures, "class_correct": sum(int(
            row.get("result", {}).get("type") == row["expected"]["type"]
        ) for row in rows),
        "max_freq_err_pct": max((row["errors"]["freq_pct"] if row["errors"]["freq_pct"] is not None else 100.0 for row in rows), default=None),
        "max_vpp_err_pct": max((row["errors"]["vpp_pct"] if row["errors"]["vpp_pct"] is not None else 100.0 for row in rows), default=None),
        "max_duty_err_pp": max((row["errors"]["duty_pp"] if row["errors"]["duty_pp"] is not None else 0.0 for row in rows), default=None),
        "max_elapsed_s": max((row.get("elapsed_s", 0.0) for row in rows), default=None),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
