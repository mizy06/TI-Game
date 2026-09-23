"""参考数据自动轮询采集：切换信号源时无需人工分段。

背景：真实 ADC 采集（R）只能记录"识别结果"，而区分不同波形的参考数据必须按
波形分批。人工来回切换信号源时，最省事的做法是让采集端持续按轮采样，每轮用
**本轮识别出的主导类别**给这一轮打标签，集齐四类后提前停止。

只复用 record_uart_waveforms 里已经验证过的事务与解析逻辑，不另写一套串口
处理。整个运行只占用一次端口，避免轮与轮之间被其他软件抢走。

输出：
  <outdir>/ref_records.jsonl   逐帧完整记录（含原始 hex、解析字段、轮次标签）
  <outdir>/metadata.json       端口身份、参数、脚本与固件哈希
  <outdir>/rounds.json         每一轮的主导类别与统计
  --log 指定的 CSV             与既有日志同格式追加
"""

import argparse
import hashlib
import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WAVE_CLASSES = ('sine', 'square', 'triangle', 'sawtooth')


def load_recorder():
    """复用记录器的端口校验、事务执行与 CSV 列定义。"""
    spec = importlib.util.spec_from_file_location(
        'record_uart_waveforms', ROOT / 'tools' / 'record_uart_waveforms.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    import csv
    import serial
    import serial.tools.list_ports

    parser = argparse.ArgumentParser(description='参考数据自动轮询采集')
    parser.add_argument('--port', default='COM15')
    parser.add_argument('--rounds', type=int, default=8, help='最大轮数')
    parser.add_argument('--frames-per-round', type=int, default=12)
    parser.add_argument('--interval', type=float, default=0.3)
    parser.add_argument('--target-per-class', type=int, default=10,
                        help='每类集满多少帧后停止采该类')
    parser.add_argument('--stale-rounds', type=int, default=20,
                        help='同一类别已集满后，连续多少轮未变化即结束')
    parser.add_argument('--budget', type=float, default=10.0)
    parser.add_argument('--acquire-timeout', type=float, default=20.0)
    parser.add_argument('--log', default=str(ROOT / 'waveform_test_log.csv'))
    parser.add_argument('--output', default=None)
    args = parser.parse_args()

    recorder = load_recorder()

    matches = list(serial.tools.list_ports.comports())
    selected, message = recorder.verify_port(args.port, matches)
    if selected is None:
        print(f'[拒绝采集] {message}')
        return 2

    try:
        port = serial.Serial(selected.device, 115200, timeout=0.1,
                             write_timeout=2, rtscts=False, dsrdtr=False)
        port.dtr = False
        port.rts = False
    except serial.SerialException as error:
        print(f'[拒绝采集] 无法打开 {selected.device}：{error}')
        print('请先在串口助手中关闭该串口，确认“设备重新插入后自动重连”'
              '没有把端口抢回去。')
        return 2

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    outdir = Path(args.output) if args.output else (
        ROOT / 'tests' / 'results' / f'uart_ref_{stamp}')
    try:
        outdir.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        port.close()
        print(f'[拒绝采集] 无法创建目录 {outdir}：{error}')
        return 2

    metadata = {
        'started_local': datetime.now().isoformat(timespec='seconds'),
        'port': selected.device,
        'port_hwid': selected.hwid,
        'port_description': selected.description,
        'baud': 115200,
        'source': 'R',
        'mode': 'round-robin reference collection; round label = dominant '
                'recognised class',
        'max_rounds': args.rounds,
        'frames_per_round': args.frames_per_round,
        'target_per_class': args.target_per_class,
        'recorder_sha256': hashlib.sha256(
            (ROOT / 'tools' / 'record_uart_waveforms.py').read_bytes()).hexdigest(),
    }
    firmware = ROOT / 'Debug' / 'TI-game.out'
    if firmware.is_file():
        metadata['firmware_sha256'] = hashlib.sha256(firmware.read_bytes()).hexdigest()
    (outdir / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')

    csv_path = Path(args.log)
    new_csv = not csv_path.exists() or csv_path.stat().st_size == 0
    per_class = {name: 0 for name in WAVE_CLASSES}
    rounds_log = []
    frame_index = 0
    stale_rounds = 0

    with csv_path.open('a', newline='', encoding='utf-8-sig') as csv_file, \
            (outdir / 'ref_records.jsonl').open('w', encoding='utf-8') as report:
        writer = csv.writer(csv_file)
        if new_csv:
            writer.writerow(recorder.CSV_HEADER)

        for round_index in range(1, args.rounds + 1):
            records = []
            for _ in range(args.frames_per_round):
                frame_index += 1
                record = recorder.run_transaction(port, 'R', b'', args.budget)
                record['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                record['index'] = frame_index
                record['round'] = round_index
                records.append(record)
                time.sleep(args.interval)

            fields_list = [r.get('fields', {}) for r in records]
            tally = {}
            for fields in fields_list:
                if fields.get('status') == 'OK':
                    tally[fields['type']] = tally.get(fields['type'], 0) + 1
            label = max(tally, key=tally.get) if tally else None
            for record in records:
                record['round_label'] = label
                report.write(json.dumps(record, ensure_ascii=False) + '\n')
                fields = record.get('fields', {})
                writer.writerow([
                    record['timestamp'], frame_index,
                    fields.get('status', record.get('protocol_error', 'NO_RESULT')),
                    fields.get('type', 'NA'),
                    fields.get('freq_hz', ''), fields.get('vpp_v', ''),
                    fields.get('duty_pct', ''), fields.get('score_pct', ''),
                    fields.get('margin_pct', ''), fields.get('cycles', ''),
                ])
            csv_file.flush()
            report.flush()

            if label:
                # 若本类已集满目标帧数，说明信号源还没切换；连续 3 轮如此就
                # 停止，避免在同一个波形上无限堆积重复数据。
                if per_class[label] >= args.target_per_class:
                    stale_rounds += 1
                else:
                    stale_rounds = 0
                per_class[label] += tally[label]
            rounds_log.append({
                'round': round_index,
                'recognised': tally,
                'label': label,
                'ok_frames': sum(tally.values()),
            })
            print(f'轮 {round_index}: {tally or "无成功帧"} -> 标签 {label}；'
                  f'累计 { {k: v for k, v in per_class.items() if v} }', flush=True)

            if all(per_class[name] >= args.target_per_class for name in WAVE_CLASSES):
                print('四类均已达到目标帧数，提前结束。')
                break
            if stale_rounds >= args.stale_rounds:
                print(f'连续 {stale_rounds} 轮识别为同一类别且已集满目标，'
                      '判定信号源尚未切换，结束本次采集。')
                break

    (outdir / 'rounds.json').write_text(
        json.dumps({'rounds': rounds_log, 'per_class': per_class},
                   indent=2, ensure_ascii=False), encoding='utf-8')
    port.close()
    print(json.dumps({'outdir': str(outdir), 'per_class': per_class},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
