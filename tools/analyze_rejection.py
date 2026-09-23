"""拒识归因：对已记录的 4096 点帧做离线回放，补全串口未回传的判据。

为什么需要它：固件在 status != OK 时只回 status/type=unknown/削顶计数，
**不回传** best_score_permille、second_score_permille、model_margin_permille、
accepted_cycles。而 waveform_result_t 内部确实算了这些量。于是"为何拒识"
只能靠把同一帧数据在主机上重放一遍算法来回答。

回放对象是编译自工程内 waveform_analyzer.c 的主机共享库，因此与固件执行
同一份算法源码（同一 O2 语义），不是另写一套近似实现。若库缺少版本 2 入口
waveform_analyze_frame，本工具直接拒绝运行，避免拿旧算法的结论解释新固件。
"""

import argparse
import ctypes
import json
import math
import struct
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALGORITHM_VERSION_V2 = 0x00020100
SAMPLE_COUNT = 4096
SAMPLE_RATE_MILLIHZ = 20_000_000
REFERENCE_MICROVOLTS = 3_300_000
UNITY_GAIN_PPM = 1_000_000
FLAG_CONTIGUOUS = 1 << 0

STATUS_NAMES = [
    'OK', 'SIGNAL_TOO_SMALL', 'ADC_HIGH_CLIPPED', 'PERIOD_NOT_FOUND',
    'FREQUENCY_OUT_OF_RANGE', 'ADC_LOW_CLIPPED', 'ADC_SAMPLE_INVALID',
    'CLASSIFICATION_AMBIGUOUS', 'BAD_ARGUMENT', 'BAD_METADATA',
    'INSUFFICIENT_SAMPLES',
]
TYPE_NAMES = ['sine', 'square', 'triangle', 'sawtooth', 'unknown']

# 取自 waveform_analyzer.c 的硬门限，单位是千分比（permille）。
# 拒识条件：best_score < 76.0% 或 (best - second) < 0.5%。
MIN_MODEL_SCORE_PERMILLE = 760
MIN_MODEL_MARGIN_PERMILLE = 5
LOW_MODEL_MARGIN_PERMILLE = 15
MIN_VALID_CYCLES = 3
MIN_FREQUENCY_MILLIHZ = 98_000
MAX_FREQUENCY_MILLIHZ = 402_000

QUALITY_BITS = {
    1: 'CLOCK_UNCALIBRATED',
    2: 'VREF_UNCALIBRATED',
    4: 'FEW_CYCLES',
    8: 'LOW_MODEL_MARGIN',
}

# 每一条拒识状态对应的"下一步该看什么"。
GATE_ADVICE = {
    'SIGNAL_TOO_SMALL': '核对输入摆幅与直流偏置：摆幅必须显著超过算法幅值门限，'
                        '且信号全程落在 0~VDDA 内。',
    'ADC_HIGH_CLIPPED': '信号碰到 VDDA，属前端/偏置问题，不是算法问题；'
                        '降低幅度或调整偏置后重测。',
    'ADC_LOW_CLIPPED': '信号碰到地电位，同上。',
    'PERIOD_NOT_FOUND': '检查是否存在可靠基波：噪声占比过高、基频低于 100 Hz '
                        '或周期数不足都会导致此状态。',
    'FREQUENCY_OUT_OF_RANGE': '确认信号频率落在赛事区间 100~400 Hz。',
    'ADC_SAMPLE_INVALID': '帧内存在 >4095 的码值：注入数据或采集链路有缺陷。',
    'CLASSIFICATION_AMBIGUOUS': '最佳与次佳模板太接近。三角波与正弦波在高次'
                                '谐波被前端滤掉时本就难分；需检查前端带宽，'
                                '并确认是否超出"每类模板均可辨识"的假设。',
    'INSUFFICIENT_SAMPLES': '样本数不满足算法要求。',
    'BAD_ARGUMENT': '调用契约问题，查指针与帧结构。',
    'BAD_METADATA': '采样率/参考电压/增益元数据不合法。',
}


