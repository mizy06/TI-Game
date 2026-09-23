"""UART 测试波数据记录器 —— 面向"为何拒识"的可追溯取证。

设计目标（与既有 tools/test_serial_waveform.py 的区别）：
1. 拒识帧不能被丢弃。固件在 status != OK 时仍会回一行 RESULT，本工具把它
   连同原始十六进制字节一起保存，而不是解析失败就静默跳过。
2. 逐行保留原始字节。每条收发行都记录单调时钟偏移、原始 hex 和解码文本，
   便于区分"没触发""处理中超时""RESULT 畸形"等不同故障。
3. 显式校验端口身份。只接受 XDS110 Class Application/User UART；端口不存在
   或被占用时直接失败，**不**回退到系统中任意一个串口。
4. 会话自包含。每个会话写入独立目录，含 metadata.json、session.jsonl、
   initial_rx.bin、w_xxx_input.bin，并追加/R 结果写入项目既有波形日志。

重要事实（决定了后续分析的可行边界）：
- 固件失败分支只打印 status/type=unknown/high_clip_samples/low_clip_samples/
  invalid_samples/sequence/quality_flags，**不含 score_pct、margin_pct、
  cycles**。而 waveform_result_t 内部其实是算出了这些量的。因此"为何拒识"
  不能只看 UART 文本，必须依赖本工具保存的原始 4096 点帧做离线回放。
- R（真实 ADC）触发不回传采样点，4096 点只存在于 MCU 内部。故 source=R 的
  记录只有可观测的外部字段；要取得样点需固件增加导出命令（见会话 README）。
"""

import argparse
import hashlib
import json
import struct
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与固件 waveform_status_t / waveform_status_name() 一一对应。
STATUS_MEANING = {
    'OK': ('识别成功', 'accepted'),
    'SIGNAL_TOO_SMALL': ('有效摆幅太小，未超过幅值门限', 'amplitude_gate'),
    'ADC_HIGH_CLIPPED': ('靠近 ADC 满量程的采样点过多（高电平饱和）', 'saturation_gate'),
    'ADC_LOW_CLIPPED': ('靠近 ADC 零端的采样点过多（低电平饱和）', 'saturation_gate'),
    'PERIOD_NOT_FOUND': ('未找到可靠周期', 'period_gate'),
    'FREQUENCY_OUT_OF_RANGE': ('频率不在 100~400 Hz 允许区间', 'frequency_gate'),
    'ADC_SAMPLE_INVALID': ('存在大于 adc_full_scale 的非法码', 'data_validity'),
    'CLASSIFICATION_AMBIGUOUS': ('最佳与次佳模板区分度不足（真·拒识）', 'model_margin_gate'),
    'INSUFFICIENT_SAMPLES': ('样本数不满足算法要求', 'sample_count_gate'),
    'BAD_ARGUMENT': ('指针类入参错误', 'api_contract'),
    'BAD_METADATA': ('采样率/参考电压/增益等元数据错误', 'api_contract'),
}

# 协议层失败（不是算法拒识），单独归类，避免与算法结论混淆。
PROTOCOL_MEANING = {
    'ERR_TIMEOUT': '固件 2.5 s 内未收满 8192 字节，注入帧不完整',
    'ERR_SAMPLE_RANGE': '收到的码值超过 4095，固件拒绝分析',
    'HANDSHAKE_FAILED': '未收到 READY_FOR_DATA，按约定不得发送二进制',
    'NO_RESULT': '读到 READY 但没有 RESULT 行',
    'NO_READY': '拿到 RESULT 后没有终态 READY',
    'DEADLINE': '单次事务超过 10 s 期限',
    'PORT_UNAVAILABLE': '串口不存在或被其他程序占用',
}

CSV_HEADER = ['时间戳', '序号', '状态', '波形类型', '频率(Hz)', '峰峰值(V)',
              '方波占空比(%)', '匹配得分(%)', '分类余量(%)', '有效周期数']

# 只匹配固件成功行；失败行的字段集合不同，由 parse_failure 单独处理。
RESULT_PREFIX = 'RESULT,'


def parse_kv(line):
    """把 ``RESULT,k=v,k=v`` 解析为字典；重复键抛异常而不是后写覆盖前写。"""
    parts = line.split(',')
    if parts.pop(0) != 'RESULT':
        raise ValueError('not a RESULT line')
    fields = {}
    for part in parts:
        key, _sep, value = part.partition('=')
        if key in fields:
            raise ValueError(f'duplicate field {key}')
        fields[key] = value
    return fields


def classify(fields):
    """由 RESULT 字段得到 (归因类别, 人可读原因)。"""
    status = fields.get('status', 'UNKNOWN')
    if status in STATUS_MEANING:
        return STATUS_MEANING[status]
    return (f'固件报告了未在头文件中登记的状态 {status}', 'unknown_status')


