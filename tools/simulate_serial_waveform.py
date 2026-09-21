#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MSPM0G3507 波形识别工程 - 串口带噪波形模拟与识别效果自动化测试工具

功能：
1. 产生符合全国大学生电子设计竞赛（TI杯）初赛规范的带噪模拟信号：
   - 4 类波形：正弦波 (SINE)、方波 (SQUARE)、三角波 (TRIANGLE)、下降锯齿波 (SAWTOOTH)
   - 频率范围：100 Hz ~ 400 Hz
   - 占空比范围：20% ~ 80% (方波)
   - 噪声等级：叠加 0% ~ 20% Vpp 高斯与多频随机噪声
   - 采样规格：20,000 Hz 采样率，4096 个 12-bit ADC 点 (0 ~ 4095)
2. 串口注入测试模式 (HIL In-the-Loop)：
   - 通过串口向单片机发送 'W' 指令
   - 实时注入 4096 个 16-bit 模拟采样点 (8192 字节)
   - 接收单片机 Cortex-M0+ 算法处理返回的 RESULT 结果
   - 与真实输入参数对比，计算分类正确率、频率误差、Vpp 误差、占空比误差及分类置信余量
3. 片上仿真触发测试模式：
   - 发送 'T' 指令触发单片机片上生成带噪波并分析
4. 批量评测与报告生成：
   - 支持全自动覆盖所有波形种类和频段，生成综合评分卡与 CSV 测试日志

