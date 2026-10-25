"""Fit archived ADC/reference points and verify on fresh independent USB data.

Never publishes algorithm parameters. Source setpoints are confined to this
calibration/validation tool; the measurement algorithm receives only ADC data.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np

from tools.amplitude_calibration import voltage_values,corrected_vpp
from tools.capture_store import CaptureStore,atomic_json
from tools.dynamic_amplitude_calibration import fit_sample_curve,tone_amplitude,_collect
from tools.ro_test import Generator
from tools.vpp_accuracy_usb import snapshot,restore
from tools.waveform_server import Engine,SerialService
from tools.host_waveform import fnv1a


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-report',type=Path,required=True)
    parser.add_argument('--port',default='COM15')
    parser.add_argument('--resource',required=True)
    parser.add_argument('--output',type=Path,default=Path('data/direct-curve-validation'))
    args=parser.parse_args()
    import pyvisa
    import serial
    report=args.reference_report.resolve()
    source=json.loads((report/'amplitude_calibration.json').read_text())
    manager=pyvisa.ResourceManager()
    device=manager.open_resource(args.resource)
    device.timeout=5000
    before=snapshot(device)
    device.close()
    manager.close()
    if source.get('reference_generator','').strip()!=before['*IDN?'].strip():
        raise ValueError('reference generator identity differs; collect new calibration references')
    training=[]
    hashes={}
    for entry in source['observations']:
        if entry['validation']:
            continue
        path=(report.parent/entry['capture']/'samples.adc.bin').resolve()
        if not path.is_relative_to(report.parent):
            raise ValueError('reference capture escapes its session')
        payload=path.read_bytes()
        info=json.loads(path.with_name('capture.json').read_text())
        if (info.get('validation')!='valid' or len(payload)!=info['payload_bytes'] or
                fnv1a(payload)!=info['metadata']['checksum'] or
                fnv1a(payload)!=info['calculated_fnv1a']):
            raise ValueError('reference payload validation/length/checksum mismatch')
        hashes[entry['capture']]=hashlib.sha256(payload).hexdigest()
        training.append(dict(entry,values=np.frombuffer(payload,dtype='<u2').astype(float)))
    curve=fit_sample_curve(training)
    baseline=dict(source['local_transfer'],operating_response=source['operating_response'])
    store=CaptureStore(args.output)
    print(f'Data: {store.path}',flush=True)
    atomic_json(store.path/'curve.json',curve)
    atomic_json(store.path/'generator-before.json',before)
    atomic_json(store.path/'provenance.json',dict(reference_report=str(report),
        reference_generator=before['*IDN?'],reference_capture_sha256=hashes,
        reference_audit_sha256=hashlib.sha256((report/'amplitude_calibration.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        fit_code_sha256=hashlib.sha256(Path(fit_sample_curve.__code__.co_filename).read_bytes()).hexdigest(),
        note='No validation frame enters curve fitting. No parameters are published.'))
    engine=Engine(None,output=io.StringIO(),workers=1)
    service=SerialService(serial.Serial(args.port,115200,timeout=3,write_timeout=3),store,engine)
    audit=dict(complete=False,observations=[],checks=[],error='')
    settings=argparse.Namespace(ro_settle=.25,ro_timeout=14.)
    frequency=267.139
    try:
        with Generator('',transport='visa',resource=args.resource,timeout=10.) as generator:
            def configure(offset,amp):
                generator.reference('sine',offset,amp,frequency)
            for amp in (.93,1.03,1.17,1.33,1.53):
                for offset in (1.49,1.71):
                    blocks=_collect(service,store,settings,generator,audit,store.path/'audit.json',
                                    offset,amp,True,configure)
                    values={}
                    for name,transfer in (('direct_curve',curve),('previous_response',baseline)):
                        fitted=[]
                        for block in blocks:
                            y=voltage_values(block['values'],transfer)
                            span=tone_amplitude(y,block['period_samples'])
                            fitted.append(corrected_vpp(span,float(y.mean()),transfer))
                        values[name]=dict(vpp_v=float(np.mean(fitted)),
                            relative_error=float(np.mean(fitted)/amp-1),
                            block_standard_deviation_v=float(np.std(fitted,ddof=1)))
                    check=dict(reference_vpp_v=amp,offset_v=offset,frequency_hz=frequency,methods=values)
                    audit['checks'].append(check)
                    atomic_json(store.path/'audit.json',audit)
                    print(f'{amp:g} Vpp DC={offset:g}: direct={values["direct_curve"]["relative_error"]*100:+.5f}% '
                          f'previous={values["previous_response"]["relative_error"]*100:+.5f}%',flush=True)
        audit['complete']=True
    except BaseException as error:
        audit['error']=f'{type(error).__name__}: {error}'
        raise
    finally:
        service.close()
        engine.close()
        manager=pyvisa.ResourceManager()
        device=manager.open_resource(args.resource)
        device.timeout=5000
        try:
            restore(device,before)
            atomic_json(store.path/'generator-after.json',snapshot(device))
        finally:
            device.close()
            manager.close()
        atomic_json(store.path/'audit.json',audit)
        methods={}
        for name in ('direct_curve','previous_response'):
            errors=[abs(r['methods'][name]['relative_error']) for r in audit['checks']]
            methods[name]=dict(count=len(errors),passed=sum(e<.001 for e in errors),
                max_absolute_relative_error=max(errors,default=None),
                median_absolute_relative_error=float(np.median(errors)) if errors else None)
        atomic_json(store.path/'summary.json',dict(complete=audit['complete'],error=audit['error'],
            methods=methods,parameters_published=False,reference='generator setpoint; scope unused'))
    return 0 if audit['complete'] and methods['direct_curve']['passed']==10 else 1


if __name__=='__main__':
    raise SystemExit(main())
