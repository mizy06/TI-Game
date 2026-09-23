"""ADC 样点取证：判断"信号本身是否合规"，并用谐波结构定位拒识根因。

为什么需要它：
  `R` 的 RESULT 行只在识别成功时输出 score/margin，拒识时不给。用 `D` 把
  样点取回来后，必须先区分两类失败：
    (a) 输入信号调理问题 —— 偏置错、削顶、幅度不足；
    (b) 真实波形与算法假设不符 —— 例如高次谐波被前端滤掉，三角波退化成
        近似正弦，于是"三角/正弦"模板相互竞争。
  两者的修法完全不同，不能混为一谈。

判据（理论谐波比，相对基波）：
  正弦      h2≈0    h3≈0    h5≈0
  三角波    h2≈0    h3=1/9  h5=1/25   （奇次、按 1/n^2 衰减）
  方波      h2≈0    h3=1/3  h5=1/5    （奇次、按 1/n 衰减）
  锯齿波    h2=1/2  h3=1/3  h5=1/5    （含全部次谐波）
  若测得 h3/h1 明显低于上表，即说明该次谐波在到达 ADC 之前已被衰减 ——
  这就是前端带宽不足的直接证据。

用法：
  python sample_forensics.py tests/results/adc_samples_xxx/adc_*.bin
  python sample_forensics.py tests/results/adc_samples_xxx/adc_*.bin --plot out.png
"""
from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

import numpy as np

FULL_SCALE = 4095
VREF_MV = 3300.0
SAMPLE_RATE = 20000.0
FREQ_WINDOW = (100.0, 400.0)

IDEAL = {
    'sine':     {2: 0.0, 3: 0.0, 5: 0.0},
    'triangle': {2: 0.0, 3: 1 / 9, 5: 1 / 25},
    'square':   {2: 0.0, 3: 1 / 3, 5: 1 / 5},
    'sawtooth': {2: 0.5, 3: 1 / 3, 5: 1 / 5},
}


def load(path: Path):
    data = path.read_bytes()
    if len(data) % 2:
        raise ValueError('字节数非偶数')
    return np.frombuffer(data, dtype='<u2').astype(np.float64)


def analyse(samples: np.ndarray):
    n = samples.size
    codes_to_mv = VREF_MV / FULL_SCALE
    lo, hi = float(samples.min()), float(samples.max())
    mean = float(samples.mean())
    at_zero = int(np.count_nonzero(samples <= 1))
    at_top = int(np.count_nonzero(samples >= FULL_SCALE - 1))

    centred = samples - mean
    # Hann 窗抑制泄漏，避免把基波能量摊到邻频而低估谐波
    window = np.hanning(n)
    spectrum = np.fft.rfft(centred * window)
    freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
    mag = np.abs(spectrum)

    lo_bin = int(np.searchsorted(freqs, FREQ_WINDOW[0]))
    hi_bin = int(np.searchsorted(freqs, FREQ_WINDOW[1]))
    if hi_bin <= lo_bin:
        return None
    f0_index = lo_bin + int(np.argmax(mag[lo_bin:hi_bin]))
    f0 = float(freqs[f0_index])

    def harmonic(nth):
        """取第 nth 次谐波附近谱峰，容忍基频估计的小误差。"""
        target = f0 * nth
        if target >= SAMPLE_RATE / 2:
            return 0.0
        centre = int(round(target / (SAMPLE_RATE / n)))
        span = max(2, int(round(f0 / (SAMPLE_RATE / n) * 0.25)))
        a, b = max(0, centre - span), min(mag.size, centre + span + 1)
        return float(mag[a:b].max()) if b > a else 0.0

    h1 = harmonic(1)
    ratios = {}
    for nth in (2, 3, 5):
        ratios[nth] = (harmonic(nth) / h1) if h1 else 0.0

    # 用最短距离把测得谐波结构与四种理想波形对比
    best, second = None, None
    distance = {}
    for name, ideal in IDEAL.items():
        d = sum((ratios[k] - ideal[k]) ** 2 for k in (2, 3, 5)) ** 0.5
        distance[name] = round(d, 4)
    order = sorted(distance.items(), key=lambda kv: kv[1])
    best, second = order[0], order[1]

    pp_mv = (hi - lo) * codes_to_mv
    dc_mv = mean * codes_to_mv

    notes = []
    if (at_zero + at_top) / n >= 0.03:
        notes.append(f"削顶 {(at_zero + at_top) / n * 100:.1f}%")
    if pp_mv < 200:
        notes.append(f"幅度过小 {pp_mv:.0f}mVpp")
    if abs(dc_mv - VREF_MV / 2) > 500:
        notes.append(f"偏置 {dc_mv:.0f}mV 偏离中点 {VREF_MV / 2:.0f}mV")
    if not FREQ_WINDOW[0] <= f0 <= FREQ_WINDOW[1]:
        notes.append(f"基频 {f0:.1f}Hz 出窗 {FREQ_WINDOW}")
    # 谐波比低于理想值一半即判定被前端衰减
    attenuation = {}
    for k in (3, 5):
        if ratios[k] < best_ideal(k, best[0]) * 0.5 and best_ideal(k, best[0]) > 0.02:
            attenuation[k] = round(ratios[k] / best_ideal(k, best[0]), 3)
    if attenuation:
        notes.append('谐波被前端衰减 ' + json.dumps(attenuation, ensure_ascii=False))

    return {
        'n': n, 'min': lo, 'max': hi, 'mean_code': round(mean, 1),
        'pp_mv': round(pp_mv, 1), 'dc_mv': round(dc_mv, 1),
        'at_zero': at_zero, 'at_top': at_top,
        'f0_hz': round(f0, 2),
        'h2_h1': round(ratios[2], 4),
        'h3_h1': round(ratios[3], 4),
        'h5_h1': round(ratios[5], 4),
        'shape_best': best[0], 'shape_best_dist': best[1],
        'shape_second': second[0], 'shape_second_dist': second[1],
        'shape_distance': distance,
        'notes': notes,
    }


