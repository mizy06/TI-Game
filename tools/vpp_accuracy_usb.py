"""Repeatable native-waveform Vpp checks; source settings enter reports only.

The source state is saved before changes and restored on exit. Raw UART frames,
calibration provenance and per-frame analysis are retained by SerialService.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import time

import numpy as np

from tools.capture_store import CaptureStore, atomic_json
from tools.precision_batch import measure_batch
from tools.unified_solver import SolverConfig
from tools.waveform_server import Engine, SerialService


def snapshot(instrument):
    return {q: instrument.query(q).strip() for q in (
        '*IDN?', 'C1:BSWV?', 'C2:BSWV?', 'C1:OUTP?', 'C2:OUTP?',
        'C1:CMBN?', 'C2:CMBN?', 'C1:SRATE?', 'C2:SRATE?')}


def fields(response):
    return response.split(' ', 1)[1].split(',')


def restore(instrument, saved):
    for channel in ('C1', 'C2'):
        instrument.write(f'{channel}:OUTP OFF')
        instrument.write(f'{channel}:CMBN OFF')
    for channel in ('C1', 'C2'):
        rate = fields(saved[f'{channel}:SRATE?'])
        mode = rate[rate.index('MODE')+1]
        command = f'{channel}:SRATE MODE,{mode}'
        if mode == 'TARB':
            command += ',VALUE,'+rate[rate.index('VALUE')+1].removesuffix('Sa/s')
            if 'INTER' in rate:
                command += ',INTER,'+rate[rate.index('INTER')+1]
        instrument.write(command)
        values = fields(saved[f'{channel}:BSWV?'])
        kind = values[values.index('WVTP')+1]
        keys = ('STDEV', 'MEAN', 'BANDSTATE', 'BANDWIDTH') if kind == 'NOISE' else (
            'FRQ', 'AMP', 'OFST', 'PHSE', 'DUTY', 'SYM')
        instrument.write(f'{channel}:BSWV WVTP,{kind}')
        for key in keys:
            if key in values:
                instrument.write(f'{channel}:BSWV {key},{values[values.index(key)+1]}')
    for channel in ('C1', 'C2'):
        instrument.write(saved[f'{channel}:CMBN?'])
        output = fields(saved[f'{channel}:OUTP?'])
        instrument.write(f'{channel}:OUTP {output[0]}')


class FixedRateService(SerialService):
    """Experimental fixed rate, leaving the normal R sampling policy intact."""
    timer_ticks = None

    def capture(self, timeout=10.):
        if self.timer_ticks is None:
            return super().capture(timeout)
        return super().capture_at_rate(f'U{self.timer_ticks:04d}\n'.encode(), timeout)

    def capture_at_rate(self, command, timeout=10.):
        if self.timer_ticks is not None:
            command = f'U{self.timer_ticks:04d}\n'.encode()
        return super().capture_at_rate(command, timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM15')
    parser.add_argument('--resource', default='USB0::0xF4EC::0x1101::SDG6XFCC900304::INSTR')
    parser.add_argument('--waves', nargs='+', choices=['sine', 'triangle', 'square', 'sawtooth'],
                        default=['sine', 'triangle', 'square', 'sawtooth'])
    parser.add_argument('--frequencies', nargs='+', type=float, default=[305.])
    parser.add_argument('--vpps', nargs='+', type=float, default=[1.35])
    parser.add_argument('--offset', type=float, default=1.6)
    parser.add_argument('--noise-std', nargs='+', type=float, default=[0.])
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--ticks', nargs='+', type=int, default=[0],
                        help='0: normal R; otherwise fixed timer period')
    parser.add_argument('--parameters', type=Path, default=Path('algorithm_parameters.json'))
    parser.add_argument('--output', type=Path, default=Path('data/vpp-accuracy'))
    args = parser.parse_args()
    import pyvisa
    import serial

    store = CaptureStore(args.output)
    print(f'Data: {store.path}', flush=True)
    atomic_json(store.path/'provenance.json', dict(
        arguments={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        parameters_sha256=hashlib.sha256(args.parameters.read_bytes()).hexdigest(),
        source_sha256={name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                      for name in ('host_waveform.py', 'vpp_spectral.py', 'precision_batch.py')},
        reference='generator native waveform setpoint; no independent voltmeter'))
    engine = Engine(None, output=io.StringIO(), config=SolverConfig(max_seconds=10.), workers=3)
    service = instrument = manager = None
    saved = None
    rows = []
    complete = False
    try:
        engine.warm()
        port = serial.Serial(args.port, 115200, timeout=3, write_timeout=3)
        service = FixedRateService(port, store, engine, parameters_path=args.parameters)
        manager = pyvisa.ResourceManager()
        instrument = manager.open_resource(args.resource)
        instrument.timeout = 5000
        saved = snapshot(instrument)
        atomic_json(store.path/'generator-before.json', saved)
        with (store.path/'results.jsonl').open('w', encoding='utf-8', buffering=1) as stream:
            for wave in args.waves:
                for frequency in args.frequencies:
                    for vpp in args.vpps:
                        for sigma in args.noise_std:
                            instrument.write('C1:OUTP OFF')
                            instrument.write('C1:CMBN OFF')
                            instrument.write('C2:OUTP OFF')
                            instrument.write('C2:CMBN OFF')
                            instrument.write('C1:SRATE MODE,DDS')
                            kind = dict(sine='SINE', triangle='RAMP', square='SQUARE', sawtooth='RAMP')[wave]
                            instrument.write(f'C1:BSWV WVTP,{kind},FRQ,{frequency:.12g},AMP,{vpp:.12g},OFST,{args.offset:.12g}')
                            if kind == 'RAMP':
                                instrument.write(f'C1:BSWV SYM,{50 if wave == "triangle" else 0}')
                            if kind == 'SQUARE':
                                instrument.write('C1:BSWV DUTY,50')
                            if sigma > 0:
                                instrument.write(f'C2:BSWV WVTP,NOISE,STDEV,{sigma:.12g},MEAN,0,BANDSTATE,ON,BANDWIDTH,{6*frequency:.12g}')
                                instrument.write('C2:OUTP ON')
                                instrument.write('C1:CMBN ON')
                            instrument.write('C1:OUTP ON')
                            readback = instrument.query('C1:BSWV?').strip()
                            time.sleep(.2)
                            for ticks in args.ticks:
                                service.timer_ticks = ticks or None
                                for repeat in range(args.repeats):
                                    row = dict(wave=wave, frequency_hz=frequency, vpp_v=vpp,
                                               noise_std_v=sigma, timer_ticks=ticks, repeat=repeat,
                                               source_readback=readback)
                                    try:
                                        batch = measure_batch(service, args.frames, 15.)
                                        error = batch['measured']['vpp_v']/vpp-1
                                        row.update(batch=batch, relative_error=error,
                                                   passed=abs(error) < .001)
                                        print(f'{wave} {frequency:g}Hz {vpp:g}V sigma={sigma:g} ticks={ticks} '
                                              f'Vpp={batch["measured"]["vpp_v"]:.9f} error={error*100:+.5f}% '
                                              f'{batch["elapsed_s"]:.2f}s', flush=True)
                                    except Exception as error:
                                        row.update(error=f'{type(error).__name__}: {error}', passed=False)
                                        print(row['error'], flush=True)
                                    rows.append(row)
                                    stream.write(json.dumps(row)+'\n')
        complete = True
    finally:
        try:
            if instrument is not None and saved is not None:
                restore(instrument, saved)
                atomic_json(store.path/'generator-after.json', snapshot(instrument))
        finally:
            if instrument is not None:
                instrument.close()
            if manager is not None:
                manager.close()
            if service is not None:
                service.close()
            engine.close()
            errors = [abs(r['relative_error']) for r in rows if 'relative_error' in r]
            atomic_json(store.path/'summary.json', dict(complete=complete, count=len(rows),
                passed=sum(r['passed'] for r in rows), errors=sum('error' in r for r in rows),
                max_absolute_relative_error=max(errors, default=None),
                median_absolute_relative_error=float(np.median(errors)) if errors else None))
    return 0 if complete and all(r['passed'] for r in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