def grab_line(port, deadline, log, started=None):
    """在绝对期限前读取一行 ASCII，记录原始字节与相对事务起点的时间。

    ``t_s`` 是相对 ``started`` 的耗时（秒，单调时钟），便于看出握手、处理、
    结果各阶段各占多久；``t_mono`` 是绝对单调时钟值，便于跨行排序。
    超期返回 None。空行返回 ''（调用方自行跳过）。
    """
    now = time.monotonic()
    while now < deadline:
        raw = port.readline()
        now = time.monotonic()
        if not raw:
            continue
        text = raw.decode('ascii', errors='replace').strip()
        log.append({
            't_s': round(now - (started if started is not None else deadline), 6),
            't_mono': round(now, 6),
            'raw_hex': raw.hex(),
            'text': text,
        })
        return text
    return None


def run_transaction(port, source, payload, budget_s=10.0):
    """执行一次完整事务，返回记录字典。

    事务按触发源分派：R/CAPTURING、W/READY_FOR_DATA+8192B+DATA 回执、
    T/GENERATING。任何一步不合规都终止并如实记录，不重试。
    """
    started = time.monotonic()
    deadline = started + budget_s
    log = []
    record = {'source': source, 'log': log}

    def remaining():
        return deadline - time.monotonic()

    command = 'RWT'[{'R': 0, 'W': 1, 'T': 2}[source]]
    if port.write(command.encode('ascii')) != 1:
        record['protocol_error'] = 'COMMAND_WRITE_SHORT'
        return record
    port.flush()

    first = grab_line(port, deadline, log, started)
    if first is None:
        record['protocol_error'] = 'DEADLINE'
        return record

    if source == 'W':
        if first != 'READY_FOR_DATA':
            record['protocol_error'] = 'HANDSHAKE_FAILED'
            record['protocol_detail'] = first
            return record
        if len(payload) != 8192:
            record['protocol_error'] = 'PAYLOAD_SIZE'
            return record
        for offset in range(0, len(payload), 256):
            block = payload[offset:offset + 256]
            if port.write(block) != len(block):
                record['protocol_error'] = 'PAYLOAD_WRITE_SHORT'
                return record
        port.flush()
        receipt = grab_line(port, deadline, log, started)
        record['data_receipt'] = receipt
        if receipt is None:
            record['protocol_error'] = 'DEADLINE'
            return record
        if receipt.startswith('ERR_'):
            record['protocol_error'] = receipt.split(',')[0]
            return record
        expected = f'DATA,bytes={len(payload)},fnv1a={fnv_of(payload)}'
        if receipt != expected:
            record['protocol_error'] = 'DATA_INTEGRITY'
            record['protocol_detail'] = {'got': receipt, 'expected': expected}
            return record
        stage_line = grab_line(port, deadline, log, started)
    else:
        # R -> CAPTURING，T -> GENERATING，随后都紧跟 PROCESSING。
        stage_line = first if first == 'PROCESSING' else grab_line(port, deadline, log, started)
        record['stage_line'] = first if first != 'PROCESSING' else None

    if stage_line is None:
        record['protocol_error'] = 'DEADLINE'
        return record
    if stage_line.startswith('ERR_'):
        record['protocol_error'] = stage_line.split(',')[0]
        return record
    if stage_line != 'PROCESSING':
        record['protocol_error'] = 'UNEXPECTED_STAGE'
        record['protocol_detail'] = stage_line
        return record

    result_line = grab_line(port, deadline, log, started)
    if result_line is None:
        record['protocol_error'] = 'DEADLINE'
        return record
    record['result_line'] = result_line
    if not result_line.startswith(RESULT_PREFIX):
        record['protocol_error'] = 'MALFORMED_RESULT'
        return record

    try:
        fields = parse_kv(result_line)
    except ValueError as error:
        record['protocol_error'] = 'UNPARSABLE_RESULT'
        record['protocol_detail'] = str(error)
        return record
    # 没有 status 的 "RESULT,..." 不是本协议的结果行，必须当作畸形处理，
    # 否则一个拼错的键会被误读成 unknown 状态而掩盖真实故障。
    if 'status' not in fields:
        record['protocol_error'] = 'MALFORMED_RESULT'
        record['protocol_detail'] = 'missing status field'
        return record
    record['fields'] = fields
    reason, family = classify(fields)
    record['reason'] = reason
    record['reason_family'] = family
    record['accepted'] = fields.get('status') == 'OK'
    # 固件只在成功行里给出这些诊断量；拒识帧必然缺席，需靠离线回放补齐。
    record['diagnostics_omitted_by_firmware'] = any(
        key not in fields for key in ('score_pct', 'margin_pct', 'cycles'))

    terminal = grab_line(port, deadline, log, started)
    if terminal is None:
        record['protocol_error'] = 'DEADLINE'
    elif terminal != 'READY':
        record['protocol_error'] = 'NO_READY'
        record['protocol_detail'] = terminal
    record['elapsed_s'] = time.monotonic() - started
    return record