零基础提示：本工具中的“真值”来自生成波形时使用的参数。host 模式直接
调用电脑上的 C DLL；inject 模式把 ADC 整数发给板端；onchip 模式只发送
触发命令，由固件内部轮流生成四类固定波形。三种模式的数据来源不同，
因此结果不能不加说明地混为同一次硬件 ADC 校准证据。
"""

import sys
import time
import re
import math
import random
import struct
import argparse
import csv
from datetime import datetime

# Windows 旧终端默认编码可能不是 UTF-8；尽量重配，失败也不影响核心测试。
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# pyserial 和 rich 都是可选依赖。没有串口库时 host 模式仍应可用；没有
# rich 时退回普通 print，算法结果不受显示方式影响。
try:
    import serial
    import serial.tools.list_ports
    HAVE_SERIAL = True
except ImportError:
    serial = None
    HAVE_SERIAL = False

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import print as rprint
    HAVE_RICH = True
except ImportError:
    HAVE_RICH = False

console = Console() if HAVE_RICH else None

SAMPLE_RATE_HZ = 20000
SAMPLE_COUNT = 4096
ADC_VDDA_MV = 3300.0
ADC_MAX_COUNT = 4095.0
ALGORITHM_VERSION = 0x00020000

import ctypes
import os

class WaveformResult(ctypes.Structure):
    """C ``waveform_result_t`` 的内存布局镜像。

    ctypes 不是按字段名与 C 对接，而是按字节偏移读内存，所以类型和顺序
    必须与 waveform_analyzer.h 完全一致。结构体一旦修改，所有 ctypes
    镜像都必须同步，并重新编译 DLL。
    """

    _fields_ = [
        ('type', ctypes.c_int),
        ('status', ctypes.c_int),
        ('frequency_millihz', ctypes.c_uint32),
        ('vpp_millivolts', ctypes.c_uint32),
        ('duty_permille', ctypes.c_uint16),
        ('confidence_permille', ctypes.c_uint16),
        ('best_score_permille', ctypes.c_uint16),
        ('second_score_permille', ctypes.c_uint16),
        ('accepted_cycles', ctypes.c_uint16),
        ('high_clip_samples', ctypes.c_uint16),
        ('dc_offset_counts', ctypes.c_uint16),
        ('low_clip_samples', ctypes.c_uint16),
        ('invalid_samples', ctypes.c_uint16),
        ('algorithm_version', ctypes.c_uint32),
        ('sequence', ctypes.c_uint32),
        ('vpp_microvolts', ctypes.c_uint32),
        ('duty_ppm', ctypes.c_uint32),
        ('period_q16_samples', ctypes.c_uint32),
        ('vpp_counts_q16', ctypes.c_uint32),
        ('quality_flags', ctypes.c_uint16),
        ('model_margin_permille', ctypes.c_uint16),
    ]

def analyze_waveform_host(samples):
    """使用同目录 DLL 中的原版 C 核心算法分析一帧样本。

    若 DLL 不存在返回 None；存在但版本错、加载失败或调用异常也打印错误
    并返回 None。成功则把 C 枚举和定点单位转换成便于 UI/CSV 使用的字典。
    """
    dll_path = os.path.join(os.path.dirname(__file__), "waveform_analyzer.dll")
    if not os.path.exists(dll_path):
        return None
    try:
        dll = ctypes.CDLL(dll_path)
        # 明确签名可防止 ctypes 把 64 位指针误当默认 int。
        dll.waveform_analyze.argtypes = [ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(WaveformResult)]
        dll.waveform_analyze.restype = ctypes.c_bool

        arr = (ctypes.c_uint16 * SAMPLE_COUNT)(*samples)
        res_c = WaveformResult()
        valid = dll.waveform_analyze(arr, ctypes.byref(res_c))
        if res_c.algorithm_version != ALGORITHM_VERSION:
            raise RuntimeError(
                f"DLL算法版本 {res_c.algorithm_version:#010x} 与要求的 "
                f"{ALGORITHM_VERSION:#010x} 不一致，请重新编译DLL"
            )
        type_names = ['sine', 'square', 'triangle', 'sawtooth', 'unknown']
        status_names = ['OK', 'SIGNAL_TOO_SMALL', 'ADC_HIGH_CLIPPED', 'PERIOD_NOT_FOUND',
                        'FREQUENCY_OUT_OF_RANGE', 'ADC_LOW_CLIPPED',
                        'ADC_SAMPLE_INVALID', 'CLASSIFICATION_AMBIGUOUS',
                        'BAD_ARGUMENT', 'BAD_METADATA', 'INSUFFICIENT_SAMPLES']

        t_name = type_names[res_c.type] if 0 <= res_c.type < len(type_names) else 'unknown'
        s_name = status_names[res_c.status] if 0 <= res_c.status < len(status_names) else 'UNKNOWN'

        # 分数是千分比，差值除以 10 后成为百分数，例如 150 -> 15.0%。
        margin = 0.0
        if res_c.best_score_permille >= res_c.second_score_permille:
            margin = (res_c.best_score_permille - res_c.second_score_permille) / 10.0

        return {
            "status": s_name,
            "type": t_name,
            "freq_hz": res_c.frequency_millihz / 1000.0 if valid else None,
            "vpp_v": res_c.vpp_millivolts / 1000.0 if valid else None,
            "duty_pct": res_c.duty_permille / 10.0 if (valid and t_name == "square") else None,
            "score_pct": res_c.best_score_permille / 10.0 if valid else None,
            "margin_pct": margin if valid else None,
            "cycles": res_c.accepted_cycles if valid else None,
            "raw": f"HOST_RESULT,status={s_name},type={t_name}"
        }
    except Exception as e:
        print(f"[DLL分析错误] {e}")
        return None

# RESULT 的字段顺序由固件 print_result() 固定。失败行只含前半部分诊断字段，
# 所以后续组是可选的；解析时使用 fullmatch，拒绝尾部乱码或未知残片。
RESULT_REGEX = re.compile(
    r"RESULT,status=(?P<status>[^,]+)"
    r",type=(?P<type>[^,]+)"
    r"(?:,freq_hz=(?P<freq_hz>[0-9.]+))?"
    r"(?:,vpp_v=(?P<vpp_v>[0-9.]+))?"
    r"(?:,duty_pct=(?P<duty_pct>[0-9.]+|NA))?"
    r"(?:,score_pct=(?P<score_pct>[0-9.]+))?"
    r"(?:,margin_pct=(?P<margin_pct>[0-9.]+))?"
    r"(?:,cycles=(?P<cycles>[0-9]+))?"
    r"(?:,high_clip_samples=(?P<high_clip_samples>[0-9]+))?"
    r"(?:,low_clip_samples=(?P<low_clip_samples>[0-9]+))?"
    r"(?:,invalid_samples=(?P<invalid_samples>[0-9]+))?"
    r"(?:,freq_millihz=(?P<freq_millihz>[0-9]+))?"
    r"(?:,vpp_microvolts=(?P<vpp_microvolts>[0-9]+))?"
    r"(?:,duty_ppm=(?P<duty_ppm>[0-9]+|NA))?"
    r"(?:,sequence=(?P<sequence>[0-9]+))?"
    r"(?:,algorithm_version=(?P<algorithm_version>[0-9]+))?"
    r"(?:,quality_flags=(?P<quality_flags>[0-9]+))?"
)

def find_xds110_port(specified_port=None):
    """按“显式端口 -> XDS110 Application -> COM15 -> 首个端口”选择串口。

    返回 ``(设备名, 人类可读说明)``；完全没有端口或指定端口不存在时设备
    名为 None。自动回退方便调试，但正式审计应显式给 ``--port``。
    """
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None, "未找到任何可用串口"

    if specified_port:
        for p in ports:
            if p.device.upper() == specified_port.upper():
                return p.device, f"指定端口: {p.device} ({p.description})"
        return None, f"指定端口不存在: {specified_port}"

    for p in ports:
        desc = p.description or ""
        if "XDS110" in desc and "Application" in desc:
            return p.device, f"匹配到 TI XDS110 虚拟串口: {p.device} ({p.description})"

    for p in ports:
        if p.device.upper() == "COM15":
            return p.device, f"匹配到 COM15: {p.description}"

    return ports[0].device, f"默认使用: {ports[0].device} ({ports[0].description})"

def generate_noisy_waveform(wave_type="sine", freq_hz=200.0, vpp_volts=2.0,
                            duty_ratio=0.5, dc_offset_volts=1.65, noise_ratio=0.15):
    """
    生成 4096 点 12 位 ADC 带噪波形采样数组
    :param wave_type: 'sine', 'square', 'triangle', 'sawtooth'
    :param freq_hz: 频率 100 ~ 400 Hz
    :param vpp_volts: 峰峰值电压 0.5 ~ 3.0 V
    :param duty_ratio: 方波占空比 0.2 ~ 0.8
    :param dc_offset_volts: 直流偏置电压 (保证波形在 0 ~ 3.3V 间)
    :param noise_ratio: 噪声峰峰值与信号峰峰值的比例 (例如 0.15 = 15% 噪声)
    :return: list of 4096 uint16 values, ground_truth dict
    """
    # dt 是相邻样本时间；20 kHz 时为 50 us。
    dt = 1.0 / SAMPLE_RATE_HZ
    samples = []
    vpp_counts = (vpp_volts / 3.3) * 4095.0
    center_counts = (dc_offset_volts / 3.3) * 4095.0
    half_amp = vpp_counts / 2.0
    start_phase = random.uniform(0.0, 2.0 * math.pi)

    # 三个噪声正弦位于主频 3~15 倍处，各自相位随机。
    noise_freqs = [freq_hz * random.uniform(3.0, 15.0) for _ in range(3)]
    noise_phases = [random.uniform(0.0, 2.0 * math.pi) for _ in range(3)]

    for i in range(SAMPLE_COUNT):
        t = i * dt
        phase = ((freq_hz * t) + (start_phase / (2.0 * math.pi))) % 1.0

        # 生成理想无噪波形归一化值 (-1.0 ~ +1.0)
        if wave_type == "sine":
            val = math.sin(2.0 * math.pi * phase)
        elif wave_type == "square":
            val = 1.0 if phase < duty_ratio else -1.0
        elif wave_type == "triangle":
            if phase < 0.25:
                val = 4.0 * phase
            elif phase < 0.75:
                val = 2.0 - (4.0 * phase)
            else:
                val = (4.0 * phase) - 4.0
        elif wave_type == "sawtooth":
            # 下降锯齿波
            val = 1.0 - (2.0 * phase)
        else:
            val = 0.0

        # 归一化波形乘信号半幅，再加直流中心，得到 ADC 浮点码。
        signal_count = center_counts + (val * half_amp)

        # 叠加高斯随机噪声与多频噪声 (幅值上限按 noise_ratio 严格控制)
        if noise_ratio > 0:
            gauss_noise = random.gauss(0.0, 0.35)  # 约 99% 在 -1.0 ~ +1.0
            harm_noise = sum(math.sin(2.0 * math.pi * f * t + ph) for f, ph in zip(noise_freqs, noise_phases)) / 3.0
            combined_noise = (0.7 * gauss_noise) + (0.3 * harm_noise)
            # 限制在 -1.0 ~ +1.0 并乘以指定噪声幅值
            combined_noise = max(-1.0, min(1.0, combined_noise))
            noise_counts = combined_noise * (vpp_counts * noise_ratio * 0.5)
            signal_count += noise_counts

        # 12-bit ADC 截断 (0 ~ 4095)
        code = int(round(signal_count))
        code = max(0, min(4095, code))
        samples.append(code)

    ground_truth = {
        "type": wave_type.lower(),
        "freq_hz": freq_hz,
        "vpp_v": vpp_volts,
        "duty_pct": (duty_ratio * 100.0) if wave_type == "square" else None,
        "noise_pct": noise_ratio * 100.0,
        "dc_v": dc_offset_volts
    }
    return samples, ground_truth

def inject_waveform_and_get_result(ser, samples, timeout=3.5):
    """执行 W 注入事务并返回 ``(解析结果或 None, 串口文本列表)``。

    只有收到 READY_FOR_DATA 才发送 8192 字节，避免协议不同步。样本按
    little-endian uint16 打包并分块发送；随后收集到 RESULT 且再次 READY。
    timeout 是结果阶段上限，握手另有 1 秒上限。
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # 1. 发送 'W' 告知单片机准备进入注入模式
    ser.write(b'W')
    ser.flush()

    # 等待单片机回复 READY_FOR_DATA
    t0 = time.time()
    ready = False
    while time.time() - t0 < 1.0:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if "READY_FOR_DATA" in line:
            ready = True
            break
        elif "CAPTURING" in line or "READY" in line:
            pass

    if not ready:
        return None, ["ERROR: READY_FOR_DATA handshake timeout; payload not sent"]

    # 2. 打包发送 4096 个 uint16 (8192 字节，Little-Endian)
    # '<' 表示小端，H 表示 16 位无符号；长度必须为 4096*2=8192 字节。
    payload = struct.pack(f"<{SAMPLE_COUNT}H", *samples)
    # 分块发送避免 Windows 串口驱动瞬时吞吐拥塞
    chunk_size = 512
    for i in range(0, len(payload), chunk_size):
        ser.write(payload[i:i+chunk_size])
    ser.flush()

    # 3. 接收 MCU 分析结果
    t0 = time.time()
    result_data = None
    log_lines = []

    while time.time() - t0 < timeout:
        raw_line = ser.readline()
        if not raw_line:
            continue
        line_str = raw_line.decode('utf-8', errors='ignore').strip()
        if not line_str:
            continue
        log_lines.append(line_str)
        if line_str.startswith("RESULT"):
            m = RESULT_REGEX.fullmatch(line_str)
            if m:
                d = m.groupdict()
                result_data = {
                    "status": d.get("status", "UNKNOWN"),
                    "type": d.get("type", "unknown").lower(),
                    "freq_hz": float(d["freq_hz"]) if d.get("freq_hz") else None,
                    "vpp_v": float(d["vpp_v"]) if d.get("vpp_v") else None,
                    "duty_pct": float(d["duty_pct"]) if d.get("duty_pct") and d["duty_pct"] != "NA" else None,
                    "score_pct": float(d["score_pct"]) if d.get("score_pct") else None,
                    "margin_pct": float(d["margin_pct"]) if d.get("margin_pct") else None,
                    "cycles": int(d["cycles"]) if d.get("cycles") else None,
                    "high_clip_samples": int(d["high_clip_samples"]) if d.get("high_clip_samples") else 0,
                    "low_clip_samples": int(d["low_clip_samples"]) if d.get("low_clip_samples") else 0,
                    "invalid_samples": int(d["invalid_samples"]) if d.get("invalid_samples") else 0,
                    "raw": line_str
                }
        elif line_str == "READY" and result_data is not None:
            break

    return result_data, log_lines

