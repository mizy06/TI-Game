"""Double-precision refinement; input is samples, metadata and blind C seeds only.

No test case, source setting, reference truth or PASS threshold is accepted.
Absolute clock/voltage accuracy is a separate calibration/hardware question.
"""
from __future__ import annotations
from contextlib import nullcontext
import math
import numpy as np
from scipy.optimize import least_squares, minimize_scalar
try:
    from threadpoolctl import threadpool_limits
except ModuleNotFoundError:
    def threadpool_limits(*_args, **_kwargs):
        return nullcontext()

TAU = 2 * math.pi


def harmonic_frequency(values, sample_rate, seed, highest=7):
    """Variable projection: jointly refit DC and all harmonic coefficients at f."""
    values = np.asarray(values, dtype=np.float64)
    t = (np.arange(values.size) - (values.size - 1) / 2) / sample_rate
    h = np.arange(1, highest + 1, dtype=np.float64)
    y = values - values.mean()

    def project(f):
        angles = TAU * f * t[:, None] * h
        s, c_ang = np.sin(angles), np.cos(angles)
        a = np.column_stack((np.ones(len(y)), s, c_ang))
        coef = np.linalg.lstsq(a, y, rcond=None)[0]
        return a, s, c_ang, coef, y - a @ coef

    width = max(.6, seed * .004)
    initial = project(seed)[4]
    fit = minimize_scalar(lambda f: float(np.dot(r := project(f)[4], r)),
        bounds=(seed - width, seed + width), method='bounded',
        options={'xatol': 1e-12, 'maxiter': 80})
    f_best = float(fit.x)

    # Analytical Golub-Pereyra variable-projection Gauss-Newton polish.
    for _ in range(2):
        a, s, c_ang, coef, r = project(f_best)
        dt_h = TAU * t[:, None] * h
        da_df = dt_h * (c_ang * coef[1:1 + highest] - s * coef[1 + highest:])
        j_f = np.sum(da_df, axis=1)
        j_proj = j_f - a @ np.linalg.lstsq(a, j_f, rcond=None)[0]
        denom = float(j_proj @ j_proj)
        if denom > 1e-20:
            step = float(j_proj @ r) / denom
            if abs(step) < 0.05 * width:
                cand = f_best + step
                if float(np.dot(r_cand := project(cand)[4], r_cand)) <= float(r @ r) * (1 + 1e-14):
                    f_best = cand

    _, _, _, coef, final = project(f_best)
    rms = float(np.sqrt(np.mean(final ** 2)))

    # When near the 12-bit ADC quantization floor, polish f by minimizing the
    # maximum quantization bin violation (1D Chebyshev polytope centering).
    if rms <= 0.35 and values.size >= 1024:
        q_width = min(0.002 * width, 0.05 * sample_rate / values.size)
        def cheb_violation(f):
            ang = TAU * f * t[:, None] * h
            a_q = np.column_stack((np.ones(len(values)), np.sin(ang), np.cos(ang)))
            c_q = np.linalg.lstsq(a_q, values, rcond=None)[0]
            return float(np.max(np.abs(values - a_q @ c_q)))
        base_v = cheb_violation(f_best)
        q_fit = minimize_scalar(cheb_violation,
            bounds=(f_best - q_width, f_best + q_width), method='bounded',
            options={'xatol': 1e-13, 'maxiter': 40})
        if q_fit.success and float(q_fit.fun) <= base_v + 1e-12:
            cand_r = project(float(q_fit.x))[4]
            if float(cand_r @ cand_r) <= float(final @ final) * (1 + 1e-6):
                f_best = float(q_fit.x)
                _, _, _, coef, final = project(f_best)
                rms = float(np.sqrt(np.mean(final ** 2)))

    accepted = (fit.success and abs(f_best - seed) < .98 * width and
                float(final @ final) <= float(initial @ initial) * (1 + 1e-9))
    fund_vpp = 2.0 * float(np.hypot(coef[1], coef[1 + highest]))
    return float(f_best if accepted else seed), {
        'method': 'joint_harmonic_variable_projection', 'accepted': bool(accepted),
        'seed_hz': float(seed), 'residual_rms_counts': rms,
        'fundamental_vpp_counts': fund_vpp,
        'harmonics': int(highest)}


