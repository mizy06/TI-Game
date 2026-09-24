#!/usr/bin/env python3
"""由赛事条件派生的确定性波形算法压力测试。

脚本生成两组数据：``preliminary_gate`` 严格位于本届初赛约束内，全部通过
才允许脚本返回成功；``competition_challenge`` 借用其他电子设计竞赛题中的
削顶、交越失真、谐波、调幅和相干干扰，只用于提前暴露薄弱点，默认不阻断
初赛门禁。所有随机数都有固定种子，失败可以逐样本复现。

流程是 Python 生成 ADC 样本与真实参数 -> ctypes 构造 C 输入帧 -> 调用
``waveform_analyze_frame`` -> 按门限判定 -> 写出完整 JSON。它测试数字算法，
不等价于真实硬件噪声模型、模拟前端或校准测试。
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import random
import sys
from typing import Iterable


SAMPLE_COUNT = 4096      # 每帧点数，与 C 静态工作区和默认工程一致。
ADC_FULL_SCALE = 4095    # 12 位无符号 ADC 的最大码值 2^12-1。
ALGORITHM_VERSION = 0x00020100
ALGORITHM_NAME = {
    1: "legacy_amdf_crossing",
    2: "global_harmonic_model",
    3: "fast_phase_model",
}

# C 枚举的双向可读映射；列表顺序必须与 waveform_analyzer.h 一致。
TYPE_ID = {"sine": 0, "square": 1, "triangle": 2, "sawtooth": 3}
TYPE_NAME = ["sine", "square", "triangle", "sawtooth", "unknown"]
STATUS_NAME = [
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

# 与头文件中的帧标志位相同。1<<n 表示只把第 n 位设为 1。
FRAME_CONTIGUOUS = 1 << 0
FRAME_CLOCK_CALIBRATED = 1 << 1
FRAME_VREF_CALIBRATED = 1 << 2


class WaveformFrame(ctypes.Structure):
    """C ``waveform_frame_t`` 的二进制镜像，字段顺序/类型不可单边修改。"""

    _fields_ = [
        ("samples", ctypes.POINTER(ctypes.c_uint16)),
        ("sample_count", ctypes.c_uint16),
        ("adc_full_scale", ctypes.c_uint16),
        ("sample_rate_millihz", ctypes.c_uint32),
        ("adc_reference_microvolts", ctypes.c_uint32),
        ("frontend_gain_ppm", ctypes.c_uint32),
        ("sequence", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
    ]


class WaveformResult(ctypes.Structure):
    """C ``waveform_result_t`` 的二进制镜像，供 ctypes 解码算法输出。"""

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


@dataclass(frozen=True)
class Case:
    """一个完整且不可变的合成测试条件。

    前半字段描述来源、理想波形和采集参数；带默认值的后半字段描述可选
    失真。``frozen=True`` 可防止用例生成后被分析循环意外修改，便于审计。
    比例字段通常以 1.0 表示 100%，电压幅度在这里使用 ADC 码域 Vpp。
    """

    group: str
    source: str
    impairment: str
    kind: str
    frequency_hz: float
    vpp_counts: float
    duty: float
    rise_width: float
    fall_width: float
    noise_ratio: float
    noise_mode: str
    sample_rate_hz: float
    phase: float
    center_mode: str
    reference_uv: int
    gain_ppm: int
    inl_lsb: float = 0.0
    harmonic_2: float = 0.0
    harmonic_3: float = 0.0
    clip_top: float = 0.0
    clip_bottom: float = 0.0
    crossover: float = 0.0
    am_depth: float = 0.0
    am_rate_hz: float = 0.0
    interferer_ratio: float = 0.0
    interferer_frequency_ratio: float = 0.0


def cyclic_distance(phase: float, center: float) -> float:
    """返回单位圆上 ``phase`` 到 ``center`` 的最短有符号距离 [-0.5,0.5]。"""
    distance = phase - center
    if distance > 0.5:
        distance -= 1.0
    elif distance < -0.5:
        distance += 1.0
    return distance


def ideal_wave(case: Case, phase: float) -> float:
    """计算用例在归一化相位 [0,1) 上的理想形状，范围约 [-1,+1]。

    方波可带独立上升/下降斜坡；相位 0 处的上升沿用 cyclic_distance 处理，
    因此跨越 1->0 的周期边界仍然连续。
    """
    if case.kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    if case.kind == "triangle":
        if phase < 0.25:
            return 4.0 * phase
        if phase < 0.75:
            return 2.0 - 4.0 * phase
        return 4.0 * phase - 4.0
    if case.kind == "sawtooth":
        return 1.0 - 2.0 * phase

    rise_distance = cyclic_distance(phase, 0.0)
    fall_distance = cyclic_distance(phase, case.duty)
    if case.rise_width > 0.0 and abs(rise_distance) <= 0.5 * case.rise_width:
        return 2.0 * rise_distance / case.rise_width
    if case.fall_width > 0.0 and abs(fall_distance) <= 0.5 * case.fall_width:
        return -2.0 * fall_distance / case.fall_width
    if 0.5 * case.rise_width < phase < case.duty - 0.5 * case.fall_width:
        return 1.0
    return -1.0


def distorted_wave(case: Case, phase: float, time_s: float) -> float:
    """在理想形状上依次叠加用例指定的确定性非理想因素。

    二/三次谐波和交越失真只施加到正弦；上下削顶、慢调幅及额外相干正弦
    可用于更多类别。这里的削顶是“源波形形状失真”，与最终 ADC 0/4095
    钳位不是一回事。
    """
    value = ideal_wave(case, phase)
    if case.kind == "sine":
        value += case.harmonic_2 * math.sin(4.0 * math.pi * phase + 0.37)
        value += case.harmonic_3 * math.sin(6.0 * math.pi * phase - 0.23)
        if case.crossover > 0.0:
            magnitude = abs(value)
            if magnitude <= case.crossover:
                value = 0.0
            else:
                value = math.copysign(
                    (magnitude - case.crossover) / (1.0 - case.crossover), value
                )
    if case.clip_top > 0.0:
        value = min(value, 1.0 - case.clip_top)
    if case.clip_bottom > 0.0:
        value = max(value, -1.0 + case.clip_bottom)
    # 调幅让包络随时间缓慢变化；interferer 是另一个与主信号相干的正弦。
    if case.am_depth > 0.0:
        value *= 1.0 + case.am_depth * math.sin(
            2.0 * math.pi * case.am_rate_hz * time_s + 0.41
        )
    if case.interferer_ratio > 0.0:
        value += case.interferer_ratio * math.sin(
            2.0 * math.pi * case.frequency_hz
            * case.interferer_frequency_ratio * time_s + 1.13
        )
    return value


def gaussian_noise(case: Case, rng: random.Random) -> list[float]:
    """生成指定频谱形态和峰峰比例的 4096 点高斯噪声。

    ``white`` 不整形；``lowpass_5f`` 用一阶低通保留低频扰动；``highband``
    用“原始值减低通值”得到高频成分。最后按有限记录实际 min/max 缩放，
    使 noise_ratio 的含义在每个用例中明确而可复现。
    """
    raw = [rng.gauss(0.0, 1.0) for _ in range(SAMPLE_COUNT)]
    if case.noise_mode == "lowpass_5f":
        cutoff = min(0.42 * case.sample_rate_hz, 5.25 * case.frequency_hz)
        alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff / case.sample_rate_hz)
        state = 0.0
        shaped = []
        # state += alpha*(input-state) 是一阶低通；alpha 越大，跟随越快。
        for value in raw:
            state += alpha * (value - state)
            shaped.append(state)
        raw = shaped
    elif case.noise_mode == "highband":
        cutoff = min(0.30 * case.sample_rate_hz, 4.25 * case.frequency_hz)
        alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff / case.sample_rate_hz)
        state = 0.0
        shaped = []
        # 高通近似 = 原始白噪声 - 它的低频部分。
        for value in raw:
            state += alpha * (value - state)
            shaped.append(value - state)
        raw = shaped
    elif case.noise_mode != "white":
        raise ValueError(f"unknown noise mode: {case.noise_mode}")

    minimum = min(raw)
    maximum = max(raw)
    midpoint = 0.5 * (minimum + maximum)
    scale = (case.noise_ratio * case.vpp_counts / (maximum - minimum)
             if maximum > minimum else 0.0)
    return [(value - midpoint) * scale for value in raw]


def choose_center(case: Case, rng: random.Random) -> float:
    """在不让“信号+噪声”碰到 ADC 两端的前提下选择直流中心码。

    low/high/middle 用于固定覆盖三种偏置边界；其他值走随机中心。两端额外
    留 18 码保护，避免本应测试分类的用例意外变成 ADC 削顶测试。
    """
    occupied_half_span = 0.5 * case.vpp_counts * (1.0 + case.noise_ratio)
    low = occupied_half_span + 18.0
    high = ADC_FULL_SCALE - occupied_half_span - 18.0
    if high <= low:
        raise ValueError("case cannot fit into ADC range")
    if case.center_mode == "low":
        return low
    if case.center_mode == "high":
        return high
    if case.center_mode == "middle":
        return 0.5 * ADC_FULL_SCALE
    return rng.uniform(low, high)


def generate_samples(case: Case, seed: int) -> list[int]:
    """把一个 Case 变成 4096 个 0~4095 整数 ADC 码。

    确定性失真可能改变波形峰峰值，所以先按本帧实际最大/最小重新归一化
    到 [-1,+1]，确保 ``vpp_counts`` 仍代表请求的源信号 Vpp；再加入噪声、
    直流中心和可选 ADC INL 正弦误差，最后四舍五入/钳位。
    """
    rng = random.Random(seed)
    shape = []
    for index in range(SAMPLE_COUNT):
        time_s = index / case.sample_rate_hz
        phase = (case.phase + case.frequency_hz * time_s) % 1.0
        shape.append(distorted_wave(case, phase, time_s))

    # 即使故意加入削顶/谐波，也保持请求的“源波形 Vpp”作为可比较真值。
    minimum = min(shape)
    maximum = max(shape)
    midpoint = 0.5 * (minimum + maximum)
    half_span = 0.5 * (maximum - minimum)
    if half_span <= 1.0e-12:
        raise ValueError("degenerate generated waveform")
    shape = [(value - midpoint) / half_span for value in shape]

    noise = gaussian_noise(case, rng)
    center = choose_center(case, rng)
    samples = []
    for signal, perturbation in zip(shape, noise):
        value = center + 0.5 * case.vpp_counts * signal + perturbation
        # INL（积分非线性）用随码值缓慢变化的正弦误差近似，单位 LSB。
        if case.inl_lsb > 0.0:
            value += case.inl_lsb * math.sin(2.0 * math.pi * value / 257.0)
        samples.append(max(0, min(ADC_FULL_SCALE, int(round(value)))))
    return samples


def make_case(group: str, source: str, impairment: str, kind: str,
              frequency_hz: float, vpp_counts: float, duty: float,
              rise_width: float, fall_width: float, noise_ratio: float,
              noise_mode: str, sample_rate_hz: float, phase: float,
              center_mode: str, reference_uv: int, gain_ppm: int,
              **kwargs: float) -> Case:
    """集中构造 Case，让两组用例生成器共享同一字段顺序和默认失真项。"""
    return Case(
        group=group,
        source=source,
        impairment=impairment,
        kind=kind,
        frequency_hz=frequency_hz,
        vpp_counts=vpp_counts,
        duty=duty,
        rise_width=rise_width,
        fall_width=fall_width,
        noise_ratio=noise_ratio,
        noise_mode=noise_mode,
        sample_rate_hz=sample_rate_hz,
        phase=phase,
        center_mode=center_mode,
        reference_uv=reference_uv,
        gain_ppm=gain_ppm,
        **kwargs,
    )


def gate_cases(seed: int, cases_per_kind: int) -> list[Case]:
    """生成本届初赛范围内的发布门禁用例。

    频率、Vpp、占空比、噪声频谱、直流中心、采样率、参考电压、前端增益
    和 INL 采用边界网格与固定种子随机值混合；每种波形生成相同数量。
    """
    rng = random.Random(seed)
    cases: list[Case] = []
    frequencies = [100.0, 100.137, 137.173, 199.951, 250.25,
                   333.333, 399.863, 400.0]
    vpps = [96.0, 160.0, 640.0, 1800.0, 3000.0]
    duties = [0.20, 0.21, 0.37, 0.50, 0.63, 0.79, 0.80]
    noise_modes = ["white", "lowpass_5f", "highband"]
    center_modes = ["low", "high", "middle", "random"]
    sample_rates = [19760.0, 19997.0, 20000.0, 20240.0]

    for kind in TYPE_ID:
        for index in range(cases_per_kind):
            # 前两轮完整覆盖预设频率，后续再从连续区间随机抽样。
            frequency = (frequencies[index % len(frequencies)]
                         if index < 2 * len(frequencies)
                         else rng.uniform(100.0, 400.0))
            vpp = vpps[index % len(vpps)]
            duty = duties[index % len(duties)] if kind == "square" else 0.5
            rise = (index % 11) / 100.0 if kind == "square" else 0.0
            fall = ((index * 7) % 11) / 100.0 if kind == "square" else 0.0
            noise_ratio = [0.0, 0.05, 0.10, 0.15, 0.20][index % 5]
            cases.append(make_case(
                group="preliminary_gate",
                source="local_2026_preliminary",
                impairment="boundary_grid_and_gaussian",
                kind=kind,
                frequency_hz=frequency,
                vpp_counts=vpp,
                duty=duty,
                rise_width=rise,
                fall_width=fall,
                noise_ratio=noise_ratio,
                noise_mode=noise_modes[index % len(noise_modes)],
                sample_rate_hz=sample_rates[index % len(sample_rates)],
                phase=(0.61803398875 * index + rng.random()) % 1.0,
                center_mode=center_modes[index % len(center_modes)],
                reference_uv=3_000_000 + (index % 7) * 60_000,
                gain_ppm=800_000 + (index % 9) * 87_500,
                inl_lsb=[0.0, 0.25, 0.50][index % 3],
            ))
    return cases


def challenge_cases(seed: int, cases_per_profile: int) -> list[Case]:
    """生成超出初赛基本门禁的赛事迁移挑战用例。

    profiles 中每项给出参考题源标签、失真名称和 Case 可选字段。默认报告
    挑战失败但不改变退出码；传 ``--require-challenge`` 才将其设为硬门禁。
    """
    # 与 gate 使用不同但可推导的随机序列，避免两组用例相位偶然同步。
    rng = random.Random(seed ^ 0xA5A5A5A5)
    profiles = [
        ("nuedc_2020_e_2021_a", "top_clip", {"clip_top": 0.08}),
        ("nuedc_2020_e_2021_a", "bottom_clip", {"clip_bottom": 0.08}),
        ("nuedc_2020_e_2021_a", "two_sided_clip",
         {"clip_top": 0.06, "clip_bottom": 0.06}),
        ("nuedc_2020_e_2021_a", "crossover", {"crossover": 0.05}),
        ("nuedc_2021_a", "harmonic_2_3",
         {"harmonic_2": 0.04, "harmonic_3": 0.06}),
        ("nuedc_2022_f_2023_d", "slow_am",
         {"am_depth": 0.06, "am_rate_hz": 13.0}),
        ("nuedc_2023_h", "coherent_interferer",
         {"interferer_ratio": 0.08, "interferer_frequency_ratio": 1.73}),
        ("nuedc_2023_h", "fifth_harmonic_interferer",
         {"interferer_ratio": 0.08, "interferer_frequency_ratio": 5.0}),
    ]
    cases: list[Case] = []
    for source, impairment, settings in profiles:
        for index in range(cases_per_profile):
            # Distortion mother problems use sinusoidal excitation.  The
            # interference profiles rotate over all four preliminary labels.
            kind = ("sine" if source in {"nuedc_2020_e_2021_a", "nuedc_2021_a"}
                    else list(TYPE_ID)[index % 4])
            duty = [0.20, 0.35, 0.50, 0.65, 0.80][index % 5]
            cases.append(make_case(
                group="competition_challenge",
                source=source,
                impairment=impairment,
                kind=kind,
                frequency_hz=[100.137, 173.3, 251.7, 333.333, 399.863][index % 5],
                vpp_counts=[220.0, 800.0, 1800.0, 2800.0][index % 4],
                duty=duty,
                rise_width=(0.02 * (index % 6)) if kind == "square" else 0.0,
                fall_width=(0.02 * ((index * 3) % 6)) if kind == "square" else 0.0,
                noise_ratio=[0.10, 0.15, 0.20][index % 3],
                noise_mode=["white", "lowpass_5f", "highband"][index % 3],
                sample_rate_hz=[19800.0, 20000.0, 20200.0][index % 3],
                phase=rng.random(),
                center_mode=["low", "middle", "high"][index % 3],
                reference_uv=3_300_000,
                gain_ppm=1_000_000,
                inl_lsb=0.5,
                **settings,
            ))
    return cases


def load_library(path: Path):
    """加载主机共享库，并声明 C 函数的精确参数和返回类型。"""
    library = ctypes.CDLL(str(path))
    library.waveform_analyze_frame.argtypes = [
        ctypes.POINTER(WaveformFrame), ctypes.POINTER(WaveformResult)
    ]
    library.waveform_analyze_frame.restype = ctypes.c_bool
    library.waveform_analyze_frame_with_algorithm.argtypes = [
        ctypes.POINTER(WaveformFrame), ctypes.POINTER(WaveformResult),
        ctypes.c_int,
    ]
    library.waveform_analyze_frame_with_algorithm.restype = ctypes.c_bool
    return library


def analyze_case(library, case: Case, sequence: int, seed: int,
                 thresholds: dict[str, float], algorithm: int) -> dict[str, object]:
    """生成、分析并判定单个用例，返回可 JSON 序列化的完整记录。

    expected Vpp 会结合该用例自己的参考电压和前端增益换算到源端 uV。
    频率/Vpp 用相对百分比，方波占空比用百分点；算法 false 时误差设 100%，
    避免缺测值被错误地统计为 0 误差。
    """
    samples = generate_samples(case, seed)
    array = (ctypes.c_uint16 * SAMPLE_COUNT)(*samples)
    # ctypes 会让 array 在本函数结束前保持存活；C 调用是同步的，不保存指针。
    frame = WaveformFrame(
        samples=array,
        sample_count=SAMPLE_COUNT,
        adc_full_scale=ADC_FULL_SCALE,
        sample_rate_millihz=int(round(case.sample_rate_hz * 1000.0)),
        adc_reference_microvolts=case.reference_uv,
        frontend_gain_ppm=case.gain_ppm,
        sequence=sequence,
        flags=(FRAME_CONTIGUOUS | FRAME_CLOCK_CALIBRATED | FRAME_VREF_CALIBRATED),
    )
    result = WaveformResult()
    valid = bool(library.waveform_analyze_frame_with_algorithm(
        ctypes.byref(frame), ctypes.byref(result), algorithm))
    if result.algorithm_version != ALGORITHM_VERSION:
        raise RuntimeError(
            f"algorithm version {result.algorithm_version:#010x} != "
            f"{ALGORITHM_VERSION:#010x}"
        )

    got_kind = TYPE_NAME[result.type] if 0 <= result.type < len(TYPE_NAME) else "invalid"
    status = (STATUS_NAME[result.status]
              if 0 <= result.status < len(STATUS_NAME) else "INVALID_STATUS")
    measured_frequency = result.frequency_millihz / 1000.0
    # 源端 Vpp = ADC码幅/满量程 * Vref / (gain_ppm/1e6)。
    expected_vpp_uv = (
        case.vpp_counts * case.reference_uv / ADC_FULL_SCALE
        * 1_000_000.0 / case.gain_ppm
    )
    frequency_error = (abs(measured_frequency - case.frequency_hz)
                       / case.frequency_hz * 100.0 if valid else 100.0)
    vpp_error = (abs(result.vpp_microvolts - expected_vpp_uv)
                 / expected_vpp_uv * 100.0 if valid else 100.0)
    duty_error = (abs(result.duty_ppm / 1_000_000.0 - case.duty) * 100.0
                  if valid and case.kind == "square" else 0.0)
    passed = (
        valid and status == "OK" and got_kind == case.kind
        and frequency_error <= thresholds["frequency_error_pct"]
        and vpp_error <= thresholds["vpp_error_pct"]
        and duty_error <= thresholds["duty_error_pp"]
    )
    return {
        "sequence": sequence,
        "case": asdict(case),
        "valid": valid,
        "status": status,
        "got_kind": got_kind,
        "measured_frequency_hz": measured_frequency,
        "measured_vpp_uv": result.vpp_microvolts,
        "measured_duty_pct": (result.duty_ppm / 10_000.0
                              if case.kind == "square" and valid else None),
        "frequency_error_pct": frequency_error,
        "vpp_error_pct": vpp_error,
        "duty_error_pp": duty_error,
        "best_score_permille": result.best_score_permille,
        "model_margin_permille": result.model_margin_permille,
        "accepted_cycles": result.accepted_cycles,
        "passed": passed,
    }


def summarize(rows: Iterable[dict[str, object]]) -> dict[str, object]:
    """汇总一组记录的通过数、最坏误差、最低余量和前 12 个失败详情。"""
    # Iterable 可能是生成器；先物化一次，后面才能多次扫描。
    rows = list(rows)
    valid_rows = [row for row in rows if row["valid"]]
    passed_rows = [row for row in rows if row["passed"]]
    return {
        "total": len(rows),
        "passes": sum(bool(row["passed"]) for row in rows),
        "status_ok": sum(row["status"] == "OK" for row in rows),
        "class_correct": sum(row["got_kind"] == row["case"]["kind"] for row in rows),
        "max_frequency_error_pct": max(row["frequency_error_pct"] for row in rows),
        "max_vpp_error_pct": max(row["vpp_error_pct"] for row in rows),
        "max_duty_error_pp": max(row["duty_error_pp"] for row in rows),
        "max_passed_frequency_error_pct": (
            max(row["frequency_error_pct"] for row in passed_rows)
            if passed_rows else None
        ),
        "max_passed_vpp_error_pct": (
            max(row["vpp_error_pct"] for row in passed_rows)
            if passed_rows else None
        ),
        "max_passed_duty_error_pp": (
            max(row["duty_error_pp"] for row in passed_rows)
            if passed_rows else None
        ),
        "min_model_margin_permille": (
            min(row["model_margin_permille"] for row in valid_rows)
            if valid_rows else None
        ),
        "failures": [row for row in rows if not row["passed"]][:12],
    }


def print_summary(name: str, summary: dict[str, object]) -> None:
    """向终端打印一行总览及最多四个代表性失败，完整数据仍在 JSON 中。"""
    print(
        f"{name}: pass={summary['passes']}/{summary['total']} "
        f"class={summary['class_correct']}/{summary['total']} "
        f"status_ok={summary['status_ok']}/{summary['total']} "
        f"worst(freq/vpp/duty)={summary['max_frequency_error_pct']:.4f}%/"
        f"{summary['max_vpp_error_pct']:.3f}%/"
        f"{summary['max_duty_error_pp']:.3f}pp"
    )
    for failure in summary["failures"][:4]:
        case = failure["case"]
        print(
            "  FAIL "
            f"source={case['source']} impairment={case['impairment']} "
            f"expected={case['kind']} got={failure['got_kind']} "
            f"status={failure['status']} f={case['frequency_hz']:.3f}Hz "
            f"errors={failure['frequency_error_pct']:.3f}%/"
            f"{failure['vpp_error_pct']:.3f}%/"
            f"{failure['duty_error_pp']:.3f}pp"
        )


def main() -> int:
    """解析参数、运行门禁/挑战组、写 JSON，并按门禁规则返回退出码。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--json", type=Path,
                        default=Path("tests/results/competition_stress_report.json"))
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--gate-cases-per-kind", type=int, default=50)
    parser.add_argument("--challenge-cases-per-profile", type=int, default=12)
    parser.add_argument("--require-challenge", action="store_true")
    parser.add_argument("--algorithm", type=int, choices=sorted(ALGORITHM_NAME),
                        default=3,
                        help="1=legacy_amdf_crossing, 2=global_harmonic_model, 3=fast_phase_model")
    args = parser.parse_args()

    # 门禁和挑战的阈值分开定义；挑战含超纲失真，因此容忍度略宽。
    gate_thresholds = {
        "frequency_error_pct": 0.50,
        "vpp_error_pct": 8.0,
        "duty_error_pp": 2.0,
    }
    challenge_thresholds = {
        "frequency_error_pct": 1.0,
        "vpp_error_pct": 12.0,
        "duty_error_pp": 3.0,
    }
    library = load_library(args.library.resolve())
    gate = gate_cases(args.seed, args.gate_cases_per_kind)
    challenge = challenge_cases(args.seed, args.challenge_cases_per_profile)

    # 乘较大的质数派生逐例 seed，减少相邻序号产生相似随机序列的机会。
    gate_rows = [
        analyze_case(library, case, index + 1, args.seed + index * 7919,
                     gate_thresholds, args.algorithm)
        for index, case in enumerate(gate)
    ]
    challenge_rows = [
        analyze_case(library, case, len(gate) + index + 1,
                     args.seed ^ (index * 104729), challenge_thresholds,
                     args.algorithm)
        for index, case in enumerate(challenge)
    ]
    gate_summary = summarize(gate_rows)
    challenge_summary = summarize(challenge_rows)
    print_summary("preliminary_gate", gate_summary)
    print_summary("competition_challenge(non-gating)", challenge_summary)

    # 报告不仅保存摘要，还保存每例输入参数和输出，便于从失败反查条件。
    report = {
        "schema_version": 1,
        "seed": args.seed,
        "algorithm_version": ALGORITHM_VERSION,
        "algorithm": args.algorithm,
        "algorithm_name": ALGORITHM_NAME[args.algorithm],
        "sources": {
            "local_2026_preliminary": "docs/2026TI杯赛道一赛事手册.pdf",
            "nuedc_2020_e_2021_a": [
                "https://res.nuedc-training.com.cn/html/news_226.html",
                "https://www.nuedc-training.com.cn/index/news/details/new_id/266",
            ],
            "nuedc_2021_a": "https://www.nuedc-training.com.cn/index/news/details/new_id/266",
            "nuedc_2022_f_2023_d": [
                "https://res.nuedc-training.com.cn/topic/2022/topic_from_15.html",
                "https://res.nuedc-training.com.cn/topic/2023/topic_98.html",
            ],
            "nuedc_2023_h": "https://res.nuedc-training.com.cn/topic/2023/topic_102.html",
            "nuedc_2026_g": "https://res.nuedc-training.com.cn/topic/2026/topic_139.html",
        },
        "gate_thresholds": gate_thresholds,
        "challenge_thresholds": challenge_thresholds,
        "preliminary_gate": gate_summary,
        "competition_challenge": challenge_summary,
        "rows": gate_rows + challenge_rows,
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report={args.json}")

    # 默认只有 preliminary_gate 决定退出码；--require-challenge 可提升挑战组。
    gate_ok = gate_summary["passes"] == gate_summary["total"]
    challenge_ok = challenge_summary["passes"] == challenge_summary["total"]
    return 0 if gate_ok and (challenge_ok or not args.require_challenge) else 1


if __name__ == "__main__":
    sys.exit(main())