def trigger_onchip_selftest(ser, timeout=2.0):
    """发送 T 触发固件片上生成波形，并读取到 RESULT/READY。

    片上自测每次自动轮换类别。调用方预设的 gt 只有在调用顺序与固件当前
    g_selftest_case 同步时才成立，不能把它当成独立的模拟输入证明。
    """
    ser.reset_input_buffer()
    ser.write(b'T')
    ser.flush()

    t0 = time.time()
    result_data = None
    log_lines = []

    while time.time() - t0 < timeout:
        raw_line = ser.readline()
        if not raw_line:
            continue
        line_str = raw_line.decode('utf-8', errors='ignore').strip()
        if not line_str:
            continue
        log_lines.append(line_str)
        if line_str.startswith("RESULT"):
            m = RESULT_REGEX.fullmatch(line_str)
            if m:
                d = m.groupdict()
                result_data = {
                    "status": d.get("status", "UNKNOWN"),
                    "type": d.get("type", "unknown").lower(),
                    "freq_hz": float(d["freq_hz"]) if d.get("freq_hz") else None,
                    "vpp_v": float(d["vpp_v"]) if d.get("vpp_v") else None,
                    "duty_pct": float(d["duty_pct"]) if d.get("duty_pct") and d["duty_pct"] != "NA" else None,
                    "score_pct": float(d["score_pct"]) if d.get("score_pct") else None,
                    "margin_pct": float(d["margin_pct"]) if d.get("margin_pct") else None,
                    "cycles": int(d["cycles"]) if d.get("cycles") else None,
                    "high_clip_samples": int(d["high_clip_samples"]) if d.get("high_clip_samples") else 0,
                    "low_clip_samples": int(d["low_clip_samples"]) if d.get("low_clip_samples") else 0,
                    "invalid_samples": int(d["invalid_samples"]) if d.get("invalid_samples") else 0,
                    "raw": line_str
                }
        elif line_str == "READY" and result_data is not None:
            break

    return result_data, log_lines

