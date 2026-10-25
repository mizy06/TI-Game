"""Resumable LAN sweep of raw Vpp bias against native generator setpoints.

Gaussian noise uses sigma=ratio*Vpp/6, without harmonic guards. Results are
relative to generator settings, not an independent voltage reference.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket
import statistics
import sys
import time

from tools.capture_store import CaptureStore, atomic_json
from tools.precision_batch import measure_batch
from tools.ro_test import Vxi11
from tools.unified_solver import SolverConfig
from tools.vpp_accuracy_usb import snapshot, restore
from tools.waveform_server import Engine, SerialService


class Vxi11Instrument:
    """Native SCPI through the generator's VXI-11 inst0 service."""
    def __init__(self, host, timeout=5., port=111):
        self.connection = Vxi11(host, port, timeout)
        try:
            self.connection.open()
        except Exception as error:
            self.connection.close()
            raise ConnectionError(f'Cannot open generator VXI-11 {host}:{port}: {error}. '
                                  'Check generator LAN IP and VXI-11 service.') from error

    def write(self, command):
        self.connection.write(command.encode('ascii')+b'\n')

    def query(self, command):
        self.write(command)
        return self.connection.read(limit=65536, text=True).decode('ascii').strip()

    def close(self):
        self.connection.close()


class SocketInstrument:
    """Native LF-terminated SCPI, without VISA resource discovery."""
    def __init__(self, host, timeout=5., port=5025):
        try:
            self.connection = socket.create_connection((host, port), timeout)
        except OSError as error:
            raise ConnectionError(f'Cannot connect to generator {host}:{port}: {error}. '
                                  'Check generator LAN IP, Ethernet link and subnet.') from error
        self.connection.settimeout(timeout)
        self.reader = self.connection.makefile('rb')

    def write(self, command):
        self.connection.sendall(command.encode('ascii')+b'\n')

    def query(self, command):
        self.write(command)
        answer = self.reader.readline(65536)
        if not answer.endswith(b'\n'):
            raise ConnectionError('Generator disconnected or returned an incomplete SCPI reply')
        return answer.decode('ascii').strip()

    def close(self):
        try:
            self.reader.close()
        finally:
            self.connection.close()


@contextmanager
def device_lock(port):
    """OS releases the lock even when a process exits unexpectedly."""
    path = Path('data')/('vpp-scan-'+''.join(c for c in port if c.isalnum())+'.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError(f'Another scan already owns {port}; stop that scan first.') from error
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class PacedInstrument:
    """Avoid silently dropped back-to-back native waveform commands."""
    def __init__(self, instrument):
        self.instrument = instrument

    def write(self, command):
        result = self.instrument.write(command)
        time.sleep(.06)
        return result

    def query(self, command):
        return self.instrument.query(command)

    def close(self):
        self.instrument.close()


def open_instrument(args):
    transport = Vxi11Instrument if args.transport == 'vxi11' else SocketInstrument
    return PacedInstrument(transport(args.host, port=args.instrument_port))


def check_connection(args):
    """Check SCPI replies and UART access without changing source settings."""
    import serial
    instrument = open_instrument(args)
    try:
        saved = snapshot(instrument)
        print(f'Generator: {saved["*IDN?"]}', flush=True)
        for query in ('C1:OUTP?', 'C2:OUTP?'):
            print(saved[query], flush=True)
        with serial.Serial(args.port, 115200, timeout=3, write_timeout=3):
            print(f'UART: {args.port} opened at 115200 baud', flush=True)
        print('Connection check passed; no waveform settings changed or captures requested.', flush=True)
    finally:
        instrument.close()


def cases(frequency_step):
    frequencies = list(range(100, 401, frequency_step))
    if frequencies[-1] != 400:
        frequencies.append(400)
    for amplitudes in (range(80, 161, 10), range(85, 160, 10)):
        for frequency in frequencies:
            for centivolts in amplitudes:
                for ratio in (.1, .4):
                    for wave in ('sine', 'square', 'triangle', 'sawtooth'):
                        yield dict(wave=wave, frequency_hz=frequency,
                                   vpp_v=centivolts/100, noise_ratio=ratio,
                                   noise_bandwidth_ratio=6. if ratio == .1 else 3.)


def key(case):
    return tuple(case[k] for k in ('wave', 'frequency_hz', 'vpp_v', 'noise_ratio'))


def schedule(plan, done):
    for repeat in range(3):
        for index, case in enumerate(plan):
            if (key(case), repeat) not in done:
                yield repeat, index, case


def aggregate(rows):
    groups = {}
    for row in rows:
        if row.get('valid'):
            groups.setdefault(key(row), []).append(row)
    result = []
    for values in groups.values():
        measured = [r['measured_vpp_v'] for r in values]
        reference = values[0]['vpp_v']
        mean = statistics.mean(measured)
        primary = [r['measured_vpp_v'] for r in values if not r.get('fallback_used')]
        result.append(dict(zip(('wave', 'frequency_hz', 'vpp_v', 'noise_ratio'), key(values[0])),
                           noise_bandwidth_ratio=values[0].get('noise_bandwidth_ratio'),
                           valid_repeats=len(values), complete=len(values) == 3,
                           fallback_repeats=sum(bool(r.get('fallback_used')) for r in values),
                           primary_only_mean_vpp_v=statistics.mean(primary) if primary else None,
                           mean_measured_vpp_v=mean, bias_v=mean-reference,
                           correction_to_add_v=reference-mean,
                           bias_pct=100*(mean/reference-1),
                           repeat_std_v=statistics.stdev(measured) if len(measured)>1 else None,
                           repeat_range_v=max(measured)-min(measured)))
    return result


def save_summary(folder, rows, planned, started):
    table = aggregate(rows)
    atomic_json(folder/'offsets.json', table)
    if table:
        with (folder/'offsets.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    atomic_json(folder/'progress.json', dict(planned_combinations=planned,
        complete_combinations=sum(r['complete'] for r in table),
        attempted_measurements=len(rows), valid_measurements=sum(bool(r.get('valid')) for r in rows),
        failed_measurements=sum(not r.get('valid') for r in rows), elapsed_s=time.monotonic()-started,
        complete=sum(r['complete'] for r in table) == planned))


