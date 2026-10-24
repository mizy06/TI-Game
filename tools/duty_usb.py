"""Built-in square duty sweep through USB VISA and the real R acquisition path.

Run as ``python -m tools.duty_usb``. Source settings only enter the report;
the measurement engine receives ADC bytes and acquisition metadata alone.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

if os.environ.get('TI_DUTY_PROJECT'):
    sys.path.insert(0, os.environ['TI_DUTY_PROJECT'])
from tools.capture_store import CaptureStore, atomic_json
from tools.precision_batch import measure_batch
from tools.unified_solver import SolverConfig
from tools.waveform_server import Engine, SerialService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM10')
    parser.add_argument('--resource', default='USB0::0xF4EC::0x1101::SDG6XFCC900304::INSTR')
    parser.add_argument('--frequencies', nargs='+', type=float, default=[200.])
    parser.add_argument('--duties', nargs='+', type=float, default=[30., 30.1, 30.5, 40., 40.1, 50.1, 79.9])
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--frames', type=int, default=3)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--vpp', type=float, default=1.4)
    parser.add_argument('--offset', type=float, default=1.5)
    parser.add_argument('--output', type=Path, default=Path('data/duty_usb'))
    parser.add_argument('--parameters', type=Path, default=Path(__file__).resolve().parents[1]/'algorithm_parameters.json')
    args = parser.parse_args()
    import pyvisa
    import serial

    store = CaptureStore(args.output)
    print(f'Data: {store.path}', flush=True)
    engine = Engine(None, config=SolverConfig(max_seconds=10.), workers=args.workers)
    results = []
    service = instrument = manager = None
    complete = False
    atomic_json(store.path/'provenance.json', dict(arguments=vars(args) | {
        'output': str(args.output), 'parameters': str(args.parameters)},
        code_sha256={name: hashlib.sha256((Path(sys.modules[SolverConfig.__module__].__file__).parent/name).read_bytes()).hexdigest()
            for name in ('unified_solver.py', 'precision_batch.py', 'square_duty.py', 'waveform_server.py', 'acquisition_calibration.py')},
        parameters_sha256=hashlib.sha256(args.parameters.read_bytes()).hexdigest(),
        pass_rule='abs(measured-duty)/duty*100 < 0.1 percent (relative duty error)'))
    try:
        engine.warm()
        # Open the board before changing source settings: a busy UART must
        # never silently turn this into an encode-only hardware test.
        port = serial.Serial(args.port, 115200, timeout=3, write_timeout=3)
        service = SerialService(port, store, engine, parameters_path=args.parameters)
        manager = pyvisa.ResourceManager()
        instrument = manager.open_resource(args.resource)
        instrument.timeout = 5000
        atomic_json(store.path/'instrument.json', dict(identity=instrument.query('*IDN?').strip(),
            before=instrument.query('C1:BSWV?').strip(), output=instrument.query('C1:OUTP?').strip()))
        with (store.path/'results.jsonl').open('w', encoding='utf-8', buffering=1) as stream:
            for frequency in args.frequencies:
                for duty in args.duties:
                    instrument.write('C1:OUTP OFF')
                    instrument.write('C1:SRATE MODE,DDS')
                    instrument.write(f'C1:BSWV WVTP,SQUARE,FRQ,{frequency:.12g},AMP,{args.vpp:.12g},OFST,{args.offset:.12g},DUTY,{duty:.12g}')
                    readback = instrument.query('C1:BSWV?').strip()
                    fields = readback.split(',')
                    actual_duty = float(fields[fields.index('DUTY')+1])
                    if abs(actual_duty-duty) > 1e-7:
                        raise ValueError(f'Duty readback differs: {readback}')
                    instrument.write('C1:OUTP ON')
                    time.sleep(.2)
                    for repeat in range(args.repeat):
                        try:
                            batch = measure_batch(service, args.frames, 15.)
                        except Exception as error:
                            result = dict(frequency_hz=frequency, duty_pct=duty, repeat=repeat,
                                source_readback=readback, passed=False,
                                error=f'{type(error).__name__}: {error}')
                            results.append(result)
                            stream.write(json.dumps(result)+'\n')
                            print(f'{frequency:g} Hz duty {duty:g}%: {result["error"]}; FAIL', flush=True)
                            continue
                        measured = batch['measured']['duty_fraction']*100
                        error_pp = abs(measured-duty)
                        result = dict(frequency_hz=frequency, duty_pct=duty, repeat=repeat,
                            source_readback=readback, batch=batch, duty_error_pp=error_pp,
                            duty_relative_error_pct=100*error_pp/duty, passed=100*error_pp/duty < .1)
                        results.append(result)
                        stream.write(json.dumps(result)+'\n')
                        print(f'{frequency:g} Hz duty {duty:g}% -> {measured:.7f}% '
                              f'error {error_pp:.7f} pp / {100*error_pp/duty:.5f}% relative; '
                              f'{"PASS" if result["passed"] else "FAIL"}', flush=True)
        complete = True
    finally:
        if instrument is not None:
            try:
                instrument.write('C1:OUTP OFF')
            finally:
                instrument.close()
        if manager is not None:
            manager.close()
        if service is not None:
            service.close()
        engine.close()
        atomic_json(store.path/'summary.json', dict(complete=complete, count=len(results),
            planned_count=len(args.frequencies)*len(args.duties)*args.repeat,
            passed=sum(x['passed'] for x in results),
            errors=sum('error' in x for x in results),
            maximum_error_pp=max((x['duty_error_pp'] for x in results if 'error' not in x), default=None),
            maximum_relative_error_pct=max((x['duty_relative_error_pct'] for x in results if 'error' not in x), default=None)))
    return 0 if complete and all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