def evaluate_case(gt, res):
    """对比生成真值与识别结果，返回 ``(是否通过, 详细误差字典)``。

    分类必须相同；频率相对误差 <=2%，Vpp 相对误差 <=8%；方波占空比
    绝对误差 <=3 个百分点。缺失测量被视为 100% 误差，绝不会默认通过。
    """
    if not res:
        return False, {"passed": False, "type_match": False, "freq_err_pct": 100.0, "vpp_err_pct": 100.0, "duty_err": None, "reason": "无有效响应"}
    if res.get("status") != "OK":
        return False, {"passed": False, "type_match": False, "freq_err_pct": 100.0, "vpp_err_pct": 100.0, "duty_err": None, "reason": res.get("status", "ERROR")}

    type_match = (gt["type"] == res["type"])
    
    # 频率误差 (允许 ±2.0%)
    freq_err_pct = 100.0
    if res.get("freq_hz") is not None and gt.get("freq_hz"):
        freq_err_pct = abs(res["freq_hz"] - gt["freq_hz"]) / gt["freq_hz"] * 100.0
    freq_ok = (res.get("freq_hz") is not None and freq_err_pct <= 2.0)

    # 峰峰值误差 (允许 ±8.0%)
    vpp_err_pct = 100.0
    if res.get("vpp_v") is not None and gt.get("vpp_v"):
        vpp_err_pct = abs(res["vpp_v"] - gt["vpp_v"]) / gt["vpp_v"] * 100.0
    vpp_ok = (res.get("vpp_v") is not None and vpp_err_pct <= 8.0)

    # 占空比误差 (允许 ±3.0 百分点)
    duty_ok = gt["type"] != "square"
    duty_err = None
    if gt["type"] == "square":
        if gt["duty_pct"] is not None and res.get("duty_pct") is not None:
            duty_err = abs(res["duty_pct"] - gt["duty_pct"])
            duty_ok = (duty_err <= 3.0)
        else:
            duty_ok = False

    passed = type_match and freq_ok and vpp_ok and duty_ok
    details = {
        "type_match": type_match,
        "freq_err_pct": freq_err_pct,
        "vpp_err_pct": vpp_err_pct,
        "duty_err": duty_err if gt["type"] == "square" else None,
        "passed": passed,
        "reason": res.get("status", "OK")
    }
    return passed, details

