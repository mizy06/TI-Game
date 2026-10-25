"""Replay and explicitly activate archived references using the previous Vpp path.

The 50-point data fit only the amplitude/offset response, never a new ADC curve.
Activation records failed independent checks as failures; it does not relax RS.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from tools.acquisition_calibration import (
    CalibrationProfile, calibration_sha256, load_latest_calibration)
from tools.capture_store import atomic_json
from tools.dynamic_amplitude_calibration import (
    calibrated_tone_values, fit_operating_response, tone_amplitude)
from tools.host_waveform import Metadata, analyze, fnv1a
from tools.unified_solver import threadpool_limits


def archived_blocks(report, audit):
    blocks, hashes = [], {}
    for entry in audit['observations']:
        folder = (report.parent/entry['capture']).resolve()
        if not folder.is_relative_to(report.parent):
            raise ValueError('reference capture escapes its session')
        payload = (folder/'samples.adc.bin').read_bytes()
        info = json.loads((folder/'capture.json').read_text(encoding='utf-8'))
        meta = Metadata(**info['metadata'])
        if (info.get('validation') != 'valid' or len(payload) != meta.payload_bytes or
                fnv1a(payload) != meta.checksum or
                fnv1a(payload) != info['calculated_fnv1a']):
            raise ValueError(f'invalid reference capture: {folder}')
        raw = np.frombuffer(payload, dtype='<u2')
        if np.any((raw <= 0) | (raw >= 4095)):
            raise ValueError('reference contains invalid or saturated ADC samples')
        hashes[entry['capture']] = hashlib.sha256(payload).hexdigest()
        blocks.append(dict(entry,values=raw,payload=payload,meta=meta))
    return blocks, hashes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-report',type=Path,required=True)
    parser.add_argument('--local-report',type=Path,required=True)
    parser.add_argument('--parameters',type=Path,default=PROJECT/'algorithm_parameters.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--activate',action='store_true',
                        help='explicitly activate and record the actual validation status')
    args = parser.parse_args(argv)
    report, local_report = args.reference_report.resolve(), args.local_report.resolve()
    audit = json.loads((report/'amplitude_calibration.json').read_text(encoding='utf-8'))
    local_audit = json.loads((local_report/'amplitude_calibration.json').read_text(encoding='utf-8'))
    local = local_audit['local_transfer']
    before = args.parameters.read_bytes()
    profile = load_latest_calibration(args.parameters)
    if profile is None:
        raise ValueError('a valid existing timing/DC calibration is required')
    blocks, hashes = archived_blocks(report,audit)
    training = [o for o in blocks if not o['validation']]
    validation = [o for o in blocks if o['validation']]
    if (len(training) != 200 or len({(o['offset_v'],o['amplitude_v']) for o in training}) != 50 or
            len(validation) != 40):
        raise ValueError('expected 50 four-frame references and 10 four-frame independent checks')
    with threadpool_limits(limits=1,user_api='blas'):
        # Rebuild the response from unfiltered references. Dropping a filter
        # flag alone would retain the old filter's amplitude compensation.
        local = {k:v for k,v in local.items() if k not in ('preprocessing','operating_response')}
        transfer = fit_operating_response(local,training)
    transfer['reference_generator'] = audit['reference_generator']
    transfer['operating_response']['fit_point_count'] = 50
    transfer['operating_response']['fit_capture_count'] = len(training)
    calibration = replace(profile.calibration,vpp_transfer=transfer)
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'parameters-before.json').write_bytes(before)
    atomic_json(args.output/'reference-provenance.json',dict(
        reference_report=str(report),local_report=str(local_report),
        reference_generator=audit['reference_generator'],reference_capture_sha256=hashes,
        reference_audit_sha256=hashlib.sha256((report/'amplitude_calibration.json').read_bytes()).hexdigest(),
        local_audit_sha256=hashlib.sha256((local_report/'amplitude_calibration.json').read_bytes()).hexdigest(),
        validation_samples_used_in_fit=False))
    checks,frames = [],[]
    with threadpool_limits(limits=1,user_api='blas'):
        groups = sorted({(o['amplitude_v'],o['offset_v']) for o in validation})
        for amplitude,offset in groups:
            selected = [o for o in validation if o['amplitude_v']==amplitude and o['offset_v']==offset]
            tones,measured = [],[]
            for block in selected:
                tones.append(tone_amplitude(calibrated_tone_values(block['values'],block['period_samples'],transfer),
                                            block['period_samples']))
                payload,meta = calibration.apply_exact(block['payload'],block['meta'])
                line,details = analyze(None,payload,meta)
                if not details['ok']:
                    raise ValueError(f"validation analysis rejected {block['capture']}: {details}")
                measured.append(details['vpp_v'])
                frames.append(dict(capture=block['capture'],reference_vpp_v=amplitude,
                                   offset_v=offset,line=line,details=details))
            error = float(np.mean(measured)/amplitude-1)
            checks.append(dict(reference_vpp_v=amplitude,offset_v=offset,
                vpp_v=float(np.mean(measured)),relative_error=error,passed=abs(error)<.001,
                reference_tone_relative_error=float(np.mean(tones)/amplitude-1)))
            print(f'{amplitude:g} Vpp, DC={offset:g}: {error*100:+.5f}% '
                  f'{"PASS" if checks[-1]["passed"] else "FAIL"}',flush=True)
            atomic_json(args.output/'checks.json',checks)
    errors = [abs(c['relative_error']) for c in checks]
    verification = dict(points=len(checks),passed=sum(c['passed'] for c in checks),
        max_absolute_relative_error=max(errors),median_absolute_relative_error=float(np.median(errors)),
        all_passed=all(c['passed'] for c in checks),reference='generator setpoint',
        absolute_accuracy_verified=False,validation_samples_used_in_fit=False)
    source = dict(command='RS',calibrated_utc=datetime.now(timezone.utc).isoformat(),
        calibration_report=str((args.output/'activation.json').resolve()),
        calibration_sha256=calibration_sha256(calibration),
        method='previous_local_lookup_and_span_offset_response',preprocessing='none',
        reference_report=str(report),local_report=str(local_report),
        independent_vpp_validation=verification,
        activation='explicit_user_request',previous_source=profile.source)
    candidate = CalibrationProfile(calibration,source)
    atomic_json(args.output/'candidate_parameters.json',candidate.to_dict())
    with (args.output/'validation-frames.jsonl').open('w',encoding='utf-8') as stream:
        for frame in frames:
            stream.write(json.dumps(frame,allow_nan=False)+'\n')
    activation = dict(method=source['method'],preprocessing='none',verification=verification,
        fit_points=50,fit_frames=200,validation_frames=40,
        parameters_published=False,source_curve_used=False,checks=checks,
        activation='explicit_user_request' if args.activate else 'preview')
    atomic_json(args.output/'activation.json',activation)
    if args.activate:
        if args.parameters.read_bytes() != before:
            raise ValueError('parameters changed during replay; refusing to overwrite concurrent work')
        atomic_json(args.parameters,candidate.to_dict())
        if load_latest_calibration(args.parameters) != candidate:
            raise ValueError('saved parameter round-trip failed')
        activation['parameters_published'] = True
        atomic_json(args.output/'activation.json',activation)
    print(json.dumps(verification,ensure_ascii=False),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
