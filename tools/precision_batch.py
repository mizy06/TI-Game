"""Several independently timed ADC blocks; one contest RESULT at completion."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import os
import time
import numpy as np
from tools import host_waveform as host


def prepare_analysis(payload, meta, record, profile, parameters_path):
    if profile is None:
        return payload, meta
    record.info.update(acquisition_calibration=profile.calibration.to_dict(),
        acquisition_calibration_source=profile.source,
        algorithm_parameters_file=str(parameters_path))
    try:
        payload, meta = profile.calibration.apply_exact(payload, meta)
    except Exception as error:
        record.info['calibration_error'] = str(error)
        record.save()
        raise
    exact = ((np.frombuffer(payload, dtype='<u2').astype(float)-meta.adc_offset_counts)
             /meta.adc_scale).astype('<f8').tobytes()
    with (record.path/'samples.calibrated.f64.bin').open('wb') as stream:
        stream.write(exact)
        stream.flush()
        os.fsync(stream.fileno())
    record.info.update(analysis_metadata=asdict(meta), analysis_payload_file='samples.adc.bin',
        calibrated_values_file='samples.calibrated.f64.bin', calibrated_values_encoding='<f8',
        analysis_sha256=hashlib.sha256(payload).hexdigest())
    record.save()
    return payload, meta


def measure_batch(service, frames=2, timeout=15., *, return_result=False):
    """Keep all captures and analyses in one request and one total deadline."""
    if not 1 <= frames <= 8:
        raise ValueError('R frames must be 1..8')
    with service.request_lock:
        return _measure_batch(service, frames, timeout, return_result=return_result)


def _measure_batch(service, frames, timeout, *, return_result):
    """Average stable blocks without inventing sample times across UART gaps."""
    from tools.waveform_server import measurement_output
    started = time.perf_counter()
    deadline = started+timeout
    profile = service.refresh_calibration() if service.use_saved_calibration else None
    records, captures, results = [], [], []
    engine = service.engine
    for index in range(frames):
        remaining = deadline-time.perf_counter()
        if remaining <= 0:
            raise TimeoutError('R multi-frame deadline exhausted; no RESULT emitted')
        payload, meta, record = service.capture(remaining)
        records.append(record)
        captures.append((payload, meta, record))
    # Receive successive blocks before doing any fitting. Each block retains
    # its own time origin; UART transmission gaps are never concatenated.
    algorithm_elapsed = 0.
    parallel = engine.parallel_pool is not None and frames > 1
    if parallel:
        prepared = []
        for index, (payload, meta, record) in enumerate(captures):
            payload, meta = prepare_analysis(payload, meta, record, profile, service.parameters_path)
            record.info['batch_member'] = index+1
            prepared.append((payload, meta, record))
        fitted, algorithm_elapsed = engine.process_frames(prepared, deadline=deadline)
        results = [details for _, details, _ in fitted]
        algorithm_work = sum(elapsed for _, _, elapsed in fitted)
    else:
        for index, (payload, meta, record) in enumerate(captures):
            payload, meta = prepare_analysis(payload, meta, record, profile, service.parameters_path)
            record.info['batch_member'] = index+1
            line, details, elapsed = engine.process(payload, meta, record, deadline=deadline, emit=False)
            algorithm_elapsed += elapsed
            if not details['ok']:
                raise ValueError(f"R block {index+1} rejected: status={details['status']}; no RESULT emitted")
            results.append(details)
        algorithm_work = algorithm_elapsed
    if time.perf_counter() >= deadline:
        raise TimeoutError('R analysis deadline exhausted; no RESULT emitted')
    if len({x['type'] for x in results}) != 1:
        raise ValueError('waveform classification changed between R blocks; no RESULT emitted')
    kind = results[0]['type']
    fields = ('frequency_hz', 'vpp_v', 'duty_fraction', 'dc_offset_v')
    values = np.array([[r[k] for k in fields] for r in results])
    mean = values.mean(axis=0)
    if (np.ptp(values[:, 0])/mean[0] > .002 or
            np.ptp(values[:, 1])/mean[1] > .10 or
            (kind == 1 and np.ptp(values[:, 2]) > .01)):
        raise ValueError('R blocks are not stationary; no RESULT emitted')
    f, vpp, duty, dc = mean
    line = f'{host.NAMES[kind]},{f:.9f},{vpp:.9f},{duty*100:.7f},{dc:.9f}'
    output = measurement_output(line)
    scatter = np.std(values, axis=0, ddof=1) if frames > 1 else np.zeros(4)
    batch = {'frames': frames, 'output': line, 'display_output': output,
        'method': 'mean of independently fitted stationary blocks; gaps not concatenated',
        'measured': dict(zip(fields, map(float, mean))),
        'block_standard_deviation': dict(zip(fields, map(float, scatter))),
        'absolute_accuracy_verified': False,
        'note': 'Block scatter excludes calibration, model bias and coherent replay noise.',
        'captures': [str(r.path.relative_to(service.store.path)) for r in records],
        'algorithm_elapsed_s': algorithm_elapsed,
        'algorithm_work_s': algorithm_work,
        'analysis_execution': 'parallel_processes' if parallel else 'serial',
        'analysis_workers': min(engine.parallel_pool.workers, frames) if parallel else 1,
        'elapsed_s': time.perf_counter()-started}
    aggregate = dict(results[-1])
    aggregate.update(batch_frames=frames, batch_captures=batch['captures'],
        measurement_sample_count=sum(r['measurement_sample_count'] for r in results),
        classification_sample_count=sum(r['classification_sample_count'] for r in results),
        **batch['measured'])
    aggregate.update(frequency_millihz=round(f*1000), vpp_microvolts=round(vpp*1e6),
        duty_ppm=round(duty*1e6))
    for record in records:
        record.info['r_batch'] = batch
        record.save()
    record.info.update(display_output=output, batch_output=line, batch_algorithm=aggregate)
    record.save()
    # The final durable write is inside the contest time limit as well.
    if time.perf_counter() >= deadline:
        batch.update(display_output='', expired_request=True)
        for record in records:
            record.info.update(display_output='', expired_request=True)
            record.save()
        raise TimeoutError('R archival deadline exhausted; no RESULT emitted')
    print(output, file=engine.output, flush=True)
    if return_result:
        return line, aggregate, algorithm_elapsed, meta, record
    return batch