class Frame(ctypes.Structure):
    """与 waveform_frame_t 逐字段对应，顺序与对齐必须完全一致。"""

    _fields_ = [
        ('samples', ctypes.POINTER(ctypes.c_uint16)),
        ('sample_count', ctypes.c_uint16),
        ('adc_full_scale', ctypes.c_uint16),
        ('sample_rate_millihz', ctypes.c_uint32),
        ('adc_reference_microvolts', ctypes.c_uint32),
        ('frontend_gain_ppm', ctypes.c_uint32),
        ('sequence', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
    ]


class Result(ctypes.Structure):
    """与 waveform_result_t 逐字段对应。"""

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


def load_library(path):
    """加载主机算法库，并强制要求它提供版本 2 的 frame 入口。"""
    library = ctypes.CDLL(str(path))
    if not hasattr(library, 'waveform_analyze_frame'):
        raise RuntimeError(
            f'{path} 未导出 waveform_analyze_frame，属版本 1 旧库，'
            '不能用它解释版本 2 固件的拒识')
    library.waveform_analyze_frame.argtypes = [
        ctypes.POINTER(Frame), ctypes.POINTER(Result)]
    library.waveform_analyze_frame.restype = ctypes.c_bool
    return library


def describe_signal(samples):
    """给出与算法无关的原始统计，用于判断拒识是否属于采集侧问题。"""
    count = len(samples)
    low = min(samples)
    high = max(samples)
    mean = sum(samples) / count
    variance = sum((value - mean) ** 2 for value in samples) / count
    return {
        'samples': count,
        'min_code': low,
        'max_code': high,
        'mean_code': round(mean, 3),
        'std_code': round(math.sqrt(variance), 3),
        'pp_code': high - low,
        'pp_mv': round((high - low) * 3300.0 / 4095.0, 3),
        'at_zero': sum(1 for value in samples if value <= 0),
        'at_full_scale': sum(1 for value in samples if value >= 4095),
    }


def replay(library, samples, sequence):
    """用主机算法跑一帧，返回完整结果字典。"""
    buffer = (ctypes.c_uint16 * len(samples))(*samples)
    frame = Frame()
    frame.samples = buffer
    frame.sample_count = len(samples)
    frame.adc_full_scale = 4095
    frame.sample_rate_millihz = SAMPLE_RATE_MILLIHZ
    frame.adc_reference_microvolts = REFERENCE_MICROVOLTS
    frame.frontend_gain_ppm = UNITY_GAIN_PPM
    frame.sequence = sequence
    frame.flags = FLAG_CONTIGUOUS
    result = Result()
    ok = library.waveform_analyze_frame(ctypes.byref(frame), ctypes.byref(result))
    if result.algorithm_version != ALGORITHM_VERSION_V2:
        raise RuntimeError(
            f'算法版本 {result.algorithm_version:#x} != 0x{ALGORITHM_VERSION_V2:x}，'
            '主机库与固件不一致，拒绝给出归因')
    status = STATUS_NAMES[result.status] if 0 <= result.status < len(STATUS_NAMES) \
        else f'UNKNOWN_STATUS_{result.status}'
    score_headroom = result.best_score_permille - MIN_MODEL_SCORE_PERMILLE
    margin_headroom = result.model_margin_permille - MIN_MODEL_MARGIN_PERMILLE
    return {
        'returns_true': bool(ok),
        'status': status,
        'type': TYPE_NAMES[result.type] if 0 <= result.type < len(TYPE_NAMES) else 'invalid',
        'frequency_millihz': result.frequency_millihz,
        'vpp_microvolts': result.vpp_microvolts,
        'duty_ppm': result.duty_ppm,
        'best_score_permille': result.best_score_permille,
        'second_score_permille': result.second_score_permille,
        'model_margin_permille': result.model_margin_permille,
        'accepted_cycles': result.accepted_cycles,
        'high_clip_samples': result.high_clip_samples,
        'low_clip_samples': result.low_clip_samples,
        'invalid_samples': result.invalid_samples,
        'dc_offset_counts': result.dc_offset_counts,
        'period_q16_samples': result.period_q16_samples,
        'quality_flags': result.quality_flags,
        'quality_flag_names': [name for bit, name in QUALITY_BITS.items()
                               if result.quality_flags & bit],
        'algorithm_version': result.algorithm_version,
        # 到拒识门限还剩多少余量；负数表示本帧按门限应当拒识。
        'score_headroom_permille': score_headroom,
        'margin_headroom_permille': margin_headroom,
        'decided_by': ('score_gate' if score_headroom < 0 else
                       'margin_gate' if margin_headroom < 0 else 'accepted'),
    }


def assess_risk(entry):
    """把一帧归入拒识风险档位，用于在记录集里排序找出最脆弱的帧。"""
    replay_result = entry['replay']
    if replay_result['status'] != 'OK':
        return 'rejected'
    margin_headroom = replay_result['margin_headroom_permille']
    score_headroom = replay_result['score_headroom_permille']
    if margin_headroom <= 5 or score_headroom <= 20:
        return 'critical'
    if margin_headroom <= 15 or score_headroom <= 60:
        return 'near'
    return 'safe'


def read_frames(path):
    """读取 4096 个小端 uint16 的帧文件；长度不符即报错而非截断。"""
    data = Path(path).read_bytes()
    if len(data) != SAMPLE_COUNT * 2:
        raise ValueError(f'{path}: 期望 8192 字节，实际 {len(data)}')
    return list(struct.unpack('<4096H', data))


def last_rejected_lines(session_path):
    """从记录会话里取出"固件判为拒识"的帧，用于回放交叉验证。"""
    rejected = []
    for line in Path(session_path).read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        fields = record.get('fields')
        if fields and fields.get('status') != 'OK':
            rejected.append((record['index'], fields.get('status')))
    return rejected


def main():
    parser = argparse.ArgumentParser(description='拒识归因：离线回放记录的帧')
    parser.add_argument('--library',
                        default=str(ROOT / 'tools' / 'waveform_analyzer_host.dll'))
    parser.add_argument('--inputs', nargs='+', required=True,
                        help='帧文件或含 input_*.bin 的目录')
    parser.add_argument('--session', default=None,
                        help='可选：记录会话 session.jsonl，用于交叉核对')
    parser.add_argument('--json', default=None, help='结果 JSON 输出路径')
    args = parser.parse_args()

    files = []
    for item in args.inputs:
        path = Path(item)
        if path.is_dir():
            files.extend(sorted(path.glob('*.bin')))
        else:
            files.append(path)

    library = load_library(args.library)
    reports = []
    skipped = []
    for index, path in enumerate(files, start=1):
        if path.stat().st_size != SAMPLE_COUNT * 2:
            # 会话目录里还有 initial_rx.bin 等非帧文件，跳过而不中断整批分析。
            skipped.append({'file': str(path), 'size': path.stat().st_size})
            continue
        samples = read_frames(path)
        entry = {
            'file': str(path),
            'signal': describe_signal(samples),
            'replay': replay(library, samples, index),
        }
        status = entry['replay']['status']
        entry['gate'] = 'accepted' if status == 'OK' else status
        entry['advice'] = GATE_ADVICE.get(status, '')
        entry['risk'] = assess_risk(entry)
        reports.append(entry)

    risk_histogram = {}
    for entry in reports:
        risk_histogram[entry['risk']] = risk_histogram.get(entry['risk'], 0) + 1
    # 余量最小的帧最可能在真实前端的谐波衰减下越界，排在最前面。
    riskiest = sorted(
        (e for e in reports if e['replay']['status'] == 'OK'),
        key=lambda e: (e['replay']['margin_headroom_permille'],
                       e['replay']['score_headroom_permille']))[:8]

    summary = {
        'generated_local': datetime.now().isoformat(timespec='seconds'),
        'library': args.library,
        'frames': len(reports),
        'skipped_non_frame_files': skipped,
        'accepted': sum(1 for r in reports if r['replay']['status'] == 'OK'),
        'status_histogram': {},
        'rejection_gates': {},
        'risk_histogram': risk_histogram,
        'gates_in_use': {
            'MIN_MODEL_SCORE_PERMILLE': MIN_MODEL_SCORE_PERMILLE,
            'MIN_MODEL_MARGIN_PERMILLE': MIN_MODEL_MARGIN_PERMILLE,
            'LOW_MODEL_MARGIN_PERMILLE': LOW_MODEL_MARGIN_PERMILLE,
            'frequency_window_millihz': [MIN_FREQUENCY_MILLIHZ, MAX_FREQUENCY_MILLIHZ],
            'MIN_VALID_CYCLES': MIN_VALID_CYCLES,
        },
        'riskiest_frames': [
            {'file': Path(e['file']).name,
             'type': e['replay']['type'],
             'margin_permille': e['replay']['model_margin_permille'],
             'margin_headroom_permille': e['replay']['margin_headroom_permille'],
             'score_permille': e['replay']['best_score_permille']}
            for e in riskiest],
    }
    for entry in reports:
        status = entry['replay']['status']
        summary['status_histogram'][status] = summary['status_histogram'].get(status, 0) + 1
        if status != 'OK':
            summary['rejection_gates'][status] = summary['rejection_gates'].get(status, 0) + 1

    if args.session:
        summary['firmware_rejected_frames'] = last_rejected_lines(args.session)

    payload = {'summary': summary, 'frames': reports}
    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    for entry in reports[:5]:
        replay_result = entry['replay']
        print(f"\n{Path(entry['file']).name}: {replay_result['status']}"
              f" type={replay_result['type']}"
              f" best={replay_result['best_score_permille']}/1000"
              f" second={replay_result['second_score_permille']}/1000"
              f" margin={replay_result['model_margin_permille']}/1000"
              f" cycles={replay_result['accepted_cycles']}"
              f" vpp={entry['signal']['pp_mv']}mV"
              f" clip={replay_result['high_clip_samples']}+{replay_result['low_clip_samples']}")
        if entry['advice']:
            print(f"  归因: {entry['advice']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