def print_result_table(gt, res, details, idx=1):
    """用 rich 表格或纯文本展示一次真值/实测/误差对比；不改变判定。"""
    if not res:
        print(f"[{idx:02d}] 注入超时或无响应！")
        return

    passed_str = "[PASS]" if details["passed"] else "[FAIL]"

    if HAVE_RICH:
        table = Table(title=f"测试用例 #{idx:02d} 评测结果 {passed_str}", border_style="cyan")
        table.add_column("评估指标", style="bold")
        table.add_column("真实输入 (Ground Truth)", style="magenta")
        table.add_column("单片机实测 (MCU Result)", style="white")
        table.add_column("误差 / 表现", style="yellow")
        table.add_column("判定", style="bold")

        # 类别
        t_style = "green" if details["type_match"] else "red"
        table.add_row("波形类别", gt["type"].upper(), res["type"].upper(),
                      f"噪声强度={gt['noise_pct']:.1f}%", f"[{t_style}]{'PASS' if details['type_match'] else 'FAIL'}[/{t_style}]")

        # 频率
        f_style = "green" if details["freq_err_pct"] <= 2.0 else "red"
        f_str = f"{res['freq_hz']:.3f} Hz" if res["freq_hz"] is not None else "N/A"
        table.add_row("频率 (Frequency)", f"{gt['freq_hz']:.3f} Hz", f_str,
                      f"误差: {details['freq_err_pct']:.2f}%", f"[{f_style}]{'PASS' if details['freq_err_pct'] <= 2.0 else 'FAIL'}[/{f_style}]")

        # Vpp
        v_style = "green" if details["vpp_err_pct"] <= 8.0 else "red"
        v_str = f"{res['vpp_v']:.3f} V" if res["vpp_v"] is not None else "N/A"
        table.add_row("峰峰值 (Vpp)", f"{gt['vpp_v']:.3f} V", v_str,
                      f"误差: {details['vpp_err_pct']:.2f}%", f"[{v_style}]{'PASS' if details['vpp_err_pct'] <= 8.0 else 'FAIL'}[/{v_style}]")

        # 方波占空比
        if gt["type"] == "square":
            d_err = details["duty_err"] or 0.0
            d_style = "green" if d_err <= 3.0 else "red"
            d_str = f"{res['duty_pct']:.1f} %" if res["duty_pct"] is not None else "N/A"
            table.add_row("占空比 (Duty)", f"{gt['duty_pct']:.1f} %", d_str,
                          f"误差: {d_err:.2f} %", f"[{d_style}]{'PASS' if d_err <= 3.0 else 'FAIL'}[/{d_style}]")

        # 匹配得分与置信余量
        score_str = f"匹配得分: {res['score_pct']:.1f} %" if res["score_pct"] is not None else "N/A"
        margin_str = f"置信余量: {res['margin_pct']:.1f} %" if res["margin_pct"] is not None else "N/A"
        margin_qual = "[green]EXCELLENT[/green]" if (res['margin_pct'] or 0) >= 15.0 else "[yellow]GOOD[/yellow]"
        table.add_row("模板得分 / 置信余量", "-", score_str, margin_str, margin_qual)

        console.print(table)
    else:
        print("-" * 60)
        f_val = f"{res['freq_hz']:.2f}Hz" if res['freq_hz'] is not None else "N/A"
        v_val = f"{res['vpp_v']:.2f}V" if res['vpp_v'] is not None else "N/A"
        m_val = f"{res['margin_pct']:.1f}%" if res['margin_pct'] is not None else "N/A"
        print(f"[{idx:02d}] 判定: {passed_str} | 输入: {gt['type'].upper()} {gt['freq_hz']:.1f}Hz Vpp={gt['vpp_v']:.2f}V 噪声={gt['noise_pct']:.0f}%")
        print(f"     实测: {res['type'].upper()} {f_val} Vpp={v_val} 分类余量={m_val}")
        print("-" * 60)