def fnv_of(payload):
    """与固件 receive_uart_samples() 相同的 FNV-1a-32 回执校验。"""
    checksum = 2166136261
    for byte in payload:
        checksum = ((checksum ^ byte) * 16777619) & 0xFFFFFFFF
    return checksum


def verify_port(requested, ports):
    """只接受唯一匹配的 XDS110 Application/User UART，否则给出明确原因。"""
    if not ports:
        return None, '系统未枚举到任何串口'
    matches = [p for p in ports if p.device.upper() == requested.upper()]
    if not matches:
        listing = ', '.join(f'{p.device}({p.description})' for p in ports)
        return None, f'指定端口 {requested} 不存在；当前枚举到: {listing}'
    port = matches[0]
    description = port.description or ''
    if 'XDS110' not in description or 'Application' not in description:
        return None, f'{requested} 不是 XDS110 Application/User UART：{description}'
    return port, 'ok'


def load_injection_cases():
    """复用已验证的注入用例与波形生成器，避免第二套实现产生偏差。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'audited_serial_test', ROOT / 'tools' / 'audited_serial_test.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    import serial
    import serial.tools.list_ports

    parser = argparse.ArgumentParser(
        description='记录 UART 测试波数据，专供后续拒识归因')
    parser.add_argument('--port', default='COM15',
                        help='XDS110 Application/User UART，默认 COM15')
    parser.add_argument('--source', choices=['R', 'W', 'T'], default='R',
                        help='R=真实ADC采集(默认), W=数字注入, T=片上自测')
    parser.add_argument('--count', type=int, default=10, help='记录帧数')
    parser.add_argument('--interval', type=float, default=0.3, help='帧间隔秒')
    parser.add_argument('--output', default=None, help='会话目录；默认自动命名')
    parser.add_argument('--log', default=str(ROOT / 'waveform_test_log.csv'),
                        help='追加写入的波形日志 CSV')
    parser.add_argument('--budget', type=float, default=10.0,
                        help='单帧事务期限秒，初赛限值为 10 s')
    parser.add_argument('--label', default='',
                        help='本批参考数据的标定波形（sine/square/triangle/'
                             'sawtooth），仅写入会话元数据与逐帧记录，'
                             '不改变与既有工具共享的 CSV 列定义')
    parser.add_argument('--acquire-timeout', type=float, default=30.0,
                        help='抢同一个串口的等待上限秒；只在同一端口上重试')
    args = parser.parse_args()

    matches = list(serial.tools.list_ports.comports())
    selected, message = verify_port(args.port, matches)
    if selected is None:
        print(f'[拒绝记录] {message}')
        return 2

    # 先抢占端口，再建会话目录。这样端口被占用时不会留下一个空目录，避免
    # 事后把"没采到数据"误当成"采到了空数据"。
    #
    # 只在**同一个已校验端口**上重试：串口助手一类软件会在设备重新枚举后
    # 自动重连，把端口瞬间抢回，因此需要一个短窗口反复尝试。这里绝不切换到
    # 其他端口，保持"端口身份唯一"的取证前提。
    port_handle = None
    attempts = 0
    deadline = time.monotonic() + args.acquire_timeout
    last_error = None
    while True:
        attempts += 1
        try:
            port_handle = serial.Serial(selected.device, 115200, timeout=0.1,
                                        write_timeout=2, rtscts=False,
                                        dsrdtr=False)
            break
        except serial.SerialException as error:
            last_error = error
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    if port_handle is None:
        # 端口被占用是本机最常见的中断原因（串口调试助手/IDE 终端未释放），
        # 必须在消耗一次探针之前就说清楚，而不是抛裸栈。
        print(f'[拒绝记录] 在 {args.acquire_timeout:.0f} s 内 {attempts} 次尝试后'
              f'仍无法打开 {selected.device}：{last_error}')
        print('原因通常是该端口已被其他程序独占，或开发板正在重新枚举。请：')
        print('  1. 在串口调试助手 SerialDebug 中关闭串口，并检查它是否开启了'
              '“设备重新插入后自动重连”')
        print('  2. 断开 CCS / IDE 的串口终端')
        print('  3. 结束任何仍在运行的旧测试脚本')
        print(f'确认释放后重试：--port {selected.device} --source {args.source}')
        return 2

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    session = Path(args.output) if args.output else (
        ROOT / 'tests' / 'results' / f'uart_wave_{stamp}')
    try:
        # 已存在目录不允许复用，防止两次测试的证据混在一起。
        session.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        # 这里失败就必须立刻归还端口，否则一次误用参数会把串口占到进程退出。
        port_handle.close()
        print(f'[拒绝记录] 无法创建会话目录 {session}：{error}')
        return 2

    injection = load_injection_cases() if args.source == 'W' else None
    cases = injection.make_cases() if injection else []

    metadata = {
        'started_local': datetime.now().isoformat(timespec='seconds'),
        'port': selected.device,
        'port_hwid': selected.hwid,
        'port_description': selected.description,
        'baud': 115200,
        'port_acquire_attempts': attempts,
        'source': args.source,
        'expected_waveform': args.label or None,
        'planned_frames': args.count,
        'frame_budget_s': args.budget,
        'sample_rate_hz': 20000,
        'sample_count': 4096,
        'adc_full_scale': 4095,
        'reference_uv': 3300000,
        'note_failure_path': ('固件拒识行不含 score_pct/margin_pct/cycles；'
                              '需用 tools/analyze_rejection.py 离线回放补全'),
        'note_R_no_samples': ('R 触发不回传采样点，样点仅存在于 MCU 内部，'
                              '因此 source=R 的记录无法离线回放波形'),
        'recorder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    firmware = ROOT / 'Debug' / 'TI-game.out'
    if firmware.is_file():
        metadata['firmware_sha256'] = hashlib.sha256(firmware.read_bytes()).hexdigest()
    (session / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')

    records_path = session / 'session.jsonl'
    csv_path = Path(args.log)
    new_csv = not csv_path.exists() or csv_path.stat().st_size == 0

    accepted = 0
    rejected = 0
    protocol_failures = 0
    with csv_path.open('a', newline='', encoding='utf-8-sig') as csv_file:
        import csv
        writer = csv.writer(csv_file)
        if new_csv:
            writer.writerow(CSV_HEADER)

        with port_handle as port:
            # 把 DTR/RTS 拉到无效电平，避免打开端口时复位目标板。
            port.dtr = False
            port.rts = False
            initial = bytearray()
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                initial.extend(port.read(port.in_waiting or 1))
            (session / 'initial_rx.bin').write_bytes(initial)

            with records_path.open('w', encoding='utf-8') as report:
                for index in range(1, args.count + 1):
                    payload = b''
                    if args.source == 'W':
                        case = cases[(index - 1) % len(cases)]
                        payload = injection.generate(case, 20260922 + index)
                        (session / f'w_{index:03d}_input.bin').write_bytes(payload)
                    elif args.source == 'T':
                        # 片上自测用例由固件内部固定循环推进，这里只记序号。
                        payload = b''

                    record = run_transaction(port, args.source, payload, args.budget)
                    record['index'] = index
                    record['timestamp'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    # 标定波形只进会话记录，让参考数据可按批分离；
                    # CSV 列保持不变，以便与既有日志共用一份文件。
                    if args.label:
                        record['expected_waveform'] = args.label
                    if payload:
                        record['input_sha256'] = hashlib.sha256(payload).hexdigest()
                    if 'protocol_error' in record:
                        protocol_failures += 1
                    elif record.get('accepted'):
                        accepted += 1
                    else:
                        rejected += 1

                    fields = record.get('fields', {})
                    writer.writerow([
                        record['timestamp'], index,
                        fields.get('status', record.get('protocol_error', 'NO_RESULT')),
                        fields.get('type', 'NA'),
                        fields.get('freq_hz', ''), fields.get('vpp_v', ''),
                        fields.get('duty_pct', ''), fields.get('score_pct', ''),
                        fields.get('margin_pct', ''), fields.get('cycles', ''),
                    ])
                    csv_file.flush()
                    report.write(json.dumps(record, ensure_ascii=False) + '\n')
                    report.flush()

                    summary = fields.get('status') or record.get('protocol_error')
                    print(f'[{index:02d}] {summary} | {record.get("reason", "-")}'
                          f' | {record.get("elapsed_s", 0):.3f}s', flush=True)
                    time.sleep(args.interval)

    outcome = {
        'session': str(session),
        'recorded_frames': args.count,
        'accepted': accepted,
        'rejected': rejected,
        'protocol_failures': protocol_failures,
        'csv_log': str(csv_path),
    }
    (session / 'outcome.json').write_text(
        json.dumps(outcome, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(outcome, indent=2, ensure_ascii=False))
    # 0=全部识别成功，1=有算法拒识，2=有协议层失败（证据不可比）。
    return 0 if rejected == 0 and protocol_failures == 0 else (
        2 if protocol_failures else 1)


if __name__ == '__main__':
    raise SystemExit(main())
