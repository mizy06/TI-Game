"""Read-only comparison of amplitude preprocessing on archived ADC frames.

Uses one common harmonic diagnostic for all methods; it does not replay or
replace the production estimator. Reference setpoints label results only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from tools.amplitude_calibration import voltage_values
from tools.host_waveform import fnv1a
from tools.unified_solver import threadpool_limits
from tools.vpp_preprocessing import harmonic_design, periodic_outlier_weights

NAMES = ('sine', 'square', 'triangle', 'sawtooth')


def diagnostic_vpp(design, y, kind, duty, weights=None):
    if weights is None:
        weights = np.ones(len(y))
    root = np.sqrt(weights)
    beta = np.linalg.lstsq(design*root[:, None], y*root, rcond=None)[0]
    magnitude = abs((beta[1]-1j*beta[2])/2)
    factor = (4., np.pi/max(np.sin(np.pi*duty), .01), np.pi**2/2, 2*np.pi)[kind]
    return float(factor*magnitude)


def analyze_frame(path, label):
    info = json.loads((path/'capture.json').read_text(encoding='utf-8'))
    if info.get('validation') != 'valid':
        raise ValueError(f'unvalidated capture: {path}')
    raw_bytes = (path/'samples.adc.bin').read_bytes()
    metadata = info['metadata']
    if (len(raw_bytes) != info['payload_bytes'] or fnv1a(raw_bytes) != metadata['checksum']):
        raise ValueError(f'capture integrity failure: {path}')
    raw = np.frombuffer(raw_bytes, dtype='<u2')
    if np.any((raw <= 0) | (raw >= 4095)):
        raise ValueError(f'raw ADC saturation: {path}')
    fitted = info.get('algorithm', {})
    analysis_meta = info.get('analysis_metadata', metadata)
    transfer = analysis_meta.get('vpp_transfer')
    y = voltage_values(raw, transfer) if transfer else raw.astype(float)*3.3/4095
    fs = analysis_meta.get('sample_rate_hz_exact') or analysis_meta['sample_rate_millihz']/1000
    reference = info.get('context', {}).get('dynamic_amplitude_reference')
    if reference:
        # period_samples was measured from the ADC data, not the generator.
        kind, frequency, duty = 0, fs/reference['period_samples'], .5
        truth, offset = reference['amplitude_v'], reference['offset_v']
        group = f'calibration:{offset:g}:{truth:g}'
    else:
        kind, frequency, duty = fitted['type'], fitted['frequency_hz'], fitted['duty_fraction'] or .5
        truth, offset = label['vpp_v'], label.get('offset_v', 1.6)
        group = f'{NAMES[kind]}:{truth:g}:{label.get("noise_std_v", 0):g}'
    orders = min(7, int(.4*fs/frequency))
    design = harmonic_design(len(y), fs, frequency, orders)
    weights, diagnostic = periodic_outlier_weights(y, fs, frequency, kind, duty,
                                                    quantization_step=3.3/4095)
    # Ignore filter boundary transients equally in every comparison.
    common = np.ones(len(y))
    common[:5] = common[-5:] = 0
    methods = dict(original=diagnostic_vpp(design, y, kind, duty, common),
        sparse_outliers=diagnostic_vpp(design, y, kind, duty, weights*common),
        median3=diagnostic_vpp(design, median_filter(y, size=3, mode='nearest'), kind, duty, common),
        moving_mean5=diagnostic_vpp(design, uniform_filter1d(y, size=5, mode='nearest'), kind, duty, common))
    root = np.sqrt(common)
    beta = np.linalg.lstsq(design*root[:, None], y*root, rcond=None)[0]
    model = design@beta
    row = dict(capture=str(path), sha256=hashlib.sha256(raw_bytes).hexdigest(),
        group=group, wave=NAMES[kind], reference_vpp_v=truth,
        reference_is_independent_voltmeter=False, fs_hz=fs, frequency_hz=frequency,
        calibration_frame=bool(reference), preprocessing=diagnostic,
        diagnostic_vpp_v=methods,
        relative_changes={name:value/methods['original']-1 for name,value in methods.items()},
        reference_relative_errors={name:value/truth-1 for name,value in methods.items()},
        archived_production_vpp_v=fitted.get('vpp_v'))
    return row, (y, model, fs, diagnostic)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    # Freeze the completed capture inventory before reading an active session.
    inventory = []
    for session in args.session:
        session = session.resolve()
        results = session/'results.jsonl'
        if results.exists():
            for line in results.read_text(encoding='utf-8').splitlines():
                row = json.loads(line)
                batch = row.get('batch')
                if not batch:
                    continue
                label = dict(row, vpp_v=row.get('vpp_v', row.get('configured_vpp_v')))
                for capture in batch['captures']:
                    path = (session/capture).resolve()
                    if not path.is_relative_to(session):
                        raise ValueError('capture path escapes session')
                    inventory.append((path, label))
        else:
            for path in sorted((session/'uart').glob('*/capture.json')):
                try:
                    info = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    continue
                if info.get('validation') == 'valid' and info.get('context', {}).get('dynamic_amplitude_reference'):
                    inventory.append((path.parent, {}))
    rows, skipped, example = [], [], None
    with threadpool_limits(1, 'blas'):
        for path, label in inventory:
            try:
                row, preview = analyze_frame(path, label)
            except (ValueError, KeyError) as error:
                skipped.append(dict(capture=str(path), error=str(error)))
                continue
            rows.append(row)
            if example is None or row['preprocessing']['removed_count'] > example[0]['preprocessing']['removed_count']:
                example = row, preview
    if not rows:
        raise ValueError('no completed, valid frames to compare')
    with (args.output/'frames.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, allow_nan=False)+'\n')
    scatter = []
    for group in sorted({r['group'] for r in rows}):
        group_rows = [r for r in rows if r['group'] == group]
        if len(group_rows) < 3:
            continue
        scatter.append(dict(group=group, frames=len(group_rows),
            relative_stddev={method:float(np.std([r['diagnostic_vpp_v'][method] for r in group_rows], ddof=1)/
                np.mean([r['diagnostic_vpp_v'][method] for r in group_rows]))
                for method in rows[0]['diagnostic_vpp_v']}))
    summary = dict(frames=len(rows), skipped=skipped, source_sessions=[str(p.resolve()) for p in args.session],
        source_files_modified=False, live_algorithm_changed=False, hardware_accessed=False,
        estimator='common least-squares harmonic fundamental diagnostic; production algorithm not replayed',
        frames_with_isolated_outliers=sum(r['preprocessing']['removed_count'] > 0 for r in rows),
        removed_per_frame_median=float(np.median([r['preprocessing']['removed_count'] for r in rows])),
        methods={method:dict(median_relative_change=float(np.median([r['relative_changes'][method] for r in rows])),
            max_absolute_relative_change=max(abs(r['relative_changes'][method]) for r in rows))
            for method in rows[0]['diagnostic_vpp_v']}, groups=scatter)
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2, allow_nan=False), encoding='utf-8')
    if example:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        row, (y, model, fs, info) = example
        residual = (y-model)*1000
        indices = np.asarray(info['removed_indices'], dtype=int)
        center = int(indices[np.argmax(np.abs(residual[indices]))]) if len(indices) else len(y)//2
        start, end = max(0, center-35), min(len(y), center+36)
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), constrained_layout=True)
        t = np.arange(start, end)/fs*1000
        axes[0].plot(t, y[start:end], '.-', label='ADC samples')
        axes[0].plot(t, model[start:end], label='Periodic harmonic model')
        visible = indices[(indices >= start) & (indices < end)]
        axes[0].scatter(visible/fs*1000, y[visible], color='crimson', s=70, facecolors='none', label='Isolated innovations')
        axes[0].set(xlabel='Time (ms)', ylabel='Voltage (V)', title='Recorded ADC samples: sparse outlier diagnostic')
        axes[0].legend()
        for method in ('sparse_outliers', 'median3', 'moving_mean5'):
            axes[1].plot([r['relative_changes'][method]*100 for r in rows], '.', label=method)
        axes[1].axhline(0, color='gray', linewidth=.8)
        axes[1].set(xlabel='Archived frame', ylabel='Vpp change (%)', title='Same harmonic diagnostic before/after preprocessing')
        axes[1].legend()
        fig.savefig(args.output/'comparison.png', dpi=160)
        plt.close(fig)
        (args.output/'example.json').write_text(json.dumps(dict(capture=row['capture'], sha256=row['sha256'],
            start_index=start, end_index=end, preprocessing=info), indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k != 'groups'}, indent=2))
    print('Results:', args.output.resolve())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