def main():
    """解析三种运行模式、生成用例、执行评测并写 CSV。"""
    if not HAVE_SERIAL:
        print("[错误] 未安装 pyserial，请运行: pip install pyserial")
        return 1
    parser = argparse.ArgumentParser(description="MSPM0 串口带噪波形模拟与识别评测工具")
    parser.add_argument("-p", "--port", type=str, default=None, help="串口号 (如 COM15)")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="波特率 (默认 115200)")
    parser.add_argument("-c", "--count", type=int, default=4, help="测试用例数 (默认 4，覆盖四种波形)")
    parser.add_argument("--noise", type=float, default=0.15, help="噪声强度比例 (默认 0.15 即 15%% Vpp)")
    parser.add_argument("--mode", type=str, choices=["inject", "onchip", "host"], default="inject",
                        help="测试模式: inject(串口注入), onchip(片上生成), host(C核心算法仿真)")
    parser.add_argument("--csv", type=str, default="simulated_test_results.csv", help="导出 CSV 结果文件")
    args = parser.parse_args()

    ser = None
    port_name = "N/A (Host Simulation)"

    # 只有串口模式才查找/打开 COM 口；host 模式无需开发板。
    if args.mode != "host":
        port_name, msg = find_xds110_port(args.port)
        if not port_name:
            print(f"[错误] {msg}")
            return 1

        if HAVE_RICH:
            console.print(Panel.fit(
                f"[bold cyan]MSPM0G3507 串口模拟带噪波形识别评测[/bold cyan]\n"
                f"目标串口: [bold green]{port_name}[/bold green] | 波特率: [bold yellow]{args.baud}[/bold yellow]\n"
                f"测试模式: [bold]{args.mode.upper()}[/bold] | 噪声设置: [bold]{args.noise*100:.0f}% Vpp[/bold] | 样本数: [bold]{args.count}[/bold]\n"
                f"{msg}",
                border_style="blue"
            ))
        else:
            print(f"=== MSPM0G3507 串口模拟带噪波识别评测 ===")
            print(f"端口: {port_name}, 模式: {args.mode}, 噪声: {args.noise*100:.0f}%")

        try:
            ser = serial.Serial(port_name, args.baud, timeout=1.5)
        except serial.SerialException as e:
            print(f"[错误] 无法打开串口 {port_name}: {e}")
            return 1
    else:
        if HAVE_RICH:
            console.print(Panel.fit(
                f"[bold cyan]MSPM0G3507 波形识别算法仿真评测 (HOST 本地 C 核心)[/bold cyan]\n"
                f"运行环境: [bold green]ARM Cortex-M0+ 算法核心 (x86_64 DLL 镜像)[/bold green]\n"
                f"测试模式: [bold]HOST_BENCHMARK[/bold] | 噪声设置: [bold]{args.noise*100:.0f}% Vpp[/bold] | 样本数: [bold]{args.count}[/bold]",
                border_style="green"
            ))
        else:
            print(f"=== MSPM0G3507 算法仿真评测 (HOST 本地 C 核心) ===")

    # 前四例固定覆盖四种类别，便于每次快速肉眼比较。
    test_cases = [
        {"type": "sine", "freq": 200.0, "vpp": 2.0, "duty": 0.5, "noise": args.noise},
        {"type": "square", "freq": 250.0, "vpp": 2.2, "duty": 0.3, "noise": args.noise},
        {"type": "triangle", "freq": 150.0, "vpp": 1.8, "duty": 0.5, "noise": args.noise},
        {"type": "sawtooth", "freq": 300.0, "vpp": 2.0, "duty": 0.5, "noise": args.noise},
    ]

    # 超过四例后追加范围内随机参数；未设 seed，因此用于探索而非严格回归。
    for _ in range(max(0, args.count - 4)):
        w_type = random.choice(["sine", "square", "triangle", "sawtooth"])
        w_freq = round(random.uniform(110.0, 390.0), 1)
        w_vpp = round(random.uniform(1.2, 2.6), 2)
        w_duty = round(random.uniform(0.25, 0.75), 2) if w_type == "square" else 0.5
        w_noise = round(random.uniform(0.05, min(0.20, args.noise)), 2)
        test_cases.append({
            "type": w_type, "freq": w_freq, "vpp": w_vpp,
            "duty": w_duty, "noise": w_noise
        })

    # utf-8-sig 带 BOM，便于 Windows Excel 正确识别中文表头。每次运行覆盖
    # 文件，避免把不同模式或噪声设置混在同一统计中。
    csv_file = open(args.csv, mode="w", newline="", encoding="utf-8-sig")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow([
        "序号", "真实类别", "真实频率(Hz)", "真实Vpp(V)", "真实占空比(%)", "噪声比例(%)",
        "识别类别", "实测频率(Hz)", "实测Vpp(V)", "实测占空比(%)", "匹配得分(%)", "分类余量(%)", "综合判定"
    ])

    passed_count = 0
    total_tests = len(test_cases)

    try:
        for idx, tc in enumerate(test_cases, 1):
            samples, gt = generate_noisy_waveform(
                wave_type=tc["type"], freq_hz=tc["freq"],
                vpp_volts=tc["vpp"], duty_ratio=tc["duty"],
                noise_ratio=tc["noise"]
            )

            if args.mode == "inject":
                if HAVE_RICH:
                    console.print(f"\n[bold blue]>>> [#{idx:02d}/{total_tests}] 正在串口注入带噪波形: {gt['type'].upper()} {gt['freq_hz']:.1f}Hz Vpp={gt['vpp_v']:.2f}V 噪声={gt['noise_pct']:.0f}% ...[/bold blue]")
                else:
                    print(f"\n>>> [#{idx:02d}/{total_tests}] 串口注入: {gt['type'].upper()} {gt['freq_hz']:.1f}Hz Vpp={gt['vpp_v']:.2f}V ...")
                res, logs = inject_waveform_and_get_result(ser, samples)
            elif args.mode == "onchip":
                gt = {"type": tc["type"], "freq_hz": tc["freq"], "vpp_v": tc["vpp"], "duty_pct": tc["duty"]*100, "noise_pct": tc["noise"]*100}
                res, logs = trigger_onchip_selftest(ser)
            else:  # host：不经过串口，直接调用同源 C DLL。
                if HAVE_RICH:
                    console.print(f"\n[bold cyan]>>> [#{idx:02d}/{total_tests}] 仿真分析带噪波形: {gt['type'].upper()} {gt['freq_hz']:.1f}Hz Vpp={gt['vpp_v']:.2f}V 噪声={gt['noise_pct']:.0f}% ...[/bold cyan]")
                else:
                    print(f"\n>>> [#{idx:02d}/{total_tests}] 仿真分析: {gt['type'].upper()} {gt['freq_hz']:.1f}Hz Vpp={gt['vpp_v']:.2f}V ...")
                res = analyze_waveform_host(samples)
                logs = ["HOST_OK"]

            if res:
                passed, details = evaluate_case(gt, res)
                if passed:
                    passed_count += 1
                print_result_table(gt, res, details, idx=idx)

                csv_writer.writerow([
                    idx, gt["type"], gt["freq_hz"], gt["vpp_v"], gt["duty_pct"], gt["noise_pct"],
                    res["type"], res["freq_hz"], res["vpp_v"], res["duty_pct"],
                    res["score_pct"], res["margin_pct"], "PASS" if passed else "FAIL"
                ])
                csv_file.flush()
            else:
                if HAVE_RICH:
                    console.print(f"[bold red][FAIL] [#{idx:02d}] 单片机未返回有效识别结果！串口响应日志: {logs}[/bold red]")
                else:
                    print(f"[FAIL] [#{idx:02d}] 单片机未返回有效识别结果！日志: {logs}")
            
            time.sleep(0.3)

    finally:
        # 无论中途是否异常，都刷新/关闭文件和串口，避免句柄占用。
        csv_file.close()
        if ser:
            ser.close()

    # 打印总结报告
    # test_cases 至少包含前四例，所以这里不会除以 0。
    acc = (passed_count / total_tests) * 100.0
    print("\n" + "=" * 65)
    print("【MSPM0 波形识别综合评测报告】")
    print(f"总测试样本数: {total_tests} 组带噪波形")
    print(f"全部通过数: {passed_count} 组")
    print(f"综合识别准确率: {acc:.1f} %")
    print(f"测试数据已保存至: {args.csv}")
    print("=" * 65 + "\n")

if __name__ == "__main__":
    raise SystemExit(main())
