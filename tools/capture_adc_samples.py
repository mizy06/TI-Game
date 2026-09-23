"""接收 MCU 上行的 ADC 样点并落盘 —— 真正把"模拟量"存下来。

背景：`R` 触发只上行一行 RESULT，4096 个采样点留在 MCU 的 g_adc_samples 里，
串口上从来没有出现模拟数据本身。固件新增 `D` 命令后，本工具按下面的顺序
取回原始波形：

    R -> CAPTURING/PROCESSING/RESULT/READY     （采集并识别，得到结论）
    D -> SAMPLES,bytes=8192,fnv1a=...  + 8192B （把同这一帧的样点取回来）
       -> READY

这样每一帧的"结论"和"波形本身"成对落盘：结论来自固件，波形可离线回放，
于是"为什么被判拒识"才有可查的证据。

字节序为小端 uint16 x 4096，与 W 注入完全一致，因此存下来的 .bin 可以直接
喂给 tools/analyze_rejection.py 做离线归因。
"""

import argparse
import csv
import hashlib
import importlib.util
import json
import struct
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_COUNT = 4096
PAYLOAD_BYTES = SAMPLE_COUNT * 2


def load_recorder():
    """复用记录器的端口校验、事务执行与 FNV-1a 实现。"""
    spec = importlib.util.spec_from_file_location(
        'record_uart_waveforms', ROOT / 'tools' / 'record_uart_waveforms.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_exactly(port, count, deadline):
    """在期限内读满 count 个字节，超时返回已读到的部分。"""
    data = bytearray()
    while len(data) < count and time.monotonic() < deadline:
        chunk = port.read(count - len(data))
        if chunk:
            data.extend(chunk)
    return bytes(data)


def parse_dump_header(line):
    """解析 ``SAMPLES,bytes=8192,fnv1a=DECIMAL``；缺字段即报错。"""
    parts = line.split(',')
    if parts.pop(0) != 'SAMPLES':
        raise ValueError(f'不是样点回执行: {line!r}')
    fields = {}
    for part in parts:
        key, _sep, value = part.partition('=')
        if key in fields:
            raise ValueError(f'重复字段 {key}')
        fields[key] = value
    for key in ('bytes', 'fnv1a'):
        if key not in fields:
            raise ValueError(f'{line!r} 缺少 {key}')
    return int(fields['bytes']), int(fields['fnv1a'])


def main():
    import serial
    import serial.tools.list_ports

    parser = argparse.ArgumentParser(description='接收 MCU 上行的 ADC 样点')
    parser.add_argument('--port', default='COM15')
    parser.add_argument('--count', type=int, default=10, help='采集多少帧')
    parser.add_argument('--interval', type=float, default=0.3, help='帧间隔秒')
    parser.add_argument('--budget', type=float, default=10.0, help='单帧期限秒')
    parser.add_argument('--acquire-timeout', type=float, default=15.0)
    parser.add_argument('--label', default='', help='标定波形，仅写入元数据')
    parser.add_argument('--csv', action='store_true',
                        help='额外为每帧写一份 序号,ADC码值 的 CSV')
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
        return 2

    def reopen():
        """USB CDC 偶发失效（ClearCommError / PermissionError 13）后重开端口。

        本机 XDS110 会周期性地重新枚举，已持有的句柄会在下一次读写时抛
        SerialException（实测表现：第一帧成功、第二帧 readline 即失败）。
        采集是长流程，必须自愈，否则一次抖动就丢掉后面所有帧。
        """
        nonlocal port
        try:
            port.close()
        except Exception:
            pass
        last = None
        for attempt in range(1, 4):
            time.sleep(0.8 * attempt)
            try:
                fresh = serial.Serial(selected.device, 115200, timeout=0.1,
                                      write_timeout=2, rtscts=False,
                                      dsrdtr=False)
                fresh.dtr = False
                fresh.rts = False
                port = fresh
                print(f'  [端口恢复] {selected.device} 第 {attempt} 次重开成功',
                      flush=True)
                return True
            except serial.SerialException as error:
                last = error
        print(f'  [端口恢复] {selected.device} 重开失败：{last}', flush=True)
        return False

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    outdir = Path(args.output) if args.output else (
        ROOT / 'tests' / 'results' / f'adc_samples_{stamp}')
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
        'source': 'R + D（采集后立即导出样点）',
        'expected_waveform': args.label or None,
        'sample_count': SAMPLE_COUNT,
        'format': 'little-endian uint16 x 4096, header SAMPLES,bytes,fnv1a',
        'sample_rate_hz': 20000,
        'adc_full_scale': 4095,
        'receiver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'requires_firmware': 'empty.c 含 D=Dump samples 命令（2026-09-22 新增）',
    }
    firmware = ROOT / 'Debug' / 'TI-game.out'
    if firmware.is_file():
        metadata['firmware_sha256'] = hashlib.sha256(firmware.read_bytes()).hexdigest()
    (outdir / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')

    index_path = outdir / 'index.jsonl'
    saved = 0
    mismatches = 0
    errors = 0

    with index_path.open('w', encoding='utf-8') as index_file:
        index = 0
        port_retries = 0
        while index < args.count:
            index += 1
            entry = {'index': index,
                     'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            # 第一步：正常采集并取得结论，与记录器使用同一套事务逻辑。
            try:
                analysis = recorder.run_transaction(port, 'R', b'', args.budget)
            except serial.SerialException as error:
                port_retries += 1
                entry['serial_error'] = f'{type(error).__name__}: {error}'
                print(f'[{index:02d}] 串口句柄失效（{error}），尝试恢复…',
                      flush=True)
                if port_retries > 3 or not reopen():
                    index_file.write(json.dumps(entry, ensure_ascii=False) + '\n')
                    index_file.flush()
                    errors += 1
                    break
                index -= 1  # 本帧未取到，恢复后重试同一序号
                time.sleep(args.interval)
                continue
            port_retries = 0
            entry['result'] = analysis.get('fields')
            entry['reason'] = analysis.get('reason')
            entry['elapsed_s'] = analysis.get('elapsed_s')
            if 'protocol_error' in analysis:
                entry['protocol_error'] = analysis['protocol_error']
                index_file.write(json.dumps(entry, ensure_ascii=False) + '\n')
                index_file.flush()
                errors += 1
                print(f'[{index:02d}] 采集失败: {analysis["protocol_error"]}',
                      flush=True)
                if analysis['protocol_error'] in ('DEADLINE', 'NO_READY',
                                                  'UNEXPECTED_STAGE'):
                    break  # 串口已失步，继续注入只会污染证据
                time.sleep(args.interval)
                continue

            # 第二步：把这一帧的样点取回来。
            started = time.monotonic()
            deadline = started + args.budget
            dump_log = []
            port.write(b'D')
            port.flush()
            try:
                header = recorder.grab_line(port, deadline, dump_log, started)
                if header is None:
                    raise TimeoutError('未收到 SAMPLES 回执')
                if header.startswith('ERR_'):
                    raise RuntimeError(header)
                declared, checksum = parse_dump_header(header)
                if declared != PAYLOAD_BYTES:
                    raise ValueError(f'字节数异常: {declared}')
                payload = read_exactly(port, PAYLOAD_BYTES, deadline)
                if len(payload) != PAYLOAD_BYTES:
                    raise TimeoutError(f'只收到 {len(payload)}/{PAYLOAD_BYTES} 字节')
                actual = recorder.fnv_of(payload)
                terminal = recorder.grab_line(port, deadline, dump_log, started)
                if terminal != 'READY':
                    raise RuntimeError(f'缺少终态 READY，收到 {terminal!r}')
                entry['dump_log'] = dump_log
            except Exception as error:
                entry['dump_error'] = f'{type(error).__name__}: {error}'
                index_file.write(json.dumps(entry, ensure_ascii=False) + '\n')
                index_file.flush()
                errors += 1
                print(f'[{index:02d}] 样点导出失败: {entry["dump_error"]}',
                      flush=True)
                if isinstance(error, serial.SerialException):
                    port_retries += 1
                    if port_retries <= 3 and reopen():
                        index -= 1  # 端口恢复后重取同一帧
                        time.sleep(args.interval)
                        continue
                break

            frame_path = outdir / f'adc_{index:03d}.bin'
            frame_path.write_bytes(payload)
            samples = struct.unpack('<4096H', payload)
            entry.update({
                'bin': frame_path.name,
                'bin_sha256': hashlib.sha256(payload).hexdigest(),
                'fnv1a_declared': checksum,
                'fnv1a_actual': actual,
                'integrity_ok': checksum == actual,
                'dump_elapsed_s': round(time.monotonic() - started, 3),
                'min_code': min(samples),
                'max_code': max(samples),
                'pp_code': max(samples) - min(samples),
                'pp_mv': round((max(samples) - min(samples)) * 3300.0 / 4095.0, 3),
            })
            if checksum != actual:
                mismatches += 1
            if args.csv:
                csv_path = outdir / f'adc_{index:03d}.csv'
                with csv_path.open('w', newline='', encoding='utf-8') as handle:
                    writer = csv.writer(handle)
                    writer.writerow(['序号', 'ADC码值'])
                    writer.writerows((n, value) for n, value in enumerate(samples))
            saved += 1
            index_file.write(json.dumps(entry, ensure_ascii=False) + '\n')
            index_file.flush()
            status = (entry['result'] or {}).get('status', entry.get('reason', '-'))
            print(f'[{index:02d}] {status} | 样点 {len(payload)}B '
                  f'码值 {entry["min_code"]}-{entry["max_code"]} '
                  f'({entry["pp_mv"]} mVpp) | 校验'
                  f'{"一致" if entry["integrity_ok"] else "不一致"} '
                  f'| {entry["dump_elapsed_s"]}s', flush=True)
            time.sleep(args.interval)

    port.close()
    outcome = {
        'outdir': str(outdir),
        'frames_saved': saved,
        'integrity_mismatches': mismatches,
        'errors': errors,
    }
    (outdir / 'outcome.json').write_text(
        json.dumps(outcome, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    return 0 if saved and not mismatches and not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