def refine_timing(values, sample_rate, seed, kind, duty):
    from tools.envelope_measurement import _shape
    values = np.asarray(values, dtype=np.float64)
    with threadpool_limits(limits=1, user_api='blas'):
        if kind == 3:
            # Within each ramp, the ratio of slope to reset height identifies
            # frequency without guessing the unobserved reset phase.
            span = float(np.percentile(values, 98)-np.percentile(values, 2))
            delta = np.diff(values)
            jumps = np.flatnonzero(np.abs(delta) > .55*span)
            expected = values.size*seed/sample_rate
            accepted = False
            if len(jumps) > .7*expected:
                sign = np.sign(np.median(delta[jumps]))
                jumps = jumps[delta[jumps]*sign > .55*span]
                jumps = jumps[np.r_[True, np.diff(jumps) > .5*sample_rate/seed]]
                if .7*expected < len(jumps) < 1.3*expected:
                    cycle = np.zeros(len(values))
                    cycle[jumps+1] = 1
                    cycle = np.cumsum(cycle)
                    index = np.arange(len(values))-(len(values)-1)/2
                    a = np.column_stack((np.ones(len(values)), index, cycle-cycle.mean()))
                    c = np.linalg.lstsq(a, values, rcond=None)[0]
                    r = values-a@c
                    f = float(-sample_rate*c[1]/c[2])
                    accepted = (abs(f/seed-1) < .002 and
                        np.std(r) < .06*span and abs(c[2]) > .7*span)
            return f if accepted else seed, duty, {'method': 'ramp_slope_reset_ratio',
                'accepted': bool(accepted), 'seed_hz': float(seed),
                'reset_count': int(len(jumps))}
        f, diagnostic = harmonic_frequency(values, sample_rate, seed)
        t = (np.arange(values.size)-(values.size-1)/2)/sample_rate
        y = (values-values.mean()) / max(float(np.std(values)), 1.)
        first = np.column_stack((np.ones(len(y)), np.sin(TAU*f*t), np.cos(TAU*f*t)))
        c = np.linalg.lstsq(first, y, rcond=None)[0]
        phase = float(np.arctan2(c[2], c[1])/TAU)
        if kind == 0:
            return f, duty, diagnostic
        if kind == 1:
            phase += duty/2-.25
            x = [f, phase, duty, .03, .03]
            lower = [f-.4, phase-.10, .10, 1e-6, 1e-6]
            upper = [f+.4, phase+.10, .90, .15, .15]
        else:
            x = [f, phase]
            lower, upper = [f-.4, phase-.1], [f+.4, phase+.1]
        def residual(q):
            d, rise, fall = (q[2], q[3], q[4]) if kind == 1 else (.5, 0., 0.)
            shape = _shape(kind, q[0]*t+q[1], d, rise, fall)
            a = np.column_stack((np.ones(len(y)), shape))
            c = np.linalg.lstsq(a, y, rcond=None)[0]
            return y-a@c
        fit = least_squares(residual, x, bounds=(lower, upper),
            x_scale=[1., .01, .02, .03, .03] if kind == 1 else [1., .01],
            ftol=1e-10, xtol=1e-11, gtol=1e-10, max_nfev=45)
        # Validate on the samples, not a guessed source specification.
        r = residual(fit.x)
        accepted = bool(fit.success and abs(fit.x[0]-f) < .39 and
                        float(np.std(r)) < .28)
        diagnostic.update(template_accepted=accepted,
            template_residual_fraction=float(np.std(r)), evaluations=int(fit.nfev))
        if accepted:
            f = float(fit.x[0])
            if kind == 1:
                duty = float(fit.x[2])
                diagnostic.update(square_rise_fraction=float(fit.x[3]),
                    square_fall_fraction=float(fit.x[4]),
                    phase_at_sample_zero=float(fit.x[1]-f*(len(values)-1)/(2*sample_rate)))
                # A step with no samples on its ramp has an interval, not a
                # uniquely recoverable sub-sample edge. Extra decimals do not
                # resolve that interval.
                diagnostic['edge_identifiable'] = bool(min(fit.x[3:])*sample_rate/f > 2)
        return f, duty, diagnostic


def continuous_extrema(function, period, knots=()):
    """Locate smooth extrema and one-sided values at all piecewise knots."""
    grid = np.arange(4096)*period/4096
    y = np.asarray(function(grid), dtype=float)
    candidates = [float(y.min()), float(y.max())]
    for sign in (-1., 1.):
        z = sign*y
        indices = np.flatnonzero(((z < np.roll(z, 1)) & (z <= np.roll(z, -1))) |
                                ((z <= np.roll(z, 1)) & (z < np.roll(z, -1))))
        for i in indices:
            center = grid[i]
            fit = minimize_scalar(lambda x: sign*float(function(np.array([x % period]))[0]),
                bounds=(center-period/4096, center+period/4096), method='bounded',
                options={'xatol': period*1e-11})
            candidates.append(sign*float(fit.fun))
    for phase in knots:
        at = (phase % 1)*period
        candidates.extend(map(float, function(np.array([(at-period*1e-11)%period,
            at, (at+period*1e-11)%period]))))
    return min(candidates), max(candidates)
