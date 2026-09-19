import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import struct
import time


def fnv1a(payload):
    checksum = 2166136261
    for value in payload:
        checksum = ((checksum ^ value) * 16777619) & 0xffffffff
    return checksum


def generate(case, seed):
    rng = random.Random(seed)
    phase_start = rng.random()
    noise = [rng.gauss(0, 1) for index in range(4096)]
    midpoint = (max(noise) + min(noise)) / 2
    scale = case['noise'] * 2 / (max(noise) - min(noise))
    samples = []
    for index in range(4096):
        phase = (phase_start + index * case['freq'] / 20000) % 1
        if case['type'] == 'sine':
            value = math.sin(2 * math.pi * phase)
        elif case['type'] == 'triangle':
            value = 1 - 4 * abs(phase - 0.5)
        elif case['type'] == 'sawtooth':
            value = 1 - 2 * phase
        else:
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
        voltage = 1.65 + value + (noise[index] - midpoint) * scale
        if not 0 < voltage < 3.3:
            raise ValueError('Generated input would clip')
        samples.append(round(voltage * 4095 / 3.3))
    return struct.pack('<4096H', *samples)


def parse_result(line):
    parts = line.split(',')
    if parts.pop(0) != 'RESULT':
        raise ValueError('Expected RESULT')
    result = {}
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
    started = time.monotonic()

    def read_line():
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
    for offset in range(0, len(payload), 256):
        block = payload[offset:offset + 256]
        if port.write(block) != len(block):
            raise IOError('Incomplete payload write')
    port.flush()
    receipt = read_line()
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
    cases = []
    for noise in (0, 0.2):
        for frequency in (100, 250, 400):
            for wave in ('sine', 'square', 'triangle', 'sawtooth'):
                settings = ((20, 0), (50, 0), (80, 0), (20, 0.1), (50, 0.1), (80, 0.1)) if wave == 'square' else ((50, 0),)
                for duty, edge in settings:
                    cases.append(dict(type=wave, freq=frequency, duty=duty, edge=edge, noise=noise))
    return cases


def main():
    import serial
    import serial.tools.list_ports
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    matches = [port for port in serial.tools.list_ports.comports() if port.device.upper() == args.port.upper()]
    if len(matches) != 1 or 'XDS110' not in matches[0].description or 'Application' not in matches[0].description:
        raise RuntimeError('Not an explicitly verified XDS110 Application UART')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
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
                record['error'] = f'{type(error).__name__}: {error}'
                print(json.dumps(record), flush=True)
                report.write(json.dumps(record) + '\n')
                report.flush()
                return 2
            report.write(json.dumps(record) + '\n')
            report.flush()
    print(f'Completed 54 transactions; engineering failures={failures}')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
