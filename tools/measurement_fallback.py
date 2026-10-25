"""Bounded independent waveform estimates when the strict solver rejects.

Only samples, their timebase and solver limits enter this module. Source
setpoints and sweep labels must never be used to select or fit a model.
"""
from __future__ import annotations

import time
import numpy as np
from scipy.optimize import least_squares, minimize_scalar

from tools.unified_solver import SolverConfig, _frequency_starts, shape, threadpool_limits


def estimate_fallback(samples, fs, config=None, *, max_seconds=1., reason='strict model rejected'):
    cfg = config or SolverConfig()
    started = time.perf_counter()
    deadline = started+max_seconds
    raw = np.asarray(samples, dtype=float)
    if raw.ndim != 1 or len(raw) < 256 or not np.all(np.isfinite(raw)) or not np.isfinite(fs) or fs <= 0:
        raise ValueError('fallback requires finite contiguous samples and a valid timebase')
    center = float(np.mean(raw))
    scale = float(np.std(raw))
    if scale <= 1e-12:
        raise ValueError('fallback cannot infer a periodic source from a flat frame')
    y = (raw-center)/scale
    n = len(y)
    t = (np.arange(n)-(n-1)/2)/fs
    lo = cfg.frequency_min_hz or 4*fs/n
    hi = cfg.frequency_max_hz or fs/12
    if not np.isfinite(lo+hi) or not 0 < lo < hi < fs/2:
        raise ValueError('invalid fallback frequency range')
    candidates = []

    def project(source, mask=None):
        mask = np.ones(n, dtype=bool) if mask is None else mask
        s, v = source[mask], y[mask]
        x = s-s.mean()
        energy = float(x@x)
        if energy < 1e-10:
            raise ValueError('unobservable fallback template')
        a = float(x@(v-v.mean())/energy)
        dc = float(v.mean()-a*s.mean())
        residual = a*source+dc-y
        return a, dc, residual

    with threadpool_limits(limits=1, user_api='blas'):
        seeds = _frequency_starts(y, fs, lo, hi, 3)
        for seed in seeds:
            if candidates and time.perf_counter() >= deadline:
                break
            orders = np.arange(1, min(7, int(.4*fs/seed))+1)

            def harmonic_fit(f, coefficients=False):
                if time.perf_counter() >= deadline:
                    raise TimeoutError('fallback fit budget exhausted')
                angle = 2*np.pi*f*t[:, None]*orders
                design = np.column_stack((np.ones(n), np.sin(angle), np.cos(angle)))
                c = np.linalg.lstsq(design, y, rcond=None)[0]
                residual = y-design@c
                return c if coefficients else float(residual@residual)

            try:
                bounds = (max(lo, seed-.65*fs/n), min(hi, seed+.65*fs/n))
                frequency = float(minimize_scalar(harmonic_fit, bounds=bounds, method='bounded',
                                                 options={'xatol': 1e-7, 'maxiter': 24}).x)
                coeff = harmonic_fit(frequency, True)
            except TimeoutError:
                break
            phase = float(np.arctan2(coeff[1+len(orders)], coeff[1])/(2*np.pi))
            folded = np.floor((frequency*t % 1)*128).astype(int)
            counts = np.bincount(folded, minlength=128)
            profile = np.bincount(folded, weights=y, minlength=128)/np.maximum(counts, 1)
            midpoint = .5*sum(np.quantile(profile[counts > 0], [.05, .95]))
            duty = float(np.clip(np.mean(profile[counts > 0] > midpoint), cfg.duty_min+.001, cfg.duty_max-.001))

            for kind in (0, 2, 1, 3):
                if candidates and time.perf_counter() >= deadline:
                    break
                edge = max(.005, min(.03, .5*min(duty, 1-duty)))
                q = None
                if kind == 0:
                    source = np.sin(2*np.pi*(frequency*t+phase))
                elif kind in (1, 2):
                    initial = [phase] if kind == 2 else [phase-.25+duty/2, duty, edge, edge]
                    lower = [-2.] if kind == 2 else [-2., cfg.duty_min, 1e-4, 1e-4]
                    upper = [2.] if kind == 2 else [2., cfg.duty_max, cfg.edge_max, cfg.edge_max]

                    def residual(q):
                        if time.perf_counter() >= deadline:
                            raise TimeoutError('fallback fit budget exhausted')
                        source = shape(kind, frequency*t+q[0], *(q[1:] if kind == 1 else ()))
                        return project(source)[2]

                    try:
                        opt = least_squares(residual, initial, bounds=(lower, upper),
                                            max_nfev=30, loss='soft_l1', f_scale=.15)
                        q = opt.x
                        source = shape(kind, frequency*t+q[0], *(q[1:] if kind == 1 else ()))
                    except (TimeoutError, ValueError):
                        continue
                else:
                    # Locate the reset on a phase grid. Fit the interior ramp,
                    # so one uncertain reset sample cannot dictate its span.
                    best = None
                    for offset in np.arange(128)/128:
                        s = shape(3, frequency*t+offset)
                        _, _, r = project(s)
                        score = float(np.mean(np.minimum(r*r, 1.)))
                        if best is None or score < best[0]:
                            best = score, offset, s
                    source = best[2]
                    p = (frequency*t+best[1]) % 1
                    mask = np.minimum(p, 1-p) > max(2/128, 1.5*frequency/fs)
                a, dc, residual = project(source, mask if kind == 3 else None)
                score_mask = mask if kind == 3 else np.ones(n, dtype=bool)
                rss = float(np.mean(residual[score_mask]**2))
                span = 2*abs(a)*scale
                observed = float(np.ptp(raw))
                if not np.isfinite(span) or not 0 < span <= 2*observed or rss >= .75:
                    continue
                candidates.append(dict(type=kind, frequency_hz=frequency, vpp=span,
                    duty_fraction=float(q[1]) if kind == 1 else 0., dc=dc*scale+center,
                    score=rss, square_rise_fraction=float(q[2]) if kind == 1 else 0.,
                    square_fall_fraction=float(q[3]) if kind == 1 else 0.))
    if not candidates:
        raise ValueError('no observable periodic fallback model within budget')
    candidates.sort(key=lambda c: c['score'])
    best = candidates[0]
    alternatives = [c for c in candidates[1:] if c['type'] != best['type']]
    margin = ((alternatives[0]['score']-best['score'])/max(best['score'], 1e-12)) if alternatives else None
    best['diagnostics'] = dict(method='bounded_independent_template_fallback', fallback_used=True,
        fallback_reason=reason, normalized_residual_rms=float(np.sqrt(best['score'])),
        model_margin_relative=margin, model_ambiguous=margin is None or margin < .1,
        square_rise_fraction=best['square_rise_fraction'], square_fall_fraction=best['square_fall_fraction'],
        elapsed_s=time.perf_counter()-started, absolute_accuracy_verified=False,
        uncertainty_note='Best available periodic-template estimate; residual and model margin do not certify source amplitude accuracy.')
    return best