def configure(instrument, case):
    wave, frequency, vpp, ratio = key(case)
    bandwidth = case.get('noise_bandwidth_ratio', 3.)*frequency
    for command in ('C1:OUTP OFF', 'C1:CMBN OFF', 'C2:OUTP OFF', 'C2:CMBN OFF',
                    'C1:SRATE MODE,DDS'):
        instrument.write(command)
    kind = dict(sine='SINE', square='SQUARE', triangle='RAMP', sawtooth='RAMP')[wave]
    instrument.write(f'C1:BSWV WVTP,{kind},FRQ,{frequency},AMP,{vpp},OFST,1.6,PHSE,0')
    if kind == 'RAMP':
        instrument.write(f'C1:BSWV SYM,{50 if wave == "triangle" else 0}')
    if wave == 'square':
        instrument.write('C1:BSWV DUTY,50')
    if ratio:
        instrument.write(f'C2:BSWV WVTP,NOISE,STDEV,{ratio*vpp/6:.12g},MEAN,0,BANDSTATE,ON,BANDWIDTH,{bandwidth}')
        instrument.write('C2:OUTP ON')
        instrument.write('C1:CMBN ON')
    instrument.write('C1:OUTP ON')
    if instrument.query('*OPC?').strip() != '1':
        raise RuntimeError('generator operation incomplete')
    time.sleep(.25)
    # Reassert after output/waveform transitions have completed; firmware can
    # otherwise report the preceding combine state despite acknowledging OPC.
    desired_combine = 'ON' if ratio else 'OFF'
    for attempt in range(3):
        instrument.write(f'C1:CMBN {desired_combine}')
        time.sleep(.3)
        if instrument.query('C1:CMBN?').strip().split()[-1] == desired_combine:
            break
    else:
        raise ValueError('generator did not accept noise combine setting')
    readback = {q: instrument.query(q).strip() for q in
                ('C1:BSWV?', 'C2:BSWV?', 'C1:CMBN?', 'C1:OUTP?', 'C2:OUTP?')}
    if readback['C1:CMBN?'].split()[-1] != ('ON' if ratio else 'OFF'):
        raise ValueError('generator noise combine state differs from request')
    from tools.vpp_accuracy_usb import fields
    if fields(readback['C1:OUTP?'])[0] != 'ON':
        raise ValueError('generator source output is disabled')
    # CMBN routes CH2 internally into CH1 and can disable the CH2 physical
    # connector. Its OUTP state is recorded, not used as a combine-state test.
    actual = fields(readback['C1:BSWV?'])
    if actual[actual.index('WVTP')+1] != kind:
        raise ValueError('generator waveform readback differs from request')
    for name, expected, unit in [('FRQ', frequency, 'HZ'), ('AMP', vpp, 'V'), ('OFST', 1.6, 'V')]:
        value = float(actual[actual.index(name)+1].removesuffix(unit))
        if abs(value-expected) > 1e-5:
            raise ValueError(f'generator {name} readback differs: {value} != {expected}')
    if ratio:
        noise = fields(readback['C2:BSWV?'])
        for name, expected, unit in [('STDEV', ratio*vpp/6, 'V'), ('BANDWIDTH', bandwidth, 'HZ')]:
            value = float(noise[noise.index(name)+1].removesuffix(unit))
            if abs(value-expected) > max(1e-6, abs(expected)*1e-4):
                raise ValueError(f'generator noise {name} readback differs')
    return readback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='10.11.13.220')
    parser.add_argument('--transport', choices=('vxi11', 'socket'), default='vxi11',
                        help='Generator LAN protocol (default: VXI-11, no VISA needed)')
    parser.add_argument('--instrument-port', type=int,
                        help='Generator port (default: 111 for VXI-11; 5025 for raw socket)')
    parser.add_argument('--port', default='COM15')
    parser.add_argument('--frequency-step', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True, help='Exact resumable run folder')
    parser.add_argument('--limit', type=int, help='Run only the first N combinations for pilot checks')
    parser.add_argument('--pilot', action='store_true', help='Four waves at both Vpp boundaries and both noise levels')
    parser.add_argument('--plan-only', action='store_true')
    parser.add_argument('--accept-source-change', action='store_true',
                        help='Record changed source hashes and resume existing results; plan and calibration must match')
    parser.add_argument('--check', action='store_true',
                        help='Check generator replies and UART access without scanning')
    args = parser.parse_args()
    if args.instrument_port is None:
        args.instrument_port = 111 if args.transport == 'vxi11' else 5025
    if not 1 <= args.instrument_port <= 65535:
        parser.error('instrument-port must be 1..65535')
    if args.transport == 'vxi11' and args.instrument_port in (5024, 5025):
        parser.error('VXI-11 uses portmapper port 111, not raw SCPI ports 5024/5025')
    if not 1 <= args.frequency_step <= 300:
        parser.error('frequency-step must be 1..300 Hz')
    if args.limit is not None and args.limit < 1:
        parser.error('limit must be positive')
    if args.check and args.plan_only:
        parser.error('choose either --check or --plan-only')
    if args.plan_only:
        return run(args)
    with device_lock(args.port):
        if args.check:
            return check_connection(args)
        return run(args)


