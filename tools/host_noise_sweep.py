#!/usr/bin/env python3
"""在电脑上对 C 算法 DLL 做可重复的噪声扫描。

脚本按多个噪声等级生成四类波形，通过 ctypes 调用与固件同源的
``waveform_analyze``，统计分类正确率和频率/Vpp/占空比误差。固定随机种子
使回归结果可复现；它验证数字算法，不验证开发板模拟采集链路。
"""

import argparse
import ctypes
import json
import math
import random
from pathlib import Path

FS = 20_000.0  # 采样率 Hz；下划线只是 Python 的数字可读分隔符。
N = 4096       # 每帧采样点数，必须与 C 兼容入口一致。
ADC_MAX = 4095.0
VDDA = 3.3
ALGORITHM_VERSION = 0x00020000


class WaveformResult(ctypes.Structure):
    """C ``waveform_result_t`` 的逐字段二进制镜像。

    ctypes 按顺序解释 C 写入的内存，因此字段类型、顺序和数量必须与
    waveform_analyzer.h 完全一致；只改一边会让后续字段全部错位。
    """
    _fields_ = [
        ("type", ctypes.c_int),
        ("status", ctypes.c_int),
        ("frequency_millihz", ctypes.c_uint32),
        ("vpp_millivolts", ctypes.c_uint32),
        ("duty_permille", ctypes.c_uint16),
        ("confidence_permille", ctypes.c_uint16),
        ("best_score_permille", ctypes.c_uint16),
        ("second_score_permille", ctypes.c_uint16),
        ("accepted_cycles", ctypes.c_uint16),
        ("high_clip_samples", ctypes.c_uint16),
        ("dc_offset_counts", ctypes.c_uint16),
        ("low_clip_samples", ctypes.c_uint16),
        ("invalid_samples", ctypes.c_uint16),
        ("algorithm_version", ctypes.c_uint32),
        ("sequence", ctypes.c_uint32),
        ("vpp_microvolts", ctypes.c_uint32),
        ("duty_ppm", ctypes.c_uint32),
        ("period_q16_samples", ctypes.c_uint32),
        ("vpp_counts_q16", ctypes.c_uint32),
        ("quality_flags", ctypes.c_uint16),
        ("model_margin_permille", ctypes.c_uint16),
    ]


TYPE_NAMES = ["sine", "square", "triangle", "sawtooth", "unknown"]
STATUS_NAMES = [
    "OK",
    "SIGNAL_TOO_SMALL",
    "ADC_HIGH_CLIPPED",
    "PERIOD_NOT_FOUND",
    "FREQUENCY_OUT_OF_RANGE",
    "ADC_LOW_CLIPPED",
    "ADC_SAMPLE_INVALID",
    "CLASSIFICATION_AMBIGUOUS",
    "BAD_ARGUMENT",
    "BAD_METADATA",
    "INSUFFICIENT_SAMPLES",
]


def ideal_wave(kind, phase, duty):
    """返回相位 ``phase`` 处的归一化理想波形值（约 -1 到 +1）。"""
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if kind == "square":
        return 1.0 if phase < duty else -1.0
    if kind == "triangle":
        if phase < 0.25:
            return 4.0 * phase
        if phase < 0.75:
            return 2.0 - 4.0 * phase
        return 4.0 * phase - 4.0
    return 1.0 - 2.0 * phase  # descending sawtooth


def generate(kind, freq, vpp, duty, noise_ratio, rng):
    """生成一帧不会主动拒绝削顶的 12 位 ADC 整数样本。

    噪声先生成有限 4096 点高斯记录，再按该记录的实际峰峰值缩放到
    ``noise_ratio * signal_vpp``；这样 20% 的含义对每个用例都确定。
    ``rng`` 由调用者持有，保证整套扫描在固定 seed 下复现。
    """
    center = 1.65 / VDDA * ADC_MAX
    vpp_counts = vpp / VDDA * ADC_MAX
    # Scale one finite Gaussian record so its observed peak-to-peak equals
    # noise_ratio * signal Vpp. This is the competition-style white-noise case.
    noise = [rng.gauss(0.0, 1.0) for _ in range(N)]
    lo, hi = min(noise), max(noise)
    scale = (noise_ratio * vpp_counts) / (hi - lo) if hi > lo else 0.0
    noise = [(x - 0.5 * (lo + hi)) * scale for x in noise]
    # 随机起始相位防止算法依赖“第一点恰好在过零/峰值”的特殊情况。
    phase0 = rng.random()
    samples = []
    for i in range(N):
        phase = (phase0 + freq * i / FS) % 1.0
        value = center + 0.5 * vpp_counts * ideal_wave(kind, phase, duty) + noise[i]
        samples.append(max(0, min(4095, int(round(value)))))
    return samples


