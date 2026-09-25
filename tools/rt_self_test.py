"""驱动固件 `RT4` 级联自激环路，并汇总一套 296 题矩阵。

背景：`RT` 让板子自己用 DAC（PA15）产生已知波形，经杜邦线回到 ADC
输入（PA25），走完整的采集 + 识别链路，最后把"标称值"和"测量值"一起
上报。因此不需要信号发生器，也能验证"模拟输出 -> 引脚 -> 连线 -> ADC
-> 算法 -> 串口"整条通路。

固件侧协议（一条 `RT1`～`RT4` 命令触发一整轮）：

    RT-START,algorithm=2,name=global_harmonic_model,cases=296,...,dump=0
    RT-CASE,index=1/296,group=custom,profile=boundary,type=sine,...
    CAPTURING
    PROCESSING
    RESULT,status=OK,type=sine,...              （与 R 路径完全同格式）
    RT-CHECK,verdict=PASS,type_ok=1,...          （标称值 vs 测量值）
    ... 共 cases 条 ...
    RT-DONE,algorithm=2,name=global_harmonic_model,pass=...,fail=...,...
    READY

296 题默认只回传文本，普通串口终端可以直接查看；需要原始样点时中止矩阵
后发送 ``D``，导出最近一帧。
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
EXPECTED_CASES = 296
EXPECTED_CUSTOM_CASES = 200
EXPECTED_CHALLENGE_CASES = 96


def command_for_algorithm(algorithm):
    """把命令行选择映射成固件命令。"""
    if algorithm not in ('1', '2', '3', '4'):
        raise ValueError(f'不支持的算法: {algorithm!r}')
    return f'RT{algorithm}'


def load_recorder():
    """复用记录器的端口身份校验、逐行读取与 FNV-1a 实现。"""
    spec = importlib.util.spec_from_file_location(
        'record_uart_waveforms', ROOT / 'tools' / 'record_uart_waveforms.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_exactly(port, count, deadline):
    """在期限内读满 count 个字节；超时返回已经读到的部分。"""
    data = bytearray()
    while len(data) < count and time.monotonic() < deadline:
        chunk = port.read(count - len(data))
        if chunk:
            data.extend(chunk)
    return bytes(data)


def parse_kv(prefix, line):
    """把 ``PREFIX,k=v,k=v`` 解析成字典；重复键报错而不是静默覆盖。"""
    parts = line.split(',')
    if parts.pop(0) != prefix:
        raise ValueError(f'不是 {prefix} 行: {line!r}')
    fields = {}
    for part in parts:
        key, _sep, value = part.partition('=')
        if key in fields:
            raise ValueError(f'重复字段 {key}')
        fields[key] = value
    return fields


def as_int(fields, key, default=None):
    """把字段转成 int；缺失时返回 default，便于容忍协议增字段。"""
    if key not in fields:
        return default
    if fields[key] == 'NA':
        return default
    return int(fields[key])


def parse_case_index(fields):
    """解析 index=当前用例/用例总数，并兼容纯整数格式。"""
    value = fields.get('index')
    if value is None:
        return None
    index_text, separator, count_text = value.partition('/')
    index = int(index_text)
    if separator:
        count = int(count_text)
        if index < 1 or index > count:
            raise ValueError(f'RT 用例序号越界: {value!r}')
    return index


def validate_matrix_contract(start, cases, summary, abort,
                             expected_algorithm=None):
    """拒绝旧 12 题固件、截断会话和分组数量错误的伪成功。"""
    if abort is not None:
        return None
    if start is None:
        return 'MISSING_RT_START'
    try:
        declared = (
            int(start['cases']), int(start['custom']), int(start['challenge']))
    except (KeyError, TypeError, ValueError):
        return 'BAD_RT_START_COUNTS'
    expected = (
        EXPECTED_CASES, EXPECTED_CUSTOM_CASES, EXPECTED_CHALLENGE_CASES)
    if declared != expected:
        return f'WRONG_MATRIX{declared}'
    if (expected_algorithm is not None and
            start.get('algorithm') != str(expected_algorithm)):
        return f'WRONG_ALGORITHM({start.get("algorithm")})'
    if len(cases) != EXPECTED_CASES:
        return f'CASE_COUNT({len(cases)}/{EXPECTED_CASES})'
    custom = sum(record.get('group') == 'custom' for record in cases)
    challenge = sum(record.get('group') == 'challenge' for record in cases)
    if (custom, challenge) != (
            EXPECTED_CUSTOM_CASES, EXPECTED_CHALLENGE_CASES):
        return f'GROUP_COUNT(custom={custom},challenge={challenge})'
    if summary is None:
        return 'MISSING_RT_DONE'
    try:
        total = int(summary['pass']) + int(summary['fail'])
        custom_total = int(summary['custom_pass']) + int(summary['custom_fail'])
        challenge_total = (
            int(summary['challenge_pass']) + int(summary['challenge_fail']))
    except (KeyError, TypeError, ValueError):
        return 'BAD_RT_DONE_COUNTS'
    if (total, custom_total, challenge_total) != expected:
        return ('BAD_RT_DONE_TOTALS'
                f'(all={total},custom={custom_total},challenge={challenge_total})')
    return None


def main():
    import serial
    import serial.tools.list_ports

    parser = argparse.ArgumentParser(
        description='跑一遍固件 RT 自激矩阵并汇总通过情况')
    parser.add_argument('--port', default='COM15',
                        help='XDS110 Application/User UART，默认 COM15')
    parser.add_argument('--timeout', type=float, default=4500.0,
                        help='整轮 296 题矩阵的期限秒，默认 4500')
    parser.add_argument('--acquire-timeout', type=float, default=15.0,
                        help='抢占同一串口的等待上限秒')
    parser.add_argument('--output', default=None, help='会话目录；默认自动命名')
    parser.add_argument('--algorithm', choices=('1', '2', '3', '4'), default='4',
                        help='1=legacy_amdf_crossing，2=global_harmonic_model，3=fast_phase_model，4=三级级联（默认）')
    parser.add_argument('--csv', action='store_true',
                        help='兼容旧固件：仅在固件回传 SAMPLES 时额外写 CSV')
    args = parser.parse_args()

    recorder = load_recorder()

    matches = list(serial.tools.list_ports.comports())
    selected, message = recorder.verify_port(args.port, matches)
    if selected is None:
        print(f'[拒绝自检] {message}')
        return 2

    port = None
    attempts = 0
    last_error = None
    deadline = time.monotonic() + args.acquire_timeout
    while True:
        attempts += 1
        try:
            port = serial.Serial(selected.device, 115200, timeout=0.1,
                                 write_timeout=2, rtscts=False, dsrdtr=False)
            port.dtr = False
            port.rts = False
            break
        except serial.SerialException as error:
            last_error = error
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    if port is None:
        print(f'[拒绝自检] 无法打开 {selected.device}：{last_error}')
        print('该端口通常被串口调试助手或 IDE 终端独占，请先释放后重试。')
        return 2

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    session = Path(args.output) if args.output else (
        ROOT / 'tests' / 'results' / f'rt_selftest_{stamp}')
    try:
        session.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        port.close()
        print(f'[拒绝自检] 无法创建会话目录 {session}：{error}')
        return 2

    command = command_for_algorithm(args.algorithm)
    metadata = {
        'started_local': datetime.now().isoformat(timespec='seconds'),
        'port': selected.device,
        'port_hwid': selected.hwid,
        'port_description': selected.description,
        'baud': 115200,
        'command': command,
        'algorithm': int(args.algorithm),
        'wiring': 'PA15 (DAC out) --jumper-- PA25 (ADC in), GND 共地',
        'sample_rate_hz': 20000,
        'sample_count': SAMPLE_COUNT,
        'frame_duration_ms': 204.8,
        'format': '296-case text matrix; D command dumps latest LE16 frame',
        'driver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    firmware = ROOT / 'Debug' / 'TI-game.out'
    if firmware.is_file():
        metadata['firmware_sha256'] = hashlib.sha256(firmware.read_bytes()).hexdigest()
    (session / 'metadata.json').write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding='utf-8')

    started = time.monotonic()
    budget_end = started + args.timeout
    log = []
    cases = []
    current = None
    summary = None
    abort_line = None
    protocol_error = None
    gpio_preflight = None
    dac_preflight = None
    integrity_mismatches = 0

    def flush_initial():
        """丢掉打开端口前排队的启动横幅，避免把它当成协议行。"""
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            port.read(port.in_waiting or 1)

    flush_initial()
    command_bytes = command.encode('ascii')
    if port.write(command_bytes) != len(command_bytes):
        port.close()
        print('[拒绝自检] 命令写入不完整')
        return 2
    port.flush()

    with (session / 'matrix.jsonl').open('w', encoding='utf-8') as matrix_file:
        while time.monotonic() < budget_end:
            line = recorder.grab_line(port, budget_end, log, started)
            if line is None:
                protocol_error = 'DEADLINE'
                break
            if line == '':
                continue

            if line.startswith('RT-START,'):
                metadata['start_declared'] = parse_kv('RT-START', line)
                continue

            if line.startswith('RT-DAC-PREFLIGHT,'):
                dac_preflight = parse_kv('RT-DAC-PREFLIGHT', line)
                metadata['dac_preflight'] = dac_preflight
                print(f'DAC preflight: {dac_preflight}')
                continue

            if line.startswith('RT-GPIO-PREFLIGHT,'):
                gpio_preflight = parse_kv('RT-GPIO-PREFLIGHT', line)
                metadata['gpio_preflight'] = gpio_preflight
                print(f'GPIO preflight: {gpio_preflight}')
                continue

            if line.startswith('RT-CASE,'):
                if current is not None:
                    cases.append(current)
                fields = parse_kv('RT-CASE', line)
                current = {
                    'index': parse_case_index(fields),
                    'group': fields.get('group'),
                    'profile': fields.get('profile'),
                    'declared_type': fields.get('type'),
                    'declared_freq_millihz': as_int(fields, 'freq_millihz'),
                    'declared_vpp_millivolts': as_int(fields, 'vpp_millivolts'),
                    'declared_duty_ppm': as_int(fields, 'duty_ppm'),
                    'declared_cycles': as_int(fields, 'cycles'),
                    'noise_permille': as_int(fields, 'noise_permille'),
                    'edge_permille': fields.get('edge_permille'),
                }
                continue

            if line.startswith('CASCADE,'):
                if current is None:
                    protocol_error = 'CASCADE_WITHOUT_CASE'
                    break
                current['cascade'] = parse_kv('CASCADE', line)
                continue

            if line.startswith('RESULT,'):
                if current is None:
                    protocol_error = 'RESULT_WITHOUT_CASE'
                    break
                current['result'] = parse_kv('RESULT', line)
                continue

            if line.startswith('RT-CHECK,'):
                if current is None:
                    protocol_error = 'CHECK_WITHOUT_CASE'
                    break
                current['check'] = parse_kv('RT-CHECK', line)
                continue

            if line.startswith('RT-RETRY,'):
                if current is None:
                    protocol_error = 'RETRY_WITHOUT_CASE'
                    break
                current.setdefault('retries', []).append(parse_kv('RT-RETRY', line))
                continue

            if line.startswith('RT-TIMING,'):
                if current is None:
                    protocol_error = 'TIMING_WITHOUT_CASE'
                    break
                current['timing'] = parse_kv('RT-TIMING', line)
                continue

            if line.startswith('SAMPLES,'):
                if current is None:
                    protocol_error = 'SAMPLES_WITHOUT_CASE'
                    break
                fields = parse_kv('SAMPLES', line)
                declared = int(fields['bytes'])
                declared_fn = int(fields['fnv1a'])
                if declared != PAYLOAD_BYTES:
                    protocol_error = f'PAYLOAD_SIZE({declared})'
                    break
                payload = read_exactly(port, PAYLOAD_BYTES, budget_end)
                if len(payload) != PAYLOAD_BYTES:
                    protocol_error = f'PAYLOAD_SHORT({len(payload)})'
                    break
                actual_fn = recorder.fnv_of(payload)
                integrity_ok = actual_fn == declared_fn
                if not integrity_ok:
                    integrity_mismatches += 1
                samples = struct.unpack('<4096H', payload)
                name = (f'rt_{current["index"]:02d}_'
                        f'{current["declared_type"]}.bin')
                (session / name).write_bytes(payload)
                if args.csv:
                    with (session / name.replace('.bin', '.csv')).open(
                            'w', newline='', encoding='utf-8') as handle:
                        writer = csv.writer(handle)
                        writer.writerow(['序号', 'ADC码值'])
                        writer.writerows(enumerate(samples))
                current.update({
                    'bin': name,
                    'bin_sha256': hashlib.sha256(payload).hexdigest(),
                    'fnv1a_declared': declared_fn,
                    'fnv1a_actual': actual_fn,
                    'integrity_ok': integrity_ok,
                    'min_code': min(samples),
                    'max_code': max(samples),
                    'pp_code': max(samples) - min(samples),
                    'pp_mv': round((max(samples) - min(samples)) * 3300.0 / 4095.0, 2),
                })
                cases.append(current)
                current = None
                continue

            if line.startswith('RT-DONE,'):
                if current is not None:
                    cases.append(current)
                    current = None
                summary = parse_kv('RT-DONE', line)
                continue

            if line.startswith('RT-ABORT,'):
                abort_line = parse_kv('RT-ABORT', line)
                continue

            if line == 'READY':
                break

            # CAPTURING / PROCESSING / 其它中间行：只留在原始日志里。

        if current is not None:
            cases.append(current)  # 半截用例也要落盘，不能丢证据

        for record in cases:
            matrix_file.write(json.dumps(record, ensure_ascii=False) + '\n')
        matrix_file.flush()

    port.close()

    if protocol_error is None:
        protocol_error = validate_matrix_contract(
            metadata.get('start_declared'), cases, summary, abort_line,
            expected_algorithm=args.algorithm)

    # 逐条打印：类型是否正确、频率/Vpp/占空比是否落在容差内。
    print(f'{"#":>3} {"类型":<9} {"判定":<5} {"标称Hz":>9} {"实测Hz":>9} '
          f'{"标称mVpp":>9} {"实测mVpp":>9} {"码范围":>12} {"校验":>4}')
    passed = 0
    failed = 0
    for record in cases:
        check = record.get('check') or {}
        verdict = check.get('verdict', 'N/A')
        if verdict == 'PASS':
            passed += 1
        else:
            failed += 1
        result = record.get('result') or {}
        declared_freq = record.get('declared_freq_millihz')
        measured_freq = result.get('freq_millihz')
        declared_vpp = record.get('declared_vpp_millivolts')
        measured_vpp = (None if result.get('vpp_microvolts') is None
                        else round(int(result['vpp_microvolts']) / 1000.0, 2))
        code_range = (f'{record["min_code"]}-{record["max_code"]}'
                      if 'min_code' in record else 'NA')
        integrity = ('OK' if record.get('integrity_ok') else
                     ('BAD' if 'integrity_ok' in record else 'NA'))
        print(f'{record["index"]:>3} {str(record["declared_type"]):<9} '
              f'{verdict:<5} '
              f'{(declared_freq or 0) / 1000.0:>9.3f} '
              f'{(int(measured_freq) / 1000.0 if measured_freq else float("nan")):>9.3f} '
              f'{declared_vpp if declared_vpp is not None else 0:>9} '
              f'{measured_vpp if measured_vpp is not None else float("nan"):>9} '
              f'{code_range:>12} {integrity:>4}')

    outcome = {
        'session': str(session),
        'cases_recorded': len(cases),
        'passed': passed,
        'failed': failed,
        'firmware_summary': summary,
        'gpio_preflight': gpio_preflight,
        'dac_preflight': dac_preflight,
        'abort': abort_line,
        'protocol_error': protocol_error,
        'integrity_mismatches': integrity_mismatches,
        'elapsed_s': round(time.monotonic() - started, 2),
    }
    (session / 'outcome.json').write_text(
        json.dumps(outcome, indent=2, ensure_ascii=False), encoding='utf-8')
    (session / 'session.jsonl').write_text(
        '\n'.join(json.dumps(entry, ensure_ascii=False) for entry in log) + '\n',
        encoding='utf-8')

    print()
    if protocol_error:
        print(f'[协议失败] {protocol_error}（已记录 {len(cases)} 条用例）')
        print('常见原因：PA15 没有接到 PA25、串口被别的程序抢占、'
              '或固件不是含 RT 的新版本。')
        return 2
    if abort_line:
        print(f'[已中止] 固件报告 {abort_line}（收到过串口输入或按键）')
        return 1
    if integrity_mismatches:
        print(f'[校验失败] {integrity_mismatches} 帧 FNV-1a 不一致，'
              '样点不可作为证据')
        return 2

    print(f'矩阵结果：{passed}/{len(cases)} 通过'
          f'（固件自报 {summary}），用时 {outcome["elapsed_s"]}s')
    if failed:
        print('失败用例的 RT-CHECK 里 freq_ok/vpp_ok/duty_ok/type_ok '
              '指明了是哪一项超标。')
    print(f'会话目录：{session}')
    return 0 if passed == len(cases) and cases else 1


if __name__ == '__main__':
    raise SystemExit(main())
