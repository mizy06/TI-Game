"""Numerical helpers for independent timebase calibration and continuous extrema.

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