def best_ideal(nth: int, name: str) -> float:
    return IDEAL[name][nth]


def render(paths, out_png: Path, limit=4):
    """把若干帧画成时域波形 + 归一化频谱，便于肉眼判断形状是否失真。"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    paths = paths[:limit]
    fig, axes = plt.subplots(len(paths), 2, figsize=(13, 2.6 * len(paths)))
    if len(paths) == 1:
        axes = np.array([axes])
    for row, raw in enumerate(paths):
        samples = load(Path(raw))
        n = samples.size
        t_ms = np.arange(n) / SAMPLE_RATE * 1000.0
        mv = samples * (VREF_MV / FULL_SCALE)
        ax = axes[row][0]
        ax.plot(t_ms, mv, lw=0.7, color='#1f4e9c')
        ax.axhline(VREF_MV / 2, color='#999999', ls='--', lw=0.7)
        ax.set_ylabel('mV')
        ax.set_title(f'{Path(raw).name}  时域（虚线=中点 1650 mV）', fontsize=9)
        ax.grid(alpha=0.25)
        if row == len(paths) - 1:
            ax.set_xlabel('时间 (ms)')

        centred = samples - samples.mean()
        mag = np.abs(np.fft.rfft(centred * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)
        mag = mag / (mag.max() or 1)
        keep = freqs <= 1200
        ax2 = axes[row][1]
        ax2.plot(freqs[keep], mag[keep], lw=0.8, color='#9c1f1f')
        for nth, colour in ((1, '#1f4e9c'), (2, '#c07a00'), (3, '#116b41'),
                            (5, '#6a1b9a')):
            if freqs[keep].size:
                fx = None
                if nth == 1:
                    lo_bin = int(np.searchsorted(freqs, FREQ_WINDOW[0]))
                    hi_bin = int(np.searchsorted(freqs, FREQ_WINDOW[1]))
                    fx = freqs[lo_bin + int(np.argmax(mag[lo_bin:hi_bin]))]
                else:
                    st = analyse(samples)
                    fx = st['f0_hz'] * nth if st else None
                if fx:
                    ax2.axvline(fx, color=colour, ls=':', lw=0.9)
        ax2.set_title(f'{Path(raw).name}  归一化频谱（虚线=h1/h2/h3/h5）',
                      fontsize=9)
        ax2.set_ylabel('|X| / max')
        ax2.grid(alpha=0.25)
        if row == len(paths) - 1:
            ax2.set_xlabel('频率 (Hz)')
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('bins', nargs='+')
    ap.add_argument('--plot', default=None, help='输出 PNG 供肉眼复核')
    ap.add_argument('--json', default=None, help='结构化结果输出路径')
    args = ap.parse_args()

    results = []
    head = (f"{'文件':<12} {'pp(mV)':>8} {'dc(mV)':>7} {'削顶%':>6} {'f0(Hz)':>8} "
            f"{'h2/h1':>7} {'h3/h1':>7} {'h5/h1':>7} {'最像':<9} 说明")
    print(head)
    print('-' * len(head))
    for raw in args.bins:
        path = Path(raw)
        try:
            st = analyse(load(path))
        except Exception as error:
            print(f'{path.name:<12} 读取失败: {error}')
            continue
        results.append({'file': path.name, **st})
        print(f"{path.name:<12} {st['pp_mv']:>8.1f} {st['dc_mv']:>7.1f} "
              f"{(st['at_zero'] + st['at_top']) / st['n'] * 100:>6.2f} "
              f"{st['f0_hz']:>8.2f} {st['h2_h1']:>7.3f} {st['h3_h1']:>7.3f} "
              f"{st['h5_h1']:>7.3f} {st['shape_best']:<9} "
              f"{'；'.join(st['notes']) or '正常'}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f'\n[json] {args.json}')
    if args.plot:
        render(args.bins, Path(args.plot))
        print(f'[plot] {args.plot}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