def analyze(dll, samples):
    """把 Python 整数列表转成 C 数组，调用 DLL 并解码枚举。

    返回 ``(valid, 原始结构体, 类别名, 状态名)``。版本不匹配立即报错，
    防止测试脚本用新版布局读取旧 DLL。
    """
    arr = (ctypes.c_uint16 * N)(*samples)
    result = WaveformResult()
    valid = bool(dll.waveform_analyze(arr, ctypes.byref(result)))
    if result.algorithm_version != ALGORITHM_VERSION:
        raise RuntimeError(
            f"DLL algorithm version {result.algorithm_version:#010x} does not "
            f"match required {ALGORITHM_VERSION:#010x}; rebuild the DLL"
        )
    kind = TYPE_NAMES[result.type] if 0 <= result.type < len(TYPE_NAMES) else "unknown"
    status = STATUS_NAMES[result.status] if 0 <= result.status < len(STATUS_NAMES) else "UNKNOWN"
    return valid, result, kind, status


def main():
    """运行全部噪声等级，打印摘要并保存 JSON 报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=25,
                        help="cases per waveform and noise level")
    parser.add_argument("--seed", type=int, default=20260917)
    parser.add_argument("--dll", default=str(Path(__file__).with_name("waveform_analyzer.dll")))
    parser.add_argument("--json", default="tests/results/host_noise_sweep.json")
    args = parser.parse_args()

    # 明确声明参数/返回类型非常重要；否则 ctypes 会按默认 int/指针规则
    # 猜测，64 位系统上可能截断地址。
    dll = ctypes.CDLL(args.dll)
    dll.waveform_analyze.argtypes = [ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(WaveformResult)]
    dll.waveform_analyze.restype = ctypes.c_bool
    rng = random.Random(args.seed)
    levels = [0.00, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50]
    kinds = ["sine", "square", "triangle", "sawtooth"]
    edge_freqs = [100.0, 101.0, 200.0, 399.0, 400.0]
    report = {"seed": args.seed, "cases_per_waveform": args.cases, "levels": []}

    for level in levels:
        rows = []
        failures = []
        for kind in kinds:
            for index in range(args.cases):
                freq = edge_freqs[index % len(edge_freqs)] if index < len(edge_freqs) else rng.uniform(100.0, 400.0)
                duty = rng.uniform(0.20, 0.80) if kind == "square" else 0.50
                # Keep the stress cases inside 0-3.3 V even at 50% noise.
                # 噪声越大，允许的信号 Vpp 越小，给 0~3.3 V 两端留余量。
                max_vpp = min(2.6, 3.0 / (1.0 + level) - 0.02)
                vpp = rng.uniform(0.80, max_vpp)
                samples = generate(kind, freq, vpp, duty, level, rng)
                valid, result, got_kind, status = analyze(dll, samples)
                measured_freq = result.frequency_millihz / 1000.0
                measured_vpp = result.vpp_millivolts / 1000.0
                freq_err = abs(measured_freq - freq) / freq * 100.0 if valid else 100.0
                vpp_err = abs(measured_vpp - vpp) / vpp * 100.0 if valid else 100.0
                duty_err = (abs(result.duty_permille / 10.0 - duty * 100.0)
                            if kind == "square" and valid else 0.0)
                # 方波 duty_err 用百分点；其他两项用相对百分比。
                passed = (valid and status == "OK" and got_kind == kind and
                          freq_err <= 2.0 and vpp_err <= 8.0 and duty_err <= 3.0)
                row = {
                    "expected": kind, "got": got_kind, "status": status,
                    "freq_hz": freq, "measured_freq_hz": measured_freq,
                    "vpp_v": vpp, "measured_vpp_v": measured_vpp,
                    "duty_pct": duty * 100.0 if kind == "square" else None,
                    "measured_duty_pct": result.duty_permille / 10.0 if kind == "square" and valid else None,
                    "freq_err_pct": freq_err, "vpp_err_pct": vpp_err,
                    "duty_err_pp": duty_err, "margin_pct": (result.best_score_permille - result.second_score_permille) / 10.0,
                    "cycles": result.accepted_cycles, "passed": bool(passed),
                }
                rows.append(row)
                if not passed and len(failures) < 8:
                    failures.append(row)
        # 摘要保留有限数量失败详情，既便于定位又避免 JSON/终端过大。
        valid_rows = [r for r in rows if r["status"] == "OK"]
        level_summary = {
            "noise_pct": level * 100.0,
            "total": len(rows),
            "passes": sum(r["passed"] for r in rows),
            "class_correct": sum(r["expected"] == r["got"] for r in rows),
            "status_ok": len(valid_rows),
            "max_freq_err_pct": max(r["freq_err_pct"] for r in rows),
            "max_vpp_err_pct": max(r["vpp_err_pct"] for r in rows),
            "max_duty_err_pp": max(r["duty_err_pp"] for r in rows),
            "min_margin_pct": min(r["margin_pct"] for r in rows),
            "failures": failures,
        }
        report["levels"].append(level_summary)
        print("noise={noise_pct:>4.0f}%  pass={passes:>3}/{total}  class={class_correct:>3}/{total}  "
              "max_err(freq/vpp/duty)={max_freq_err_pct:.3f}%/{max_vpp_err_pct:.3f}%/{max_duty_err_pp:.2f}pp  "
              "min_margin={min_margin_pct:.1f}%".format(**level_summary))

    output = Path(args.json)
    # 自动创建父目录；报告是生成物，可以覆盖同一路径的旧扫描结果。
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"saved={output}")


if __name__ == "__main__":
    main()
