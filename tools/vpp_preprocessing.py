"""Conservative periodic outlier weights for amplitude experiments.

No raw samples are rewritten. Callers must apply the returned weights to both
the observed data and the fitted source. Not enabled in the live estimator:
isolated spikes, coherent distortion and timing jitter have different causes.
"""
from __future__ import annotations

import numpy as np


def harmonic_design(count, fs, frequency, orders=7):
    t = (np.arange(count)-(count-1)/2)/fs
    angle = 2*np.pi*frequency*t
    return np.column_stack([np.ones(count)]+[
        column for order in range(1, orders+1)
        for column in (np.cos(order*angle), np.sin(order*angle))])


def periodic_outlier_weights(samples, fs, frequency, kind=0, duty=.5, *,
                             orders=7, quantization_step=0.):
    """Identify sparse one/two-sample innovations, protecting waveform knots.

    Fit periodic harmonics and estimate residual scale with MAD. Preserve long
    bursts and repeated phase features, since these can be source distortion.
    This is a sample-admission policy, not a voltage or frequency calibration.
    """
    y = np.asarray(samples, dtype=float)
    if (y.ndim != 1 or len(y) < 128 or not np.isfinite(y).all() or
            not np.isfinite(fs) or not np.isfinite(frequency) or
            not 0 < frequency < fs/12 or kind not in (0, 1, 2, 3) or
            not 1 <= orders <= 15 or not 0 < duty < 1 or
            not np.isfinite(quantization_step) or quantization_step < 0):
        raise ValueError('invalid periodic outlier input')
    n = len(y)
    weights = np.ones(n)
    info = dict(method='sparse_periodic_residual_mask', applied=False,
                sample_count=n, removed_count=0, removed_indices=[],
                edge_protected_count=0, threshold_sigma=6., max_run_samples=2,
                source_samples_modified=False)
    if n*frequency/fs < 32:
        return weights, dict(info, reason='too_few_cycles')
    orders = min(orders, int(.4*fs/frequency))
    design = harmonic_design(n, fs, frequency, orders)
    beta = np.linalg.lstsq(design, y, rcond=None)[0]
    source_first = (-.5j if kind == 0 else
                    -4j/np.pi**2 if kind == 2 else
                    -1j/np.pi if kind == 3 else
                    (1-np.exp(-2j*np.pi*duty))/(1j*np.pi))
    first = (beta[1]-1j*beta[2])/2
    phase = np.angle(first/source_first)/(2*np.pi)
    p = ((np.arange(n)-(n-1)/2)*frequency/fs+phase) % 1
    knots = (0., duty) if kind == 1 else (.25, .75) if kind == 2 else (0.,) if kind == 3 else ()
    protected = np.zeros(n, dtype=bool)
    guard = max(2*frequency/fs, .025)
    for knot in knots:
        protected |= np.abs((p-knot+.5) % 1-.5) <= guard
    # Exclude knots from the pilot objective too: Fourier ringing at a true
    # square edge must not determine what constitutes an ADC impulse.
    pilot_weights = (~protected).astype(float)
    for _ in range(3):
        root = np.sqrt(pilot_weights)
        beta = np.linalg.lstsq(design*root[:, None], y*root, rcond=None)[0]
        residual = y-design@beta
        center = float(np.median(residual[~protected]))
        sigma = max(float(np.median(np.abs(residual[~protected]-center)))/.6744897501960817,
                    quantization_step/np.sqrt(12), 1e-12)
        pilot_weights = np.minimum(1., 3*sigma/np.maximum(np.abs(residual-center), 1e-30))
        pilot_weights[protected] = 0.
    threshold = max(6*sigma, 2*quantization_step)
    candidates = (np.abs(residual-center) > threshold) & ~protected
    changes = np.diff(np.r_[False, candidates, False].astype(int))
    starts, ends = np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)
    selected = np.zeros(n, dtype=bool)
    for start, end in zip(starts, ends):
        if end-start <= 2:
            selected[start:end] = True
    # Repeated phase structure can be real overshoot/ripple. Its removal is a
    # change to the signal definition, so preserve it for a separate model.
    bins = min(512, max(64, int(4*fs/frequency)))
    phase_bins = np.minimum((p*bins).astype(int), bins-1)
    total = np.bincount(phase_bins, minlength=bins)
    hits = np.bincount(phase_bins[selected], minlength=bins)
    recurring = (hits >= 3) & (hits/np.maximum(total, 1) > .15)
    recurring = recurring | np.roll(recurring, 1) | np.roll(recurring, -1)
    selected &= ~recurring[phase_bins]
    info.update(residual_sigma=sigma, threshold=threshold, orders=orders,
        candidate_count=int(candidates.sum()), edge_protected_count=int(protected.sum()),
        recurring_phase_count=int(np.count_nonzero(candidates & recurring[phase_bins])),
        residual_rms_before=float(np.sqrt(np.mean(residual[~protected]**2))))
    if selected.sum() > .01*n:
        return weights, dict(info, reason='not_sparse')
    if not selected.any():
        return weights, dict(info, reason='no_isolated_outliers')
    weights[selected] = 0.
    info.update(applied=True, reason='isolated_periodic_innovations',
                removed_count=int(selected.sum()), removed_indices=np.flatnonzero(selected).tolist())
    return weights, info
