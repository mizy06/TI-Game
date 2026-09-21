"""可审计的串口数字注入测试。

这份工具强调“结果能追溯到原始输入”：它生成确定性带噪波形，把 4096 个
12 位 ADC 码按小端 uint16 发送给固件，核对固件返回的字节数和 FNV-1a，
再保存每个输入二进制、串口原文、固件哈希、脚本哈希与判定结果。

它只测试 UART 注入到算法输出的数字链路，不代表 PA25、参考电压或模拟
前端已经校准。必须显式指定 XDS110 Application UART 和一个全新输出目录。
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import time


def fnv1a(payload):
    """计算与固件 receive_uart_samples() 完全相同的 32 位 FNV-1a。

    初值和乘数是 FNV-1a 标准常量；``& 0xffffffff`` 模拟 C ``uint32_t``
    乘法溢出后的低 32 位。该校验用于发现传输损坏，不具备加密安全性。
    """
    checksum = 2166136261
    for value in payload:
        checksum = ((checksum ^ value) * 16777619) & 0xffffffff
    return checksum


def generate(case, seed):
    """根据一个用例字典生成固定可复现的 8192 字节 ADC 负载。

    ``case`` 包含 type/freq/duty/edge/noise；``seed`` 决定起始相位和噪声。
    波形先在归一化 [-1,+1] 中生成，再换算到 1.65 V 中心、约 2 Vpp，
    最后映射成 4096 个 12 位码并用 ``<4096H`` 打包：``<`` 是小端，
    ``H`` 是无符号 16 位整数。若信号会碰 0/3.3 V，直接报错而不伪造削顶。
    """
    rng = random.Random(seed)
    phase_start = rng.random()
    noise = [rng.gauss(0, 1) for index in range(4096)]
    # 按本段有限噪声的实际峰峰范围缩放，而不是只按理论标准差估算。
    midpoint = (max(noise) + min(noise)) / 2
    scale = case['noise'] * 2 / (max(noise) - min(noise))
    samples = []
    for index in range(4096):
        # phase 用 0~1 表示一整周；每点前进 frequency/sample_rate 周。
        phase = (phase_start + index * case['freq'] / 20000) % 1
        if case['type'] == 'sine':
            value = math.sin(2 * math.pi * phase)
        elif case['type'] == 'triangle':
            value = 1 - 4 * abs(phase - 0.5)
        elif case['type'] == 'sawtooth':
            value = 1 - 2 * phase
        else:
            # 方波的 duty 从百分数换成比例；edge 表示边沿占一周的比例。
            duty, edge = case['duty'] / 100, case['edge']
            shifted = (phase + edge / 2) % 1
            if edge and shifted < edge:
                value = -1 + 2 * shifted / edge
            elif shifted < duty:
                value = 1
            elif edge and shifted < duty + edge:
                value = 1 - 2 * (shifted - duty) / edge
            else:
                value = -1
        # value 的半幅为 1 V，所以基础信号为 2 Vpp。
        voltage = 1.65 + value + (noise[index] - midpoint) * scale
        if not 0 < voltage < 3.3:
            raise ValueError('Generated input would clip')
        samples.append(round(voltage * 4095 / 3.3))
    return struct.pack('<4096H', *samples)


def parse_result(line):
    """严格解析一行固件 ``RESULT,key=value,...`` 文本。

    重复字段、缺少必需字段、非有限数、非正测量值、类别/占空比不一致都
    会抛异常。测试工具宁愿明确失败，也不把半截或畸形串口文本当成成功。
    失败状态允许没有测量字段，但类型必须是 ``unknown``。
    """
    parts = line.split(',')
    if parts.pop(0) != 'RESULT':
        raise ValueError('Expected RESULT')
    result = {}
    # split('=', 1) 只切第一个等号，随后用字典检测重复键。
    for part in parts:
        key, value = part.split('=', 1)
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    if result['status'] != 'OK':
        if result['type'] != 'unknown':
            raise ValueError('Inconsistent failure')
        return result
    if result['type'] not in ('sine', 'square', 'triangle', 'sawtooth'):
        raise ValueError('Invalid type')
    # OK 结果必须包含这些数值，且 float('nan')/float('inf') 也要拒绝。
    for key in ('freq_hz', 'vpp_v', 'score_pct', 'margin_pct', 'cycles'):
        result[key] = float(result[key])
        if not math.isfinite(result[key]):
            raise ValueError('Nonfinite measurement')
    if result['freq_hz'] <= 0 or result['vpp_v'] <= 0:
        raise ValueError('Nonpositive measurement')
    if result['type'] == 'square':
        result['duty_pct'] = float(result['duty_pct'])
        if not 0 < result['duty_pct'] < 100:
            raise ValueError('Invalid duty')
    elif result['duty_pct'] != 'NA':
        raise ValueError('Unexpected duty')
    return result


def transact(port, payload, log):
    """完成一次有握手、回执和结束标志的注入事务。

    协议顺序严格为 W -> READY_FOR_DATA -> 8192 字节 -> DATA 回执 ->
    PROCESSING -> RESULT -> READY。任一步错误都抛异常；特别是握手失败时
    不发送二进制，以免固件把数据字节误解为后续命令。

    ``port`` 只需提供 pyserial 风格的 write/flush/readline，因而单元测试可
    传入 FakePort。``log`` 会追加每行耗时、原始十六进制和解码文本。
    """
    started = time.monotonic()

    def read_line():
        """在整次事务开始后的 10 秒总期限内读取一行 ASCII。"""
        while time.monotonic() - started < 10:
            raw = port.readline()
            if raw:
                text = raw.decode('ascii').strip()
                log.append(dict(elapsed_s=time.monotonic() - started, raw_hex=raw.hex(), text=text))
                return text
        raise TimeoutError('10 second deadline exceeded')

    if port.write(b'W') != 1:
        raise IOError('Incomplete command')
    port.flush()
    if read_line() != 'READY_FOR_DATA':
        raise RuntimeError('Handshake failed; no payload sent')
    if len(payload) != 8192:
        raise ValueError('Invalid payload size')
    # 分成 256 字节小块，避免某些 USB-UART 驱动一次写入过大时拥塞。
    for offset in range(0, len(payload), 256):
        block = payload[offset:offset + 256]
        if port.write(block) != len(block):
            raise IOError('Incomplete payload write')
    port.flush()
    receipt = read_line()
    # 字节数正确但顺序/内容错误时，FNV-1a 仍会不匹配。
    expected_receipt = f'DATA,bytes={len(payload)},fnv1a={fnv1a(payload)}'
    if receipt != expected_receipt:
        raise RuntimeError(f'Data integrity failure: {receipt!r}, expected {expected_receipt!r}')
    if read_line() != 'PROCESSING':
        raise RuntimeError('Expected PROCESSING')
    result = parse_result(read_line())
    elapsed = time.monotonic() - started
    if read_line() != 'READY':
        raise RuntimeError('Missing terminal READY')
    return result, elapsed


def make_cases():
    """构造 54 个覆盖类别、边界频率、噪声、占空比和边沿的用例。

    非方波每组参数只需一个占空比占位；方波额外覆盖 20/50/80% 占空比
    与 0/10% 边沿宽度。三档频率×两档噪声共同覆盖比赛范围。
    """
    cases = []
    for noise in (0, 0.2):
        for frequency in (100, 250, 400):
            for wave in ('sine', 'square', 'triangle', 'sawtooth'):
                settings = ((20, 0), (50, 0), (80, 0), (20, 0.1), (50, 0.1), (80, 0.1)) if wave == 'square' else ((50, 0),)
                for duty, edge in settings:
                    cases.append(dict(type=wave, freq=frequency, duty=duty, edge=edge, noise=noise))
    return cases


def main():
    """解析命令行、验证端口身份、执行用例并写出审计目录。"""
    # serial 只在真正运行硬件工具时导入，让不装 pyserial 的主机仍能导入
    # 本模块并运行 parse_result/fnv1a 等单元测试。
    import serial
    import serial.tools.list_ports
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    # 不自动退回任意串口：审计测试必须唯一匹配明确指定的 XDS110 应用口。
    matches = [port for port in serial.tools.list_ports.comports() if port.device.upper() == args.port.upper()]
    if len(matches) != 1 or 'XDS110' not in matches[0].description or 'Application' not in matches[0].description:
        raise RuntimeError('Not an explicitly verified XDS110 Application UART')
    output = Path(args.output)
    # exist_ok=False 防止覆盖旧证据或把两次测试混进同一目录。
    output.mkdir(parents=True, exist_ok=False)
    # 元数据明确写“非 ADC”，并记录二进制/脚本 SHA-256，便于日后确认版本。
    metadata = dict(port=args.port, hwid=matches[0].hwid, mode='UART injection; NOT ADC', seed=20260917,
                    fs=20000, count=4096, dc_v=1.65, vpp_v=2,
                    noise_model='Finite Gaussian white record scaled to requested Vpp; Nyquist 10kHz',
                    thresholds_NOT_official=dict(freq_pct=2, vpp_pct=8, duty_pp=3),
                    firmware_identity='audited-20260917-v1; see flash and boot logs',
                    firmware_sha256=hashlib.sha256(Path('Debug/TI-game.out').read_bytes()).hexdigest(),
                    test_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (output / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    failures = 0
    with serial.Serial(args.port, 115200, timeout=0.1, write_timeout=2) as port, (output / 'results.jsonl').open('w', encoding='utf-8') as report:
        # 测试开始先保存 1 秒内已有的串口字节，通常包含启动横幅，也能发现
        # 端口连接到了错误设备或固件刚复位。
        initial = bytearray()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            initial.extend(port.read(port.in_waiting or 1))
        (output / 'initial_rx.bin').write_bytes(initial)
        for index, case in enumerate(make_cases()):
            record = dict(index=index + 1, expected=case, log=[])
            try:
                payload = generate(case, 20260917 + index)
                (output / f'input_{index + 1:03d}.bin').write_bytes(payload)
                record['input_sha256'] = hashlib.sha256(payload).hexdigest()
                result, elapsed = transact(port, payload, record['log'])
                record.update(result=result, elapsed_s=elapsed)
                # 门禁顺序：协议成功、分类正确、总耗时合格，再检查测量误差。
                passed = result['status'] == 'OK' and result['type'] == case['type'] and elapsed <= 10
                if result['status'] == 'OK':
                    errors = dict(freq_pct=abs(result['freq_hz'] / case['freq'] - 1) * 100, vpp_pct=abs(result['vpp_v'] / 2 - 1) * 100)
                    passed = passed and errors['freq_pct'] <= 2 and errors['vpp_pct'] <= 8
                    if case['type'] == 'square':
                        duty = result['duty_pct']
                        errors['duty_pp'] = abs(duty - case['duty']) if isinstance(duty, (int, float)) else None
                        passed = passed and errors['duty_pp'] is not None and errors['duty_pp'] <= 3
                    record['errors'] = errors
                record['passed'] = bool(passed)
                failures += not passed
                print(json.dumps({key: value for key, value in record.items() if key != 'log'}), flush=True)
            except Exception as error:
                # 协议/文件/端口异常与普通“算法指标未过”不同：立即保存并以
                # 退出码 2 中止，避免后续记录建立在失步串口之上。
                record['error'] = f'{type(error).__name__}: {error}'
                print(json.dumps(record), flush=True)
                report.write(json.dumps(record) + '\n')
                report.flush()
                return 2
            report.write(json.dumps(record) + '\n')
            report.flush()
    print(f'Completed 54 transactions; engineering failures={failures}')
    # 0=全过，1=事务完成但有工程门禁失败，2=上方的基础设施/协议异常。
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