def validate_resume(folder, manifest, accept_source_change=False):
    path = folder/'plan.json'
    if not path.exists():
        atomic_json(path, manifest)
        return
    previous = json.loads(path.read_text(encoding='utf-8'))
    changed = sorted(k for k in set(previous) | set(manifest)
                     if previous.get(k) != manifest.get(k))
    if not changed:
        return
    if changed != ['source_sha256']:
        raise ValueError('resume settings changed: '+', '.join(changed)+
                         '; use a new folder for a different plan or calibration')
    if not accept_source_change:
        raise ValueError('resume source code changed; settings and calibration match. '
                         'Use --accept-source-change to retain results and record the new code version')
    atomic_json(folder/f'source-change-{time.time_ns()}.json',
                dict(previous_manifest=previous, next_manifest=manifest,
                     note='Existing measurements retained; subsequent measurements use the new source version'))
    atomic_json(path, manifest)
    print('Source change recorded; existing measurements retained.', flush=True)


def run(args):
    plan = list(cases(args.frequency_step))
    if args.pilot:
        plan = [c for c in plan if c['frequency_hz'] == 100 and
                c['vpp_v'] in (.8, 1.6)]
    if args.limit:
        plan = plan[:args.limit]
    parameters = Path('algorithm_parameters.json')
    manifest = dict(cases=plan, repeats=3, frames_per_repeat=3, host=args.host,
                    transport=args.transport, instrument_port=args.instrument_port,
                    traversal='three_full_rounds; coarse_then_midpoints; frequency,vpp,noise,wave',
                    parameters_sha256=hashlib.sha256(parameters.read_bytes()).hexdigest(),
                    source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in [Path(__file__), *Path('tools').glob('*.py')]},
                    offset_v=1.6, square_duty=.5, noise_bandwidth_by_ratio={'0.1': 6., '0.4': 3.},
                    noise_definition='Gaussian sigma = noise_ratio * Vpp / 6; no harmonic guards',
                    reference='native generator setpoint; no independent voltage reference',
                    estimator='raw internal Vpp before display deduction; mean of 3 independent R batches')
    print(f'{len(plan)} combinations; {3*len(plan)} R batches; {9*len(plan)} frames', flush=True)
    if args.plan_only:
        return
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder/'plan.json'
    validate_resume(folder, manifest, args.accept_source_change)
    rows = []
    results_path = folder/'results.jsonl'
    if results_path.exists():
        with results_path.open(encoding='utf-8') as stream:
            for line in stream:
                row = json.loads(line)
                row.pop('batch', None)
                rows.append(row)
    done = {(key(row), row['repeat']) for row in rows if row.get('valid')}
    started = time.monotonic()
    import serial
    instrument = service = engine = saved = None
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        print(f'Connecting generator: {args.transport} {args.host}:{args.instrument_port}', flush=True)
        instrument = open_instrument(args)
        saved = snapshot(instrument)
        print(f'Generator: {saved["*IDN?"]}', flush=True)
        atomic_json(folder/f'generator-before-{time.time_ns()}.json', saved)
        for channel in ('C1', 'C2'):
            if 'LOAD,HZ' not in saved[f'{channel}:OUTP?']:
                raise ValueError(f'{channel} must already use HZ load')
        engine = Engine(None, output=io.StringIO(), config=SolverConfig(max_seconds=10.), workers=3)
        engine.warm()
        port = serial.Serial(args.port, 115200, timeout=3, write_timeout=3)
        service = SerialService(port, CaptureStore(folder/'captures'), engine, parameters_path=parameters)
        consecutive_failures = 0
        with results_path.open('a', encoding='utf-8', buffering=1) as stream:
            for round_index, index, case in schedule(plan, done):
                pending = [round_index]
                if shutil.disk_usage(folder).free < 2*1024**3:
                    raise RuntimeError('less than 2 GiB free disk space; stopped before capture')
                readback = configure(instrument, case)
                attempts = {}
                for repeat in pending:
                    attempts[repeat] = attempts.get(repeat, 0)+1
                    row = dict(case, repeat=repeat, attempt=attempts[repeat],
                               time_ns=time.time_ns(), source_readback=readback)
                    try:
                        batch = measure_batch(service, 3, 15.)
                        measured = batch['measured']
                        value = measured['vpp_v']
                        row.update(batch=batch, best_available_measurement=measured,
                                   fallback_used=batch.get('fallback_used', False),
                                   fallback_reasons=batch.get('fallback_reasons', []),
                                   used_frames=batch['frames'], frame_selection=batch.get('frame_selection'))
                        if not measured.get('ok', True) or not 0 < value < 3.3:
                            raise ValueError('invalid Vpp estimate')
                        if batch['output'].split(',')[0] != case['wave']:
                            raise ValueError('waveform classification differs from source')
                        if abs(measured['frequency_hz']/case['frequency_hz']-1) > .02:
                            raise ValueError('measured frequency differs from source by more than 2 percent')
                        row.update(valid=True, measured_vpp_v=value,
                                   bias_v=value-case['vpp_v'], batch=batch)
                        consecutive_failures = 0
                    except Exception as error:
                        row.update(valid=False, error=f'{type(error).__name__}: {error}')
                        consecutive_failures += 1
                    stream.write(json.dumps(row, allow_nan=False)+'\n')
                    rows.append({k: v for k, v in row.items() if k != 'batch'})
                    if row['valid']:
                        print(f'round={repeat+1}/3 {index+1}/{len(plan)} {key(case)} bias={row["bias_v"]:+.8f}V '
                              f'fallback={row.get("fallback_used",False)} frames={row.get("used_frames")}', flush=True)
                        if row.get('fallback_used'):
                            print('  fallback reason: '+','.join(row.get('fallback_reasons',[])), flush=True)
                    else:
                        print(f'{index+1}/{len(plan)} {key(case)} ERROR {row["error"]}', flush=True)
                    if consecutive_failures >= 3:
                        raise RuntimeError('three consecutive invalid measurements; stopped for diagnosis')
                    if not row['valid']:
                        if attempts[repeat] >= 3:
                            raise RuntimeError('repeat failed three attempts; stopped for diagnosis')
                        pending.append(repeat)
                if index % 10 == 0 or index == len(plan)-1:
                    save_summary(folder, rows, len(plan), started)
    finally:
        try:
            if instrument is not None and saved is not None:
                restore(instrument, saved)
        finally:
            if service is not None:
                service.close()
            if engine is not None:
                engine.close()
            if instrument is not None:
                instrument.close()
            if sys.platform == 'win32':
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            save_summary(folder, rows, len(plan), started)


if __name__ == '__main__':
    try:
        main()
    except (ConnectionError, OSError, RuntimeError, ValueError) as error:
        print(f'scan: {error}', file=sys.stderr, flush=True)
        raise SystemExit(2)
