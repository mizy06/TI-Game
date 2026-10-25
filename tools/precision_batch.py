"""Several independently timed ADC blocks; one contest RESULT at completion."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import os
import time
import numpy as np
from tools import host_waveform as host


def _choose_uniform_period(frequencies, clock_hz, count, excluded=()):
    """Choose a uniform timer from measured frequencies, with a small guard.

    Evaluate nearby frequencies too: finite record fits cannot specify an
    infinitely precise phase lattice. No source setting enters this search.
    """
    candidates = np.linspace(min(frequencies), max(frequencies), 9)
    probes = sorted({float(f*(1+delta)) for f in candidates for delta in (-2e-6, 0., 2e-6)})
    samples = np.arange(count)
    best = None
    for ticks in range(1500, 1701):
        if ticks in excluded:
            continue
        gap = 0.
        for frequency in probes:
            phase = np.sort((samples*frequency*ticks/clock_hz) % 1.)
            gap = max(gap, float(np.max(np.diff(np.r_[phase, phase[0]+1.]))))
            if best is not None and gap >= best[0]:
                break
        if best is None or gap < best[0]:
            best = gap, ticks
    return best[1], dict(timer_ticks=best[1], worst_phase_gap_cycles=best[0],
                         probe_frequencies_hz=probes)


def _combine_diverse_duty(values, prepared, results):
    """Downweight sample lattices that leave a square edge unobserved.

    A frame is uniformly timed; its largest uncovered phase gap comes only
    from measured frequencies, known sample rate and sample count. It never
    uses the generator setting. This is a weighting measure, not a certificate
    of absolute accuracy in the presence of interference.
    """
    rates = [m.sample_rate_hz_exact or m.sample_rate_millihz/1000 for _, m, _ in prepared]
    if len(set(rates)) < 2:
        return float(values[:, 2].mean()), None, values[:, 2]
    gaps = []
    frequencies = [result['frequency_hz'] for result in results]
    for (payload, _, _), rate in zip(prepared, rates):
        # A blind lattice can bias its own frequency fit enough to appear
        # well covered. Check every independently measured frequency and keep
        # the worst gap, so that fit cannot award itself excessive weight.
        samples = np.arange(len(payload)//2)
        candidate_gaps = []
        for frequency in frequencies:
            phase = np.sort((samples*frequency/rate) % 1.)
            candidate_gaps.append(float(np.max(np.diff(np.r_[phase, phase[0]+1.]))))
        gaps.append(max(candidate_gaps))
    gaps = np.asarray(gaps)
    weights = 1/np.maximum(gaps, 1/len(prepared[0][0])*2)**2
    weights /= weights.sum()
    selected = float(weights @ values[:, 2])
    reliable = values[weights >= .1*weights.max(), 2]
    return selected, dict(method='inverse_squared_worst_uncovered_phase_gap',
        coverage_frequencies_hz=frequencies,
        phase_gaps_cycles=gaps.tolist(), weights=weights.tolist(),
        duty_fraction=selected), reliable


def _reconcile_joint_vpp(frame_vpps, joint_vpp, joint_relative_rms=None):
    """Reconcile frame consensus with independently measured joint-fit quality."""
    valid_joint = bool(joint_vpp is not None and np.isfinite(joint_vpp) and
                       joint_vpp > 0)
    quality = (float(joint_relative_rms) if joint_relative_rms is not None and
               np.isfinite(joint_relative_rms) and joint_relative_rms >= 0 else None)
    values = np.asarray(frame_vpps, dtype=float)
    values = values[np.isfinite(values) & (values > 0)]
    if not len(values):
        return (float(joint_vpp) if valid_joint else None), dict(accepted=valid_joint,
            selected_method="joint_frame_fit" if valid_joint else "no_valid_estimate", relative_disagreement=None,
            relative_frame_mad=None, agreement_limit=None,
            joint_fit_relative_rms=quality,
            joint_fit_quality_support=False)

    center = float(np.median(values))
    mad = float(np.median(np.abs(values - center))) / center
    disagreement = abs(float(joint_vpp)-center)/center if valid_joint else None
    if disagreement is not None and not np.isfinite(disagreement):
        valid_joint, disagreement = False, None
    limit = max(.0005, 5 * mad)
    joint_fit_quality_limit = .0005
    joint_fit_quality_support = bool(valid_joint and
        quality is not None and quality <= joint_fit_quality_limit)
    accepted = bool(valid_joint and
                    (disagreement <= limit or joint_fit_quality_support))
    return (float(joint_vpp) if accepted else center), dict(
        accepted=accepted,
        selected_method="joint_frame_fit" if accepted else "independent_frame_median",
        independent_frame_median_vpp=center,
        relative_disagreement=disagreement,
        relative_frame_mad=mad,
        agreement_limit=limit,
        joint_fit_relative_rms=quality,
        joint_fit_quality_limit=joint_fit_quality_limit,
        joint_fit_quality_support=joint_fit_quality_support)


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
    records, captures, results, prepared = [], [], [], []
    engine = service.engine
    parallel = engine.parallel_pool is not None and frames > 1
    algorithm_elapsed = algorithm_work = 0.
    sampling_plan = None
    square_sampling = False
    requested_frames = frames

    def prepare_pending():
        for index in range(len(prepared), len(captures)):
            payload, meta, record = captures[index]
            payload, meta = prepare_analysis(payload, meta, record, profile, service.parameters_path)
            record.info['batch_member'] = index+1
            prepared.append((payload, meta, record))

    def analyse_pending():
        nonlocal algorithm_elapsed, algorithm_work
        start = len(results)
        prepare_pending()
        if parallel:
            fitted, elapsed = engine.process_frames(prepared[start:], deadline=deadline, allow_rejected=True)
            results.extend(details for _, details, _ in fitted)
            algorithm_elapsed += elapsed
            algorithm_work += sum(elapsed for _, _, elapsed in fitted)
        else:
            for index in range(start, len(prepared)):
                payload, meta, record = prepared[index]
                _, details, elapsed = engine.process(payload, meta, record, deadline=deadline, emit=False)
                algorithm_elapsed += elapsed
                algorithm_work += elapsed
                results.append(details)

    for index in range(frames):
        capable = (index > 0 and captures[0][1].flags & 8 and
                   callable(getattr(service, 'capture_at_rate', None)))
        if capable and index == 1:
            # Only a waveform with distinct, long plateaus needs additional
            # square-edge phases. Keep the existing R/R/R path for other shapes.
            from tools.square_duty import edge_interval_duty
            prepare_pending()
            _, first_edges = edge_interval_duty(prepared, [])
            square_sampling = bool(first_edges.get('frequency_bounds_hz') and
                                   min(first_edges['plateau_fractions']) >= .97)
        diverse = capable and square_sampling
        command = (b'R', b'S', b'T')[index % 3]
        if diverse and index == 2:
            # Bound the frequency from plateau transitions without waiting for
            # full waveform fits; all final fits can still run in parallel.
            from tools.square_duty import edge_interval_duty
            prepare_pending()
            _, edges = edge_interval_duty(prepared, [])
            if (edges.get('frequency_bounds_hz') and
                    min(edges['plateau_fractions']) >= .9):
                raw_rate = captures[0][1].sample_rate_millihz
                first_ticks = round(32000000000/raw_rate)
                meta = prepared[0][1]
                clock = (meta.sample_rate_hz_exact or meta.sample_rate_millihz/1000)*first_ticks
                ticks, sampling_plan = _choose_uniform_period(
                    edges['frequency_bounds_hz'], clock, len(captures[0][0])//2,
                    excluded=(first_ticks, 1597))
                command = f'U{ticks:04d}\n'.encode('ascii')
        remaining = deadline-time.perf_counter()
        if remaining <= 0:
            raise TimeoutError('R multi-frame deadline exhausted; no RESULT emitted')
        payload, meta, record = (service.capture_at_rate(command, remaining)
                                 if diverse else service.capture(remaining))
        records.append(record)
        captures.append((payload, meta, record))
    if sampling_plan is not None and frames == 3:
        from tools.square_duty import edge_interval_duty
        prepare_pending()
        bounded, edges = edge_interval_duty(prepared, [])
        if bounded is None and edges.get('frequency_bounds_hz') and deadline-time.perf_counter() > 4.5:
            used = [round(32000000000/m.sample_rate_millihz) for _, m, _ in captures]
            ticks, extra_plan = _choose_uniform_period(edges['frequency_bounds_hz'], clock,
                len(captures[0][0])//2, excluded=used)
            payload, meta, record = service.capture_at_rate(f'U{ticks:04d}\n'.encode('ascii'),
                                                           deadline-time.perf_counter())
            records.append(record)
            captures.append((payload, meta, record))
            sampling_plan['additional_frame'] = extra_plan
    frames = len(captures)
    # Each block retains its own time origin; gaps are never concatenated.
    analyse_pending()
    if time.perf_counter() >= deadline:
        raise TimeoutError('R analysis deadline exhausted; no RESULT emitted')
    original_results = results
    original_prepared = prepared
    valid = [i for i, r in enumerate(results) if r.get('ok') and r.get('type') in range(4)
             and all(np.isfinite(r.get(k, np.nan)) for k in
                     ('frequency_hz', 'vpp_v', 'duty_fraction', 'dc_offset_v'))
             and r['frequency_hz'] > 0 and r['vpp_v'] > 0]
    if not valid:
        reasons = [r.get('rejection_reason', f"status={r.get('status')}") for r in results]
        raise ValueError(f'R has no usable frame; source Vpp unavailable: {reasons}')
    groups = {kind: [i for i in valid if results[i]['type'] == kind]
              for kind in {results[i]['type'] for i in valid}}
    def quality(i):
        r = original_results[i]
        return (bool(r.get('fallback')), float(r.get('unified_solver', {}).get('normalized_residual_rms', 1.)))
    selected = min(groups.values(), key=lambda g: (-len(g), min(quality(i) for i in g), g[0]))
    frame_selection = dict(requested_frames=requested_frames, captured_frames=len(captures),
        used_indices=[i+1 for i in selected],
        excluded=[dict(index=i+1, reason=r.get('rejection_reason', 'waveform_disagreement'),
                       status=r.get('status')) for i, r in enumerate(results) if i not in selected],
        fallback_used=len(selected) != len(results), method='majority_waveform_then_fit_quality')
    frame_selection['classification_tied'] = sum(len(g) == len(selected) for g in groups.values()) > 1
    results = [original_results[i] for i in selected]
    prepared = [original_prepared[i] for i in selected]
    kind = results[0]['type']
    fields = ('frequency_hz', 'vpp_v', 'duty_fraction', 'dc_offset_v')
    values = np.array([[r[k] for k in fields] for r in results])
    if len(values) > 1:
        center = np.median(values, axis=0)
        unstable = (np.ptp(values[:, 0])/center[0] > .002 or
                    np.ptp(values[:, 1])/center[1] > .10 or
                    (kind == 1 and np.ptp(values[:, 2]) > .01))
        if unstable:
            # Choose a measured frame near the multiframe consensus, rather
            # than average several changing waveforms into a fictitious one.
            distance = np.abs(values[:, 0]/center[0]-1)+np.abs(values[:, 1]/center[1]-1)
            chosen = min(range(len(values)), key=lambda i: (float(distance[i]), quality(selected[i])))
            frame_selection.update(fallback_used=True, method='nonstationary_consensus_frame',
                                   used_indices=[selected[chosen]+1],
                                   observed_vpp_range_v=[float(values[:, 1].min()), float(values[:, 1].max())])
            frame_selection['excluded'].extend(dict(index=selected[i]+1, reason='nonstationary_batch', status=0)
                                               for i in range(len(values)) if i != chosen)
            results, prepared, values = [results[chosen]], [prepared[chosen]], values[[chosen]]
    frames = len(results)
    mean = values.mean(axis=0)
    batch_duty = None
    reliable_duties = values[:, 2]
    if kind == 1:
        mean[2], batch_duty, reliable_duties = _combine_diverse_duty(values, prepared, results)
        if batch_duty is not None:
            from tools.square_duty import edge_interval_duty
            duty, interval = edge_interval_duty(prepared, values[:, 0])
            batch_duty['edge_intervals'] = interval
            if (duty is None and interval.get('reason') == 'unresolved_edge_intervals' and
                    min(interval['plateau_fractions']) >= .9):
                # A wider interval still supplies a common edge model. Keep
                # its midpoint as the point estimate after the extra capture;
                # replacing it with spectral weights can let one biased frame
                # override all observed transitions. Preserve the unresolved
                # flag and width; this does not certify absolute accuracy.
                duty = interval['midpoint']
                interval['selected_as_point_estimate'] = True
            if duty is not None:
                mean[2] = duty
                batch_duty['duty_fraction'] = duty
    batch_vpp = None
    spectral = [r.get('spectral_vpp', {}) for r in results]
    highly_consistent = (bool(spectral) and all(s.get('accepted') and
        float(s.get('relative_rms',float('inf'))) <= 2e-4 for s in spectral))
    use_joint_vpp = (kind == 3 or not highly_consistent)
    if (frames > 1 and kind in (0, 1, 3) and use_joint_vpp and
            all(meta.vpp_transfer is not None for _, meta, _ in prepared)):
        from tools.vpp_spectral import joint_frame_amplitude
        joint_frames = []
        for (payload, meta, _), details in zip(prepared, results):
            joint_frames.append(dict(samples=np.frombuffer(payload, dtype='<u2'),
                transfer=meta.vpp_transfer, fs=(meta.sample_rate_hz_exact or
                meta.sample_rate_millihz/1000), frequency=details['frequency_hz'],
                dc=details['dc_offset_v'],duty=details['duty_fraction'], rise=details.get('unified_solver',{}).get(
                    'square_rise_fraction',1e-5), fall=details.get('unified_solver',{}).get(
                    'square_fall_fraction',1e-5), vpp=details['vpp_v'],
                background_separation=details.get('spectral_vpp',{}).get('background_separation')))
        try:
            batch_vpp = joint_frame_amplitude(joint_frames, kind)
        except (ValueError, np.linalg.LinAlgError) as error:
            batch_vpp = dict(accepted=False, rejection=str(error))
        if batch_vpp is not None:
            batch_elapsed = float(batch_vpp.get('elapsed_s',0.))
            algorithm_elapsed += batch_elapsed
            algorithm_work += batch_elapsed
            selected_vpp, consensus = _reconcile_joint_vpp(
                values[:, 1], batch_vpp.get('vpp_v'),
                batch_vpp.get('relative_rms') if kind in (1, 3) else None)
            batch_vpp.update(consensus, selected_vpp_v=selected_vpp)
            if selected_vpp is not None:
                mean[1] = selected_vpp
    if time.perf_counter() >= deadline:
        raise TimeoutError('R joint Vpp analysis exceeded deadline; no RESULT emitted')
    if kind == 1 and np.ptp(reliable_duties) > .01:
        mean[2] = float(np.median(reliable_duties))
        frame_selection.update(fallback_used=True, duty_fallback='median_of_observed_duties')
    f, vpp, duty, dc = mean
    line = f'{host.NAMES[kind]},{f:.9f},{vpp:.9f},{duty*100:.7f},{dc:.9f}'
    output = measurement_output(line)
    scatter = np.std(values, axis=0, ddof=1) if frames > 1 else np.zeros(4)
    batch = {'frames': frames, 'output': line, 'display_output': output,
        'frame_selection': frame_selection,
        'fallback_used': bool(frame_selection['fallback_used'] or any(r.get('fallback') for r in results)
                              or (batch_vpp and not batch_vpp.get('accepted'))),
        'fallback_frames': [i+1 for i, r in enumerate(original_results) if r.get('fallback')],
        'model_ambiguous': bool(frame_selection['classification_tied'] or
                               any(r.get('fallback', {}).get('model_ambiguous') for r in results)),
        'method': ('selected actual frame from nonstationary measurements' if
                   frame_selection['method'] == 'nonstationary_consensus_frame' else
                   'joint harmonic fit across independently timed frames' if batch_vpp and
                   batch_vpp.get('accepted') else
                   'mean of independently fitted stationary blocks; gaps not concatenated'),
        'measured': dict(zip(fields, map(float, mean))),
        'vpp_joint_fit': batch_vpp,
        'duty_phase_coverage': batch_duty,
        'duty_sampling_plan': sampling_plan,
        'block_standard_deviation': dict(zip(fields, map(float, scatter))),
        'absolute_accuracy_verified': False,
        'note': 'Block scatter excludes calibration, model bias and coherent replay noise.',
        'captures': [str(r.path.relative_to(service.store.path)) for r in records],
        'used_captures': [str(r.path.relative_to(service.store.path)) for _, _, r in prepared],
        'algorithm_elapsed_s': algorithm_elapsed,
        'algorithm_work_s': algorithm_work,
        'analysis_execution': 'parallel_processes' if parallel else 'serial',
        'analysis_workers': min(engine.parallel_pool.workers, frames) if parallel else 1,
        'elapsed_s': time.perf_counter()-started}
    batch['fallback_reasons'] = (["model_fit"] if batch['fallback_frames'] else [])
    if frame_selection['fallback_used']:
        batch['fallback_reasons'].append('frame_selection')
    if batch_vpp and not batch_vpp.get('accepted'):
        batch['fallback_reasons'].append('joint_amplitude')
    aggregate = dict(results[-1])
    aggregate.update(batch_frames=frames, batch_captures=batch['captures'],
        measurement_sample_count=sum(r['measurement_sample_count'] for r in results),
        classification_sample_count=sum(r['classification_sample_count'] for r in results),
        **batch['measured'])
    aggregate['batch_fallback'] = dict(used=batch['fallback_used'], selection=frame_selection,
                                     fallback_frames=batch['fallback_frames'])
    if batch['fallback_used']:
        aggregate.setdefault('vpp_quality', {}).update(fallback_used=True,
            absolute_accuracy_verified=False, accuracy_0p1_percent_certified=False)
    aggregate.update(frequency_millihz=round(f*1000), vpp_microvolts=round(vpp*1e6),
        duty_ppm=round(duty*1e6))
    if batch_vpp and batch_vpp.get('accepted'):
        aggregate['vpp_joint_fit'] = batch_vpp
        aggregate['vpp_millivolts'] = round(vpp*1000)
        aggregate['vpp_counts_q16'] = round(vpp/(meta.reference_microvolts/
            (meta.adc_full_scale*meta.gain_ppm))*65536)
        aggregate.setdefault('vpp_quality',{}).update(
            meaning='clean_source_under_cross_frame_harmonic_support_assumption',
            independent_reference_used=False,absolute_accuracy_verified=False,
            accuracy_0p1_percent_certified=False)
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
        return line, aggregate, algorithm_elapsed, prepared[-1][1], record
    return batch
