"""正弦/三角可辨识性的余量敏感性实验（纯主机，不接硬件）。

动机来自实测：真实输入的三角波（约 197 Hz、约 0.94 Vpp）在固件里给出的
模型余量只有 0.8~1.0%，而拒识硬门限是 0.5%，于是同一路静态信号会间歇性
被拒识。本实验回答两个问题：

1. 理想无噪三角波在算法下的余量是多少？
2. 噪声峰峰比升到多少时，三角波的余量会跌到 0.5% 门限以下？

回放使用与固件同源的 waveform_analyzer.c 主机库，因此结论与固件同源。
"""

import argparse
import ctypes
import importlib.util
import json
import math
import random
import struct
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_RATE_HZ = 20000
SAMPLE_COUNT = 4096
CENTER_V = 1.65
FULL_SCALE = 4095
VREF_V = 3.3

MIN_MODEL_MARGIN_PERMILLE = 5


def load_replay_tools():
    """复用 analyze_rejection 的 ctypes 绑定与回放函数，避免第二套结构体。"""
    spec = importlib.util.spec_from_file_location(
        'analyze_rejection', ROOT / 'tools' / 'analyze_rejection.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lowpass(values, cutoff_hz, sample_rate_hz=SAMPLE_RATE_HZ):
    """一阶 RC 低通，模拟模拟前端的有限带宽。

    三角波与正弦波的可辨识信息全在三角波的高次奇谐波里；截止频率越低，
    滤掉的谐波越多，两者就越像。相位滞后不影响按相位折叠的模板拟合。
    """
    rc = 1.0 / (2 * math.pi * cutoff_hz)
    dt = 1.0 / sample_rate_hz
    alpha = dt / (rc + dt)
    out = []
    previous = values[0]
    for value in values:
        previous += alpha * (value - previous)
        out.append(previous)
    return out


def make_signal(kind, frequency_hz, vpp_v, noise_vpp_ratio, seed, cutoff_hz=None):
    """生成一帧 12 位 ADC 码。

    波形先在 [-1, 1] 归一化，可选经过一阶低通模拟前端带宽，再叠加噪声并
    换算电压。噪声按**有限记录的实际峰峰**缩放，而不是按理论标准差估算，
    这样 noise_vpp_ratio 有确切的物理含义。
    """
    rng = random.Random(seed)
    phase_start = rng.random()
    noise = [rng.gauss(0, 1) for _ in range(SAMPLE_COUNT)]
    midpoint = (max(noise) + min(noise)) / 2
    span = max(noise) - min(noise)
    scale = (noise_vpp_ratio * vpp_v / 2) / span if span else 0.0

    waveform = []
    for index in range(SAMPLE_COUNT):
        phase = (phase_start + index * frequency_hz / SAMPLE_RATE_HZ) % 1
        if kind == 'sine':
            waveform.append(math.sin(2 * math.pi * phase))
        else:  # triangle：与固件自测的三段线性三角波一致
            waveform.append(1 - 4 * abs(phase - 0.5))

    if cutoff_hz:
        waveform = lowpass(waveform, cutoff_hz)

    # 低通会压低幅度，按低通后的实际峰值重新归一化到目标 Vpp，
    # 否则幅度变化会和带宽效应混在一起，结论不可归因。
    peak = max(abs(value) for value in waveform) or 1.0
    waveform = [value / peak for value in waveform]

    samples = []
    for index in range(SAMPLE_COUNT):
        voltage = (CENTER_V + waveform[index] * vpp_v / 2
                   + (noise[index] - midpoint) * scale)
        if not 0.0 < voltage < VREF_V:
            raise ValueError(f'生成信号越界: {voltage:.4f} V')
        samples.append(round(voltage * FULL_SCALE / VREF_V))
    return samples


def main():
    parser = argparse.ArgumentParser(description='正弦/三角余量敏感性实验')
    parser.add_argument('--library',
                        default=str(ROOT / 'tools' / 'waveform_analyzer_host.dll'))
    parser.add_argument('--frequency', type=float, default=197.4,
                        help='实测中心频率 Hz')
    parser.add_argument('--vpp', type=float, default=0.94,
                        help='实测峰峰值 V')
    parser.add_argument('--sweep', choices=['noise', 'lowpass'], default='noise',
                        help='noise=扫噪声峰峰比；lowpass=扫前端截止频率')
    parser.add_argument('--noise-ratios', type=float, nargs='+',
                        default=[0.0, 0.02, 0.05, 0.10, 0.15, 0.20])
    parser.add_argument('--cutoffs', type=float, nargs='+',
                        default=[300.0, 500.0, 800.0, 1000.0, 2000.0, 5000.0,
                                 10000.0, 20000.0],
                        help='低通截止频率 Hz（lowpass 模式）')
    parser.add_argument('--trials', type=int, default=30)
    parser.add_argument('--json', default=None)
    args = parser.parse_args()

    analyze = load_replay_tools()
    library = analyze.load_library(args.library)

    report = {
        'generated_local': datetime.now().isoformat(timespec='seconds'),
        'library': args.library,
        'sweep': args.sweep,
        'frequency_hz': args.frequency,
        'vpp_v': args.vpp,
        'trials_per_point': args.trials,
        'gate_margin_permille': MIN_MODEL_MARGIN_PERMILLE,
        'points': [],
    }

    # lowpass 模式下噪声固定为 0，把带宽效应单独隔离出来；noise 模式下
    # 前端视为理想（不滤波），两种模式互不混淆。
    sweeps = []
    for kind in ('triangle', 'sine'):
        for value in (args.cutoffs if args.sweep == 'lowpass' else args.noise_ratios):
            sweeps.append((kind, value))

    for kind, value in sweeps:
        margins = []
        scores = []
        rejected = 0
        for trial in range(args.trials):
            kwargs = dict(seed=1000 + trial)
            if args.sweep == 'lowpass':
                kwargs.update(noise_vpp_ratio=0.0, cutoff_hz=value)
            else:
                kwargs.update(noise_vpp_ratio=value)
            samples = make_signal(kind, args.frequency, args.vpp, **kwargs)
            result = analyze.replay(library, samples, trial + 1)
            margins.append(result['model_margin_permille'])
            scores.append(result['best_score_permille'])
            if result['status'] != 'OK':
                rejected += 1
        report['points'].append({
            'kind': kind,
            'parameter': 'cutoff_hz' if args.sweep == 'lowpass' else 'noise_vpp_ratio',
            'value': value,
            'margin_min': min(margins),
            'margin_mean': round(sum(margins) / len(margins), 2),
            'margin_max': max(margins),
            'score_min': min(scores),
            'rejected_frames': rejected,
            'rejection_rate_pct': round(rejected / args.trials * 100, 1),
        })

    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.json:
        Path(args.json).write_text(payload, encoding='utf-8')
    print(payload)

    label = '截止频率' if args.sweep == 'lowpass' else '噪声峰峰比'
    print(f'\n结论摘要（{label} 扫描；门限 = '
          f'{MIN_MODEL_MARGIN_PERMILLE}/1000 千分比）：')
    for point in report['points']:
        shown = (f"{point['value']:>7.0f} Hz" if args.sweep == 'lowpass'
                 else f"{point['value'] * 100:>5.1f} %")
        print(f"  {point['kind']:<8} {label}={shown} "
              f"margin={point['margin_min']:>4}-{point['margin_max']:<4} "
              f"mean={point['margin_mean']:>5.2f} "
              f"拒识率={point['rejection_rate_pct']:>5.1f}%")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
