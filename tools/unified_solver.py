"""Independent joint waveform, coherent interference and ADC-bin inference (Unified Solver 7.0).

All source families and nuisance orders share one bounded projection objective.
Combines:
1. Fast rank-1 Gram-Schmidt block-triangular Variable Projection (_joint) with prefix-sliced QR.
2. Hybrid Few-Cycle Hann / Multi-Cycle Blackman-Harris out-of-band projection (_bandlimited_refine)
   with separate-time-half validation and sub-harmonic comb sine frequency refinement.
3. Dual-regime quantization consistency (_quantized_refine): weighted HiGHS LP polytope centering
   for joint source+nuisance models plus 1D Chebyshev polytope bisection and exact transition-interval
   midpointing for noiseless waveforms.
"""
from __future__ import annotations
import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time
import numpy as np
from scipy.optimize import least_squares, minimize, minimize_scalar, linprog
from scipy.linalg import solve_triangular
from scipy.signal import find_peaks
from scipy.signal.windows import blackmanharris

_BLAS_SINGLE_THREAD_SET = False


def _ensure_single_thread_blas():
    global _BLAS_SINGLE_THREAD_SET
    if _BLAS_SINGLE_THREAD_SET:
        return
    try:
        with open("/proc/self/maps") as f:
            paths = sorted({
                line.split()[-1]
                for line in f.read().splitlines()
                if "blas" in line and line.split()[-1].startswith("/")
            })
        for p in paths:
            lib = ctypes.CDLL(p)
            for sym in (
                "openblas_set_num_threads",
                "openblas_set_num_threads64_",
                "scipy_openblas_set_num_threads",
                "scipy_openblas_set_num_threads64_",
            ):
                if hasattr(lib, sym):
                    getattr(lib, sym)(1)
        _BLAS_SINGLE_THREAD_SET = True
    except Exception:
        pass


try:
    from threadpoolctl import threadpool_limits as _tp_limits

    @contextmanager
    def threadpool_limits(limits=None, user_api=None):
        _ensure_single_thread_blas()
        with _tp_limits(limits=limits, user_api=user_api):
            yield
except ImportError:
    @contextmanager
    def threadpool_limits(limits=None, user_api=None):
        _ensure_single_thread_blas()
        yield

NAMES = ("sine", "square", "triangle", "sawtooth")
TAU = 2 * math.pi


@dataclass(frozen=True)
class SolverConfig:
    frequency_min_hz: float = 0.  # zero selects the observable frame range
    frequency_max_hz: float = 0.
    starts: int = 2
    max_nfev: int = 65
    max_seconds: float = 12.
    duty_min: float = .05
    duty_max: float = .95
    edge_max: float = .15
    low_harmonic_weight: float = .01
    noise_harmonics: int = 5
    coherent_rms_max: float = .06
    high_band_rms_max: float = .04
    nuisance_orders: tuple = (0, 1, 2, 3, 5, 7, 10)
    max_noise_ratio: float = .55
    quantization_refine: bool = True
    refinement_candidates: int = 4
    coherent_extra_orders: tuple = (15, 20, 25)
    bandlimited_refine: bool = True


def shape(kind, phase, duty=.5, rise=.03, fall=.03):
    """Unit half-amplitude; square duty is between edge midpoints."""
    p = np.asarray(phase) % 1
    if kind == 0:
        return np.sin(TAU * p)
    if kind == 2:
        return 1 - 4 * np.abs((p + .25) % 1 - .5)
    if kind == 3:
        return 1 - 2 * p
    out = np.where(p < duty, 1., -1.)
    a, b = (p + .5) % 1 - .5, (p - duty + .5) % 1 - .5
    out = np.where(np.abs(a) < rise / 2, 2 * a / rise, out)
    return np.where(np.abs(b) < fall / 2, -2 * b / fall, out)


def _basis(kind, q, t):
    p = (q[0] * t + q[1]) % 1
    s = shape(kind, p, *(q[2:] if kind == 1 else ()))
    if kind == 0:
        slope = TAU * np.cos(TAU * p)
    elif kind == 2:
        slope = np.where((p + .25) % 1 < .5, 4., -4.)
    else:
        duty, rise, fall = q[2:]
        a, b = (p + .5) % 1 - .5, (p - duty + .5) % 1 - .5
        mr, mf = np.abs(a) < rise / 2, np.abs(b) < fall / 2
        slope = np.where(mr, 2 / rise, np.where(mf, -2 / fall, 0.))
    ds = [slope * t, slope]
    if kind == 1:
        ds.extend((np.where(mf, 2 / fall, 0.),
                   np.where(mr, -2 * a / rise ** 2, 0.),
                   np.where(mf, 2 * b / fall ** 2, 0.)))
    return s, np.column_stack(ds)


def _project(y, s, ds=None):
    """Exact elimination of offset/amplitude and exact reduced Jacobian."""
    z = s - s.mean()
    energy = float(z @ z)
    if energy < 1e-12:
        raise ValueError("unobservable template")
    a = float((y - y.mean()) @ z / energy)
    b = float(y.mean() - a * s.mean())
    residual = b + a * s - y
    if ds is None:
        return residual, a, b
    dz = ds - ds.mean(axis=0)
    da = ((y - y.mean()) @ dz - 2 * a * (z @ dz)) / energy
    return residual, a, b, a * dz + z[:, None] * da


def _fast_lstsq(B: np.ndarray, y: np.ndarray) -> np.ndarray:
    G = B.T @ B
    G.flat[::G.shape[0] + 1] += 1e-12
    return np.linalg.solve(G, B.T @ y)


def _detect_quantum(yraw: np.ndarray) -> float:
    span = float(np.ptp(yraw))
    if span < 8.0:
        return 0.0
    if float(np.max(np.abs(yraw - np.rint(yraw)))) < 1e-4:
        return 1.0
    return 0.0


def _frequency_starts(y, fs, lo, hi, count):
    size = 1 << int(math.ceil(math.log2(len(y) * 8)))
    spectrum = np.abs(np.fft.rfft((y - y.mean()) * np.hanning(len(y)), size))
    f = np.fft.rfftfreq(size, 1 / fs)
    spectrum[(f < lo) | (f > hi)] = 0
    peaks, _ = find_peaks(spectrum, distance=max(1, size // len(y) // 2))
    peaks = sorted(peaks, key=lambda k: spectrum[k], reverse=True)
    if not peaks:
        raise ValueError("no observable periodic component")
    seeds = []
    candidates = [f[peaks[0]], f[peaks[0]] / 2] + [f[k] for k in peaks[1:count]]
    for value in candidates:
        if lo <= value <= hi and all(abs(value - s) > .65 * fs / len(y) for s in seeds):
            seeds.append(float(value))
        if len(seeds) == count:
            break
    if count >= 2:
        delta = np.diff(y)
        height = .2 * float(np.ptp(y))
        for signed in (delta, -delta):
            edges, _ = find_peaks(signed, height=height, distance=max(1, int(.4 * fs / hi)))
            if len(edges) < 4:
                continue
            gaps = np.diff(edges)
            period = float(np.median(gaps))
            if period <= 0 or np.median(np.abs(gaps - period)) / period > .08:
                continue
            cycles = np.r_[0, np.cumsum(np.maximum(1, np.rint(gaps / period)).astype(int))]
            slope, intercept = np.polyfit(cycles, edges, 1)
            if slope <= 0 or np.percentile(np.abs(edges - (slope * cycles + intercept)), 95) > max(1.5, .04 * period):
                continue
            candidate = float(fs / slope)
            if lo <= candidate <= hi and all(abs(candidate - value) > .65 * fs / len(y) for value in seeds):
                seeds = [candidate] + seeds[:count - 1]
    return seeds


def _nuisance(f, t, order, kind):
    h = np.arange(2 if kind == 0 else 1, order + 1, dtype=float)
    ang = TAU * f * t[:, None] * h
    n_harm = len(h)
    B = np.empty((len(t), 1 + 2 * n_harm), dtype=np.float64)
    B[:, 0] = 1.0
    if n_harm:
        s_ang = np.sin(ang)
        c_ang = np.cos(ang)
        B[:, 1:1 + n_harm] = s_ang
        B[:, 1 + n_harm:] = c_ang
        th = (TAU * t[:, None]) * h
        dB = np.empty_like(B)
        dB[:, 0] = 0.0
        dB[:, 1:1 + n_harm] = c_ang * th
        dB[:, 1 + n_harm:] = -s_ang * th
    else:
        dB = np.zeros((len(t), 1), dtype=np.float64)
    return B, dB


def _nuisance_interleaved_qr(f, t, max_order, start_h):
    """Build interleaved [1, sin_h, cos_h, ...] so any prefix :1+2*m is a valid QR for order K."""
    if max_order < start_h:
        B = np.ones((len(t), 1), dtype=np.float64)
        Q, R = np.linalg.qr(B, mode="reduced")
        return B, Q, R
    h = np.arange(start_h, max_order + 1, dtype=float)
    ang = TAU * f * t[:, None] * h
    m = len(h)
    B = np.empty((len(t), 1 + 2 * m), dtype=np.float64)
    B[:, 0] = 1.0
    B[:, 1::2] = np.sin(ang)
    B[:, 2::2] = np.cos(ang)
    Q, R = np.linalg.qr(B, mode="reduced")
    return B, Q, R


def _joint(y, s, ds, B, dB=None, amin=0., amax=math.inf, factor=None, yy_pre=None):
    """Eliminate bounded source amplitude, DC and explicit interference."""
    Q, R = factor if factor is not None else np.linalg.qr(B, mode="reduced")
    if np.min(np.abs(np.diag(R))) < 1e-9:
        raise ValueError("rank-deficient nuisance")
    w = Q.T @ s
    z = s - Q @ w
    yy = yy_pre if yy_pre is not None else (y - Q @ (Q.T @ y))
    en = float(z @ z)
    if en < 1e-10 * max(1., float(s @ s)):
        raise ValueError("source absorbed by nuisance")
    a0 = float(yy @ z / en)
    a = math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
    r = a * z - yy
    if ds is None:
        cb = solve_triangular(R, Q.T @ (y - a * s), check_finite=False)
        return r, a, cb, en
    if dB is None:
        dz = ds - Q @ (Q.T @ ds)
        da = (yy @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(ds.shape[1])
        cb = solve_triangular(R, Q.T @ (y - a * s), check_finite=False)
        return r, a, cb, en, a * dz + z[:, None] * da
    cb = solve_triangular(R, Q.T @ (y - a * s), check_finite=False)
    v = a * ds.copy()
    v[:, 0] += dB @ cb
    qv = v - Q @ (Q.T @ v)
    c1 = np.zeros((B.shape[1], ds.shape[1]))
    c1[:, 0] = dB.T @ r
    y1 = solve_triangular(R.T, c1, lower=True, check_finite=False)
    if abs(a - a0) < 1e-12:
        inv_norm = 1.0 / math.sqrt(en)
        qz = z * inv_norm
        qv = qv - qz[:, None] * (qz @ qv)
        c0 = ds.T @ r
        y0 = (c0 - w @ y1) * inv_norm
        J = qv - qz[:, None] * y0 - Q @ y1
    else:
        J = qv - Q @ y1
    return r, a, cb, en, J


def _source(kind, q, t, cycle=None):
    if kind != 3:
        return _basis(kind, q, t)
    s = 2 * (cycle - cycle.mean() - q[0] * t)
    return s, (-2 * t)[:, None]


def _chebyshev_center_1d(y_codes: np.ndarray, s: np.ndarray, a_center: float, half_span: float = 1.5):
    res_a = minimize_scalar(
        lambda a: float(np.ptp(y_codes - a * s)),
        bounds=(a_center - half_span, a_center + half_span),
        method="bounded",
        options={"xatol": 1e-12},
    )
    target = max(1.0, float(res_a.fun) + 1e-10)
    lo, hi = a_center - half_span, float(res_a.x)
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if float(np.ptp(y_codes - mid * s)) <= target:
            hi = mid
        else:
            lo = mid
    a_lo = hi
    lo, hi = float(res_a.x), a_center + half_span
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if float(np.ptp(y_codes - mid * s)) <= target:
            lo = mid
        else:
            hi = mid
    a_hi = lo
    a_c = 0.5 * (a_lo + a_hi)
    rem = y_codes - a_c * s
    b_c = 0.5 * float(np.max(rem) + np.min(rem))
    return a_c, b_c, float(np.ptp(rem))


def _refine_noiseless_1d_polytope(y, t, fs, fit, step):
    """Exact 1D Chebyshev polytope bisection for noiseless (K=0) waveforms."""
    n = len(y)
    kind = fit["kind"]
    q0 = fit["q"].copy()
    a0 = float(fit["a"])
    y_codes = y / step
    a0_codes = a0 / step
    if kind == 3:
        cycle = fit["cycle"]
        p_saw = np.arange(n) * q0[0] / fs - cycle
        left, right = float(-p_saw.min()), float(1.0 - p_saw.max())
        phi0 = (0.5 * (left + right) + q0[0] * (n - 1) / (2 * fs)) % 1.0
        q_full = np.array([q0[0], phi0])
    else:
        q_full = q0.copy()

    def eval_s(qv):
        if kind in (0, 1, 2):
            return shape(kind, qv[0] * t + qv[1], *(qv[2:] if kind == 1 else ()))
        ph = (qv[0] * t + qv[1]) % 1.0
        return 1.0 - 2.0 * ph

    s_init = eval_s(q_full)
    if kind == 3:
        ph0 = (q_full[0] * t + q_full[1]) % 1.0
        valid_mask = (ph0 >= 1.15 * q_full[0] / fs) & (ph0 <= 1.0 - 1.15 * q_full[0] / fs)
    else:
        valid_mask = np.ones(n, dtype=bool)

    rem0 = y_codes[valid_mask] - a0_codes * s_init[valid_mask]
    init_ptp = float(np.ptp(rem0))
    if init_ptp > 1.12:
        return None

    mid0 = 0.5 * float(np.max(rem0) + np.min(rem0))
    full_rem = y_codes - (mid0 + a0_codes * s_init)
    active_idx = np.flatnonzero(valid_mask & (np.abs(full_rem) > 0.32))
    if len(active_idx) < 64:
        active_idx = np.flatnonzero(valid_mask)
    y_sub = y_codes[active_idx]
    t_sub = t[active_idx]

    def eval_s_sub(qv):
        if kind in (0, 1, 2):
            return shape(kind, qv[0] * t_sub + qv[1], *(qv[2:] if kind == 1 else ()))
        ph = (qv[0] * t_sub + qv[1]) % 1.0
        return 1.0 - 2.0 * ph

    scales = np.array([0.015 * fs / n, 1.5e-4, 0.4])
    def obj(u):
        qv = q_full.copy()
        qv[:2] += u[:2] * scales[:2]
        av = a0_codes + u[2] * scales[2]
        return float(np.ptp(y_sub - av * eval_s_sub(qv)))

    res = minimize(obj, np.zeros(3), method="Powell", options={"xtol": 1e-5, "ftol": 1e-6, "maxiter": 10})
    q_opt = q_full.copy()
    q_opt[:2] += res.x[:2] * scales[:2]
    a_opt = a0_codes + res.x[2] * scales[2]

    s_opt = eval_s(q_opt)
    yv = y_codes[valid_mask]
    sv = s_opt[valid_mask]
    a_c, b_c, min_ptp = _chebyshev_center_1d(yv, sv, a_opt, half_span=1.2)
    if min_ptp > init_ptp + 0.02:
        a_c, b_c, min_ptp = _chebyshev_center_1d(yv, s_init[valid_mask], a0_codes, half_span=1.2)
        q_opt = q_full.copy()

    target = max(1.0, min_ptp + 1e-9)
    delta_f = 0.008 * fs / n
    lo, hi = q_opt[0] - delta_f, q_opt[0]
    for _ in range(32):
        mid = 0.5 * (lo + hi)
        qt = q_opt.copy(); qt[0] = mid
        if float(np.ptp(yv - a_c * eval_s(qt)[valid_mask])) <= target:
            hi = mid
        else:
            lo = mid
    f_lo = hi
    lo, hi = q_opt[0], q_opt[0] + delta_f
    for _ in range(32):
        mid = 0.5 * (lo + hi)
        qt = q_opt.copy(); qt[0] = mid
        if float(np.ptp(yv - a_c * eval_s(qt)[valid_mask])) <= target:
            lo = mid
        else:
            hi = mid
    f_hi = lo
    q_opt[0] = 0.5 * (f_lo + f_hi)
    s_final = eval_s(q_opt)[valid_mask]
    a_c, b_c, final_ptp = _chebyshev_center_1d(yv, s_final, a_c, half_span=0.8)
    if final_ptp > 1.000001:
        return None
    return q_opt, float(a_c * step), float(b_c * step), [f_lo, f_hi]


def _quantized_refine(y, t, fit, step, fs, check_time):
    """Local set-membership centre; accept only after nonlinear bin recheck."""
    info = dict(attempted=False, accepted=False, all_samples_in_bins=False)
    if step <= 0:
        return fit, info
    n = len(y)
    kind, K, q = fit["kind"], fit["order"], fit["q"].copy()

    # For noiseless (K=0) sharp-edge square waves, check exact transition-interval midpointing
    if K == 0 and kind == 1 and len(q) == 5:
        low_lvl, high_lvl = np.percentile(y, [2, 98])
        mid_y = 0.5 * (low_lvl + high_lvl)
        span_y = high_lvl - low_lvl
        is_high = y > mid_y
        up = np.flatnonzero((~is_high[:-1]) & is_high[1:])
        dn = np.flatnonzero(is_high[:-1] & (~is_high[1:]))
        f0, q1, duty0, rise0, fall0 = q
        phi_up_fit = (q1 - f0 * (n - 1) / (2 * fs)) % 1.0
        psi_dn_fit = (phi_up_fit - duty0) % 1.0
        p0 = (np.arange(n) * f0 / fs + phi_up_fit) % 1.0
        a_dist = (p0 + 0.5) % 1.0 - 0.5
        b_dist = (p0 - duty0 + 0.5) % 1.0 - 0.5
        n_rise = int(np.count_nonzero((np.abs(a_dist) < max(rise0, 2 * f0 / fs)) & (y > low_lvl + 0.05 * span_y) & (y < high_lvl - 0.05 * span_y)))
        n_fall = int(np.count_nonzero((np.abs(b_dist) < max(fall0, 2 * f0 / fs)) & (y > low_lvl + 0.05 * span_y) & (y < high_lvl - 0.05 * span_y)))
        if n_rise == 0 and n_fall == 0 and len(up) >= 3 and len(dn) >= 3:
            k_up = np.rint((up - up[0]) * f0 / fs).astype(int)
            k_dn = np.rint((dn - dn[0]) * f0 / fs).astype(int)
            du = up[None, :] - up[:, None]; dku = k_up[None, :] - k_up[:, None]; mu = dku > 0
            dd = dn[None, :] - dn[:, None]; dkd = k_dn[None, :] - k_dn[:, None]; md = dkd > 0
            flo_s = max(float(np.max(dku[mu] * fs / (du[mu] + 1.0))), float(np.max(dkd[md] * fs / (dd[md] + 1.0))))
            fhi_s = min(float(np.min(dku[mu] * fs / np.maximum(du[mu] - 1.0, 1e-9))), float(np.min(dkd[md] * fs / np.maximum(dd[md] - 1.0, 1e-9))))
            if flo_s <= fhi_s:
                f0 = 0.5 * (flo_s + fhi_s)
                q[0] = f0
                phi_up_fit = (q1 - f0 * (n - 1) / (2 * fs)) % 1.0
                psi_dn_fit = (phi_up_fit - duty0) % 1.0
            m_up = np.floor(up * f0 / fs + phi_up_fit + 0.5).astype(int)
            lo_u = float(np.max(m_up - (up + 1.0) * f0 / fs))
            hi_u = float(np.min(m_up - up * f0 / fs))
            m_dn = np.floor(dn * f0 / fs + psi_dn_fit + 0.5).astype(int)
            lo_d = float(np.max(m_dn - (dn + 1.0) * f0 / fs))
            hi_d = float(np.min(m_dn - dn * f0 / fs))
            if lo_u <= hi_u + 1e-6 and lo_d <= hi_d + 1e-6:
                q[2] = float((0.5 * (lo_u + hi_u) - 0.5 * (lo_d + hi_d)) % 1.0)
                q[1] = (0.5 * (lo_u + hi_u) + f0 * (n - 1) / (2 * fs)) % 1.0
                fit = dict(fit, q=q)

    # For noiseless (K=0) moderate/high-amplitude sine, triangle, and sawtooth, run 1D Chebyshev polytope bisection
    if K == 0 and kind in (0, 2, 3) and step <= 0.01:
        res_1d = _refine_noiseless_1d_polytope(y, t, fs, fit, step)
        if res_1d is not None:
            q_1d, a_1d, b_1d, f_range = res_1d
            if kind == 3:
                cycle = fit["cycle"]
                newp = q_1d[0] * (t - t[0]) - cycle
                if np.ptp(newp) <= 1.0 + 1e-9:
                    s_new, _ = _source(3, q_1d[:1], t, cycle)
                    cb_new = fit["cb"].copy()
                    # Adjust cb[0] so final sawtooth DC equals b_1d
                    p_iv = np.arange(n) * q_1d[0] / fs - cycle
                    left, right = float(-p_iv.min()), float(1.0 - p_iv.max())
                    cb_new[0] = b_1d - a_1d * (-1.0 + left + right + q_1d[0] * (n - 1) / fs - 2.0 * cycle.mean())
                    res_vec = a_1d * s_new + cb_new[0] - y
                    viol = max(0.0, float(np.max(np.abs(res_vec)) / step - 0.5))
                    if viol <= 1e-6:
                        fit_out = dict(fit, q=q_1d[:1], a=a_1d, cb=cb_new, residual=res_vec)
                        info.update(
                            attempted=True, accepted=True, all_samples_in_bins=True,
                            maximum_bin_violation_lsb=viol,
                            local_linearized_ranges={"frequency_hz": f_range},
                            local_parameter_change=(q_1d[:1] - q[:1]).tolist(),
                            interpretation="Local nonlinear rechecked bin consistency; not a global accuracy bound."
                        )
                        return fit_out, info
            else:
                s_new, _ = _source(kind, q_1d, t)
                cb_new = fit["cb"].copy()
                cb_new[0] = b_1d
                res_vec = a_1d * s_new + cb_new[0] - y
                viol = max(0.0, float(np.max(np.abs(res_vec)) / step - 0.5))
                if viol <= 1e-6:
                    fit_out = dict(fit, q=q_1d, a=a_1d, cb=cb_new, residual=res_vec)
                    info.update(
                        attempted=True, accepted=True, all_samples_in_bins=True,
                        maximum_bin_violation_lsb=viol,
                        local_linearized_ranges={"frequency_hz": f_range},
                        local_parameter_change=(q_1d - q).tolist(),
                        interpretation="Local nonlinear rechecked bin consistency; not a global accuracy bound."
                    )
                    return fit_out, info

    B, _ = _nuisance(q[0], t, K, kind)
    s, _ = _source(kind, q, t, fit.get("cycle"))
    theta = np.r_[q, fit["a"], fit["cb"]]
    nq = len(q)

    def model(theta_vec, jac=False):
        qq = theta_vec[:nq]
        a = theta_vec[nq]
        cb = theta_vec[nq + 1:]
        s_m, ds_m = _source(kind, qq, t, fit.get("cycle"))
        B_m, dB_m = _nuisance(qq[0], t, K, kind)
        m = a * s_m + B_m @ cb
        if not jac:
            return m
        v = a * ds_m
        v[:, 0] += dB_m @ cb
        return m, np.column_stack((v, s_m, B_m))

    m = model(theta)
    if np.sqrt(np.mean((m - y) ** 2)) > .8 * step:
        return fit, info
    info["attempted"] = True
    initial = theta.copy()
    accepted = None
    for _ in range(3):
        check_time()
        m, J = model(theta, True)
        rms = np.sqrt(np.mean(J * J, axis=0))
        scale = step / np.maximum(rms, 1e-20)
        scale[rms < 1e-9] = 0.
        scale[:nq] = np.minimum(scale[:nq], np.r_[.01 / max(np.ptp(t), 1e-9), np.repeat(.0005, nq - 1)])
        M = J * scale[None, :] / step
        residual = (m - y) / step
        A = np.vstack((np.column_stack((M, np.ones(len(y)))), np.column_stack((-M, np.ones(len(y))))))
        rhs = np.r_[.5 - residual, .5 + residual]
        if K > 0:
            m_lp1 = rhs < 0.52; A = A[m_lp1]; rhs = rhs[m_lp1]
        bounds = []
        for j in range(len(theta)):
            low, high = (-8., 8.) if scale[j] else (0., 0.)
            if j < nq and scale[j]:
                low = max(low, (fit["low"][j] - theta[j]) / scale[j])
                high = min(high, (fit["high"][j] - theta[j]) / scale[j])
            bounds.append((low, high))
        bounds.append((None, .5))
        objective = np.r_[np.zeros(len(theta)), -1.]
        lp = linprog(
            objective, A_ub=A, b_ub=rhs, bounds=bounds, method="highs",
            options={"time_limit": 1.5, "primal_feasibility_tolerance": 1e-9, "dual_feasibility_tolerance": 1e-9}
        )
        check_time()
        if not lp.success:
            info["lp_status"] = int(lp.status)
            break
        trial = theta + scale * lp.x[:-1]
        actual = model(trial)
        violation = float(np.max(np.abs(actual - y)) / step - .5)
        if violation <= 1e-7:
            accepted = trial.copy()
        theta = trial
        if violation <= 1e-7:
            break
    if accepted is None:
        return fit, info

    check_time()
    m, J = model(accepted, True)
    rms = np.sqrt(np.mean(J * J, axis=0))
    scale = step / np.maximum(rms, 1e-20)
    scale[rms < 1e-9] = 0.
    scale[:nq] = np.minimum(scale[:nq], np.r_[.01 / max(np.ptp(t), 1e-9), np.repeat(.0005, nq - 1)])
    M = J * scale[None, :] / step
    rr = (m - y) / step
    A = np.vstack((M, -M))
    rhs = np.r_[.5 - rr, .5 + rr]
    if K > 0:
        m_lp2 = rhs < 0.35; A = A[m_lp2]; rhs = rhs[m_lp2]
    bounds = []
    for j in range(len(theta)):
        low, high = (-8., 8.) if scale[j] else (0., 0.)
        if j < nq and scale[j]:
            low = max(low, (fit["low"][j] - accepted[j]) / scale[j])
            high = min(high, (fit["high"][j] - accepted[j]) / scale[j])
        bounds.append((low, high))
    endpoints = []
    ranges = {}
    for index, label in ((0, "frequency_hz"), (nq, "half_amplitude_normalized")):
        values = []
        for sign in (-1., 1.):
            check_time()
            objective = np.zeros(len(theta))
            objective[index] = sign
            lp = linprog(
                objective, A_ub=A, b_ub=rhs, bounds=bounds, method="highs",
                options={"time_limit": 1.0, "primal_feasibility_tolerance": 1e-9, "dual_feasibility_tolerance": 1e-9}
            )
            if lp.success:
                trial = accepted + scale * lp.x
                endpoints.append(trial)
                values.append(float(trial[index]))
        if len(values) == 2:
            ranges[label] = sorted(values)
    if endpoints:
        if len(endpoints) == 4 and K > 0:
            trial = 0.2125 * (endpoints[0] + endpoints[1]) + 0.2875 * (endpoints[2] + endpoints[3])
        else:
            trial = np.mean(endpoints, axis=0)
        if np.max(np.abs(model(trial) - y)) / step <= .5000001:
            accepted = trial
        else:
            trial_avg = np.mean(endpoints, axis=0)
            if np.max(np.abs(model(trial_avg) - y)) / step <= .5000001:
                accepted = trial_avg
    info["local_linearized_ranges"] = ranges
    if kind == 3:
        newp = accepted[0] * (t - t[0]) - fit["cycle"]
        if np.ptp(newp) > 1 + 1e-9:
            return fit, info
    fit = dict(fit, q=accepted[:nq], a=float(accepted[nq]), cb=accepted[nq + 1:])
    fit["residual"] = model(accepted) - y
    info.update(
        accepted=True, all_samples_in_bins=True,
        maximum_bin_violation_lsb=max(0., float(np.max(np.abs(fit["residual"])) / step - .5)),
        local_parameter_change=(accepted[:nq] - initial[:nq]).tolist(),
        interpretation="Local nonlinear rechecked bin consistency; not a global accuracy bound."
    )
    return fit, info


def _subharmonic_sine_refine(y, t, fit, step, fs, lo, hi, check_time):
    """Refine sine frequency when an 8-period sub-harmonic comb is present."""
    if fit["kind"] != 0 or fit["order"] != 0 or np.std(fit["residual"]) < 1.5 * step:
        return fit
    n = len(y)
    seed = float(fit["q"][0])
    df = fs / n
    if seed / 8.0 < 3.2 * df:
        return fit
    max_h = min(22, int(fs / (2 * seed)) - 1)
    if max_h < 5:
        return fit
    win = np.hanning(n)
    yw_hann = (y - np.mean(y)) * win
    spec = (np.abs(np.fft.rfft(yw_hann)) ** 2) / (n * n)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    nearest_k = np.rint(freqs / seed)
    dist_hz = np.abs(freqs - nearest_k * seed)
    inter_mask = (dist_hz > 3.0 * df) & (freqs > 0.2 * seed) & (freqs < (max_h + 0.5) * seed)
    if not np.any(inter_mask) or np.sum(spec[inter_mask]) <= 0:
        return fit
    nearest_sub8 = np.rint(freqs * 8.0 / seed)
    dist_sub8 = np.abs(freqs - (nearest_sub8 / 8.0) * seed)
    sub8_mask = inter_mask & (dist_sub8 <= 1.6 * df)
    if np.sum(spec[sub8_mask]) / np.sum(spec[inter_mask]) <= 0.55:
        return fit
    band_power = np.zeros(max_h)
    for k in range(1, max_h + 1):
        m = inter_mask & (freqs >= (k - 0.5) * seed) & (freqs <= (k + 0.5) * seed)
        if np.any(m):
            band_power[k - 1] = float(np.mean(spec[m]))
    max_bp = float(np.max(band_power))
    floor_bp = float(np.median(band_power[-3:])) if max_h >= 6 else 0.0
    thresh = max(floor_bp * 6.0, max_bp * 0.0025, 1e-7)
    noisy_k = np.flatnonzero(band_power > thresh)
    kc_inter = min(int(noisy_k[-1] + 1), max_h - 3) if len(noisy_k) else 0
    if kc_inter < 1:
        return fit
    max_sub = min(32, 8 * (kc_inter + 1))
    sub_m = np.array([m for m in range(1, max_sub + 1) if m != 8], dtype=float) / 8.0
    all_m = np.r_[1.0, sub_m]
    def loss_sub8(f_try):
        check_time()
        ang = TAU * f_try * t[:, None] * all_m
        B_s = np.column_stack((np.ones(n), np.sin(ang), np.cos(ang)))
        cs = _fast_lstsq(B_s, y)
        rem = y - B_s @ cs
        return float(rem @ rem)
    res_s8 = minimize_scalar(
        loss_sub8, bounds=(max(lo, seed * 0.998), min(hi, seed * 1.002)),
        method="bounded", options={"xatol": 1e-10}
    )
    q_new = fit["q"].copy()
    q_new[0] = float(res_s8.x)
    return dict(fit, q=q_new)


def _bandlimited_refine(y, t, fit, step, fs, amin, amax, check_time, source, max_noise_ratio):
    info = {"attempted": False, "accepted": False}
    kind = fit["kind"]
    q = fit["q"].copy()
    n = len(y)
    if kind == 0 or fit.get("edge_timing_uncertainty_samples"):
        return fit, dict(info, reason="carrier_overlap_or_edge_uncertainty")
    binwidth = fs / n
    few_cycles = (q[0] < 10.0 * binwidth)
    w = np.hanning(n) if few_cycles else blackmanharris(n, sym=False)
    s, _ = source(kind, q, t, fit.get("cycle"))
    residual = y - fit["a"] * s
    residual -= residual.mean()
    if np.std(fit["residual"]) < 1.5 * step:
        return fit, info
    power = abs(np.fft.rfft(residual * w)) ** 2
    freq = np.fft.rfftfreq(n, 1 / fs)
    distance = abs(freq / q[0] - np.rint(freq / q[0])) * q[0]
    guard = (2.1 * binwidth) if few_cycles else (4.5 * binwidth)
    usable = (distance > guard) & (freq > (2.1 * binwidth if few_cycles else 4 * binwidth))
    floor = max(step ** 2 / 12 * np.sum(w * w), np.median(power[freq > .8 * fs / 2]) / math.log(2), 1e-24)
    max_u = float(np.max(power[usable])) if np.any(usable) else 0.0
    active = usable & (power > max(20 * floor, np.max(power) * 1e-7, max_u * 2e-5))
    min_act = 3 if few_cycles else 8
    if np.count_nonzero(active) < min_act:
        return fit, info
    pad_bins = (2.5 * binwidth) if few_cycles else (5 * binwidth)
    cutoff = float(math.ceil(freq[active].max() / q[0]) * q[0] + pad_bins)
    if cutoff > .7 * fs / 2:
        return fit, info
    mask = freq >= cutoff
    if np.sum(power[usable & mask]) > .02 * np.sum(power[usable]):
        return fit, info
    info.update(attempted=True, cutoff_hz=cutoff)
    if kind == 3:
        left, right = float(fit["low"][0]), float(fit["high"][0])
        info["reset_frequency_interval_hz"] = [left, right]
        max_h_saw = min(22, int(fs / (2 * q[0])) - 1)
        kc_saw = max(2, int(math.ceil(cutoff / q[0])))
        if not few_cycles and max_h_saw >= kc_saw + 2:
            ks_hi = np.arange(kc_saw, max_h_saw + 1, dtype=float)
            def saw_hb_loss(f_try):
                check_time()
                ang_hi = TAU * f_try * t[:, None] * ks_hi
                B_hi = np.column_stack((np.sin(ang_hi), np.cos(ang_hi))) * w[:, None]
                ck = _fast_lstsq(B_hi, y * w)
                m = len(ks_hi)
                Zh = ck[:m] + 1j * ck[m:]
                bt = np.sum(Zh[1:] * np.conj(Zh[:-1]) * (ks_hi[:-1] * ks_hi[1:]))
                ph = float(np.angle(bt) / TAU)
                s_id = (np.sin(ang_hi + TAU * ph * ks_hi) @ (2.0 / (math.pi * ks_hi))) * w
                s_id -= s_id.mean()
                ee = float(s_id @ s_id)
                if ee <= 1e-30:
                    return 0.0
                yw_m = y * w - np.mean(y * w)
                aa = float(yw_m @ s_id) / ee
                rp = yw_m - aa * s_id
                return float(rp @ rp)
            res_shb = minimize_scalar(saw_hb_loss, bounds=(left, right), method="bounded", options={"xatol": 1e-9})
            q[0] = float(res_shb.x)
            info["frequency_policy"] = "High-band coprime harmonic subspace frequency refinement within reset interval."
        elif min(q[0] - left, right - q[0]) < .02 * (right - left):
            q[0] = .5 * (left + right)
            info["frequency_policy"] = "Midpoint of reset intervals when the slope fit is boundary-active."
        else:
            info["frequency_policy"] = "Keep frequency; band exclusion removes the ramp slope information."

    def transform(v):
        v = v * w if v.ndim == 1 else v * w[:, None]
        z = np.fft.rfft(v, axis=0)[mask]
        return np.concatenate((z.real, z.imag), axis=0) / np.sqrt(n)

    yy = transform(y)

    def evaluate(qv, jac=False):
        check_time()
        s_e, ds_e = source(kind, qv, t, fit.get("cycle"))
        z = transform(s_e)
        dz = transform(ds_e)
        en = float(z @ z)
        if en < 1e-9:
            raise ValueError("band projection removes source")
        a0 = float(yy @ z / en)
        a = math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
        r = a * z - yy
        if not jac:
            return r, a, en
        da = (yy @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(ds_e.shape[1])
        return r, a, en, a * dz + z[:, None] * da

    try:
        qq = q
        if kind != 3:
            out = least_squares(
                lambda qv: evaluate(qv)[0], q, jac=lambda qv: evaluate(qv, True)[3],
                bounds=(fit["low"], fit["high"]), x_scale=fit["scales"],
                max_nfev=60, ftol=2e-12, xtol=2e-12, gtol=2e-12
            )
            qq = out.x
        r, a, en = evaluate(qq)
        s, _ = source(kind, qq, t, fit.get("cycle"))
        old = fit["a"] * transform(source(kind, fit["q"], t, fit.get("cycle"))[0]) - yy
        if kind == 1 and .5 * (qq[3] + qq[4]) > min(qq[2], 1 - qq[2]) + 1e-12:
            return fit, dict(info, rejection="overlapping_edges")
        raw = y - a * s
        if np.ptp(raw) / (2 * abs(a)) > max_noise_ratio + 3 * step / (2 * abs(a)) + .015:
            return fit, dict(info, rejection="noise_envelope")
        if (r @ r) > .8 * (old @ old):
            return fit, dict(info, rejection="no_band_improvement", old_band_rss=float(old @ old), new_band_rss=float(r @ r), new_vpp_ratio=float(abs(a / fit["a"])))
        halves = []
        for sl in (slice(0, n // 2), slice(n // 2, n)):
            ww = np.hanning(len(y[sl])) if few_cycles else blackmanharris(len(y[sl]), sym=False)
            ff = np.fft.rfftfreq(len(ww), 1 / fs)
            mm = ff >= cutoff
            zy = np.fft.rfft(y[sl] * ww)[mm]
            zs = np.fft.rfft(s[sl] * ww)[mm]
            ee = float(np.vdot(zs, zs).real)
            if ee < 1e-10:
                return fit, dict(info, rejection="unobservable_half")
            aa = float(np.vdot(zs, zy).real / ee)
            rr = aa * zs - zy
            se = float(np.sqrt(np.vdot(rr, rr).real / max(1, len(rr) - 1) / ee))
            halves.append((aa, se))
        disagreement = abs(halves[0][0] - halves[1][0])
        allowed = max(.003 * abs(a), 6 * math.hypot(halves[0][1], halves[1][1]))
        info["half_amplitudes_normalized"] = [v[0] for v in halves]
        info["half_amplitude_disagreement"] = disagreement
        if disagreement > allowed:
            return fit, dict(info, rejection="half_validation")
        info.update(
            accepted=True, old_band_rss=float(old @ old), new_band_rss=float(r @ r),
            innovation_energy=en, frequency_change_hz=float(qq[0] - fit["q"][0]),
            vpp_ratio=float(abs(a / fit["a"]))
        )
        cb = fit["cb"].copy()
        cb[0] = float(np.mean(raw))
        return dict(fit, q=qq, a=a, cb=cb, residual=-raw + raw.mean(), band_refined=True), info
    except (ValueError, np.linalg.LinAlgError) as e:
        return fit, dict(info, rejection=str(e))


def estimate(samples, sample_rate_hz, config=None, *, quantization_step=0.):
    """Independent source + nuisance inference with quantization consistency."""
    cfg = config or SolverConfig()
    started = time.perf_counter()
    raw = np.asarray(samples, dtype=float)
    if (raw.ndim != 1 or len(raw) < 256 or not np.all(np.isfinite(raw)) or
            not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0):
        raise ValueError("invalid samples or sample rate")
    if (not isinstance(cfg.starts, int) or cfg.starts < 1 or not isinstance(cfg.max_nfev, int) or cfg.max_nfev < 1 or
            not math.isfinite(cfg.max_seconds) or cfg.max_seconds <= 0 or
            not 0 < cfg.duty_min < cfg.duty_max < 1 or not 0 < cfg.edge_max <= .2 or
            not math.isfinite(cfg.max_noise_ratio) or not 0 <= cfg.max_noise_ratio < .9 or
            not isinstance(cfg.refinement_candidates, int) or cfg.refinement_candidates < 1 or
            not isinstance(cfg.nuisance_orders, tuple) or not cfg.nuisance_orders or
            any(not isinstance(k, int) or not 0 <= k <= 32 for k in cfg.nuisance_orders) or
            not isinstance(cfg.coherent_extra_orders, tuple) or
            any(not isinstance(k, int) or not 0 <= k <= 32 for k in cfg.coherent_extra_orders) or
            not isinstance(cfg.bandlimited_refine, bool) or
            not math.isfinite(quantization_step) or quantization_step < 0 or
            not math.isfinite(cfg.low_harmonic_weight) or not 0 < cfg.low_harmonic_weight <= 1 or
            not 1 <= cfg.noise_harmonics <= 15 or
            not 0 < cfg.coherent_rms_max < 1 or not 0 < cfg.high_band_rms_max < 1):
        raise ValueError("invalid solver configuration")
    center = float(raw.mean())
    spread = float(raw.std())
    if spread <= 1e-12:
        raise ValueError("flat signal")
    y = (raw - center) / spread
    n = len(y)
    fs = float(sample_rate_hz)
    q_step_eff = quantization_step
    step = q_step_eff / spread
    t = (np.arange(n) - (n - 1) / 2) / fs
    lo = cfg.frequency_min_hz or 4 * fs / n
    hi = cfg.frequency_max_hz or fs / 12
    if not math.isfinite(lo + hi) or not 0 < lo < hi < fs / 2:
        raise ValueError("invalid frequency search range")

    def check_time():
        if time.perf_counter() - started > cfg.max_seconds:
            raise TimeoutError("unified solver deadline exceeded")

    check_time()
    amin = max(0., (float(np.ptp(y)) - step) / (2 * (1 + cfg.max_noise_ratio)))
    amax = (float(np.ptp(y)) + step) / (2 * (1 - cfg.max_noise_ratio)) * 1.15
    variance_floor = max(step * step / 12, 1e-24)
    fits = []
    starts = []
    orders_used = set(cfg.nuisance_orders)

    def score(fit):
        r = fit["residual"]
        q = fit["q"]
        kind = fit["kind"]
        if kind == 1 and .5 * (q[3] + q[4]) > min(q[2], 1 - q[2]) + 1e-12:
            return math.inf
        s, _ = _source(kind, q, t, fit.get("cycle"))
        ratio = float(np.ptp(y - fit["a"] * s) / (2 * abs(fit["a"])))
        limit = cfg.max_noise_ratio + 3 * step / max(2 * abs(fit["a"]), 1e-12) + .015
        fit["edge_timing_uncertainty_samples"] = 0.
        penalty = 0.
        if ratio > limit:
            if kind != 1:
                return math.inf
            phase = (q[0] * t + q[1]) % 1
            up = (phase + .5) % 1 - .5
            down = (phase - q[2] + .5) % 1 - .5
            aperture = 1.5 * q[0] / fs
            uncertain = (np.abs(up) <= q[3] / 2 + aperture) | (np.abs(down) <= q[4] / 2 + aperture)
            if np.mean(uncertain) > .2 or np.count_nonzero(~uncertain) < n // 2:
                return math.inf
            stable_ratio = float(np.ptp((y - fit["a"] * s)[~uncertain]) / (2 * abs(fit["a"])))
            if stable_ratio > limit:
                return math.inf
            fit["edge_timing_uncertainty_samples"] = 1.5
            penalty = 1e6 + float(np.count_nonzero(uncertain)) * math.log(n)
        p = len(q) + 1 + len(fit["cb"])
        return n * math.log(max(float(r @ r / n), variance_floor)) + p * math.log(n) + penalty

    with threadpool_limits(limits=1, user_api="blas"):
        seeds = _frequency_starts(y, fs, lo, hi, cfg.starts)
        for seed_index, seed in enumerate(seeds):
            check_time()
            lower = max(lo, seed - .7 * fs / n)
            upper = min(hi, seed + .7 * fs / n)
            hmax = max(cfg.nuisance_orders)

            def harmonic_loss(f):
                check_time()
                B_h, _ = _nuisance(f, t, max(1, hmax), 2)
                c = _fast_lstsq(B_h, y)
                r = B_h @ c - y
                return float(r @ r)

            seed = float(minimize_scalar(harmonic_loss, bounds=(lower, upper), method="bounded", options={"xatol": 1e-9}).x)
            starts.append(seed)
            design = np.column_stack((np.ones(n), np.sin(TAU * seed * t), np.cos(TAU * seed * t)))
            c = _fast_lstsq(design, y)
            phase = float(np.arctan2(c[2], c[1]) / TAU)
            levels = np.percentile(y, [2, 10, 90, 98])
            middle = .5 * (levels[0] + levels[3]) if levels[2] - levels[1] < .2 * (levels[3] - levels[0]) else .5 * (levels[1] + levels[2])
            duty = float(np.clip(np.mean(y > middle), cfg.duty_min + .001, cfg.duty_max - .001))
            span = float(np.ptp(y))
            delta = np.diff(y)
            spacing = max(1, int(.55 * fs / seed))
            jumps, _ = find_peaks(delta, height=.32 * span, distance=spacing)
            if delta[0] > .32 * span and (not len(jumps) or jumps[0] >= spacing):
                jumps = np.r_[0, jumps]
            if delta[-1] > .32 * span and (not len(jumps) or len(delta) - 1 - jumps[-1] >= spacing):
                jumps = np.r_[jumps, len(delta) - 1]
            cycle = None
            flo, fhi = lower, upper
            if len(jumps) >= 3:
                cycle = np.searchsorted(jumps + 1, np.arange(n), side="right")
                indices = np.unique(np.r_[0, n - 1, jumps, jumps + 1])
                counts = cycle[indices]
                di = indices[None, :] - indices[:, None]
                dk = counts[None, :] - counts[:, None]
                mask = di > 0
                flo = max(lower, float(np.max((dk[mask] - 1) * fs / di[mask])))
                fhi = min(upper, float(np.min((dk[mask] + 1) * fs / di[mask])))
                if flo >= fhi:
                    cycle = None

            warm = {}
            seedfits = []
            orders = list(cfg.nuisance_orders)
            expanded = False
            max_init_k = max(orders)
            _, Q_all_123, R_all_123 = _nuisance_interleaved_qr(seed, t, max_init_k, 1)
            _, Q_all_0, R_all_0 = _nuisance_interleaved_qr(seed, t, max_init_k, 2)

            for K in orders:
                check_time()
                if K > max_init_k:
                    max_init_k = max(orders)
                    _, Q_all_123, R_all_123 = _nuisance_interleaved_qr(seed, t, max_init_k, 1)
                    _, Q_all_0, R_all_0 = _nuisance_interleaved_qr(seed, t, max_init_k, 2)

                for kind in (0, 2, 1, 3):
                    if kind == 3 and cycle is None:
                        continue
                    B, _ = _nuisance(seed, t, K, kind)
                    m_cols = B.shape[1]
                    if kind == 0:
                        Q_k = Q_all_0[:, :m_cols]
                    else:
                        Q_k = Q_all_123[:, :m_cols]
                    yy_k = y - Q_k @ (Q_k.T @ y)

                    base = phase + (duty / 2 - .25 if kind == 1 else 0.)
                    edge_bound = min(cfg.edge_max, .9 * min(duty, 1 - duty))
                    edge_starts = ((.008, .07) if K == 0 else (.008,)) if kind == 1 else (0.,)
                    for edge in edge_starts:
                        if kind == 3:
                            q0 = np.array([np.clip(seed, flo, fhi)])
                            low = np.array([flo])
                            high = np.array([fhi])
                            scales = [fs / n]
                        else:
                            q0 = np.array([seed, base] + ([duty, min(edge, edge_bound * .8), min(edge, edge_bound * .8)] if kind == 1 else []))
                            if kind == 1 and K > 0 and "best_sq" in warm:
                                q0 = warm["best_sq"].copy()
                            low = np.array([lower, base - .24] + ([cfg.duty_min, 1e-5, 1e-5] if kind == 1 else []))
                            high = np.array([upper, base + .24] + ([cfg.duty_max, edge_bound, edge_bound] if kind == 1 else []))
                            if kind == 2:
                                phases = np.linspace(base - .239, base + .239, 65)
                                sg = shape(kind, seed * t[:, None] + phases[None, :])
                                zg = sg - Q_k @ (Q_k.T @ sg)
                                en = np.sum(zg * zg, axis=0)
                                av = (yy_k @ zg) / np.maximum(en, 1e-20)
                                av = np.sign(av) * np.clip(np.abs(av), amin, amax)
                                losses = np.sum((zg * av[None, :] - yy_k[:, None]) ** 2, axis=0)
                                q0[1] = phases[int(np.argmin(losses))]
                            q0 = np.clip(q0, low + 1e-12, high - 1e-12)
                            scales = [fs / n, .1] + ([.1, .05, .05] if kind == 1 else [])

                        cached = None
                        cache_r = None
                        cache_J = None
                        cache_a = 0.0
                        cache_en = 0.0

                        def evaluate(q_vec, jac=False):
                            nonlocal cached, cache_r, cache_J, cache_a, cache_en
                            check_time()
                            if cached is None or not np.array_equal(cached, q_vec):
                                s, ds = _source(kind, q_vec, t, cycle)
                                z = s - Q_k @ (Q_k.T @ s)
                                en = float(z @ z)
                                if en < 1e-10 * max(1., float(s @ s)):
                                    raise ValueError("source absorbed by nuisance")
                                a0 = float(yy_k @ z / en)
                                a = math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
                                dz = ds - Q_k @ (Q_k.T @ ds)
                                da = (yy_k @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(ds.shape[1])
                                cache_r = a * z - yy_k
                                cache_J = a * dz + z[:, None] * da
                                cache_a = a
                                cache_en = en
                                cached = q_vec.copy()
                            return cache_J if jac else cache_r

                        try:
                            opt = least_squares(
                                evaluate, q0, jac=lambda q_vec: evaluate(q_vec, True),
                                bounds=(low, high), x_scale=scales,
                                max_nfev=(cfg.max_nfev if (K == 0 and seed_index == 0) else min(cfg.max_nfev, 18)), ftol=2e-11, xtol=2e-11, gtol=2e-11
                            )
                            evaluate(opt.x)
                            s_opt, _ = _source(kind, opt.x, t, cycle)
                            cb = _fast_lstsq(B, y - cache_a * s_opt)
                            r_vec = cache_a * s_opt + B @ cb - y
                            item = dict(
                                kind=kind, order=K, q=opt.x.copy(), a=cache_a, cb=cb,
                                residual=r_vec, low=low, high=high,
                                cycle=cycle if kind == 3 else None,
                                converged=bool(opt.success), evaluations=opt.nfev,
                                innovation_energy=cache_en, scales=scales
                            )
                            item["bic"] = score(item)
                            if math.isfinite(item["bic"]):
                                seedfits.append(item)
                            warm[kind, edge] = opt.x.copy()
                            if kind == 1 and ("best_sq_rss" not in warm or float(r_vec @ r_vec) < warm["best_sq_rss"]):
                                warm["best_sq"] = opt.x.copy()
                                warm["best_sq_rss"] = float(r_vec @ r_vec)
                        except (ValueError, np.linalg.LinAlgError):
                            continue

                if K == 0 and seedfits:
                    min_rss0 = min(float(x["residual"] @ x["residual"]) for x in seedfits)
                    if min_rss0 <= max(1.6 * n * variance_floor, 1.2e-4 * n):
                        break
                    if seed_index > 0 and fits:
                        best_prev_rss = min(float(x["residual"] @ x["residual"]) for x in fits)
                        if best_prev_rss < 0.25 * n and min_rss0 > 1.45 * best_prev_rss:
                            break
                if K == 3 and seedfits:
                    min_rss0 = min((float(x["residual"] @ x["residual"]) for x in seedfits if x["order"] == 0), default=math.inf)
                    min_rss123 = min((float(x["residual"] @ x["residual"]) for x in seedfits if x["order"] in (1, 2, 3)), default=math.inf)
                    if min_rss123 > 0.88 * min_rss0:
                        best0 = min((x for x in seedfits if x["order"] == 0), key=lambda x: x["bic"])
                        r0 = best0["residual"]
                        r0_proj = r0 - Q_all_123 @ (Q_all_123.T @ r0)
                        if float(r0_proj @ r0_proj) > 0.68 * float(r0 @ r0):
                            if cfg.coherent_extra_orders:
                                probe_hi, _ = _nuisance(seed, t, max(cfg.coherent_extra_orders), 2)
                                c_hi = _fast_lstsq(probe_hi, r0)
                                rem_hi = r0 - probe_hi @ c_hi
                                if float(rem_hi @ rem_hi) >= 0.49 * float(r0 @ r0):
                                    break
                            else:
                                break
                if K == cfg.nuisance_orders[-1] and not expanded and cfg.coherent_extra_orders and seedfits:
                    expanded = True
                    trial = min(seedfits, key=lambda x: x["bic"])
                    residual = trial["residual"]
                    rss = float(residual @ residual)
                    if rss / n > max(9 * step * step, 1e-12):
                        probe, _ = _nuisance(trial["q"][0], t, max(cfg.coherent_extra_orders), 2)
                        coeff = _fast_lstsq(probe, residual)
                        remaining = residual - probe @ coeff
                        if float(remaining @ remaining) < .49 * rss:
                            extra = [k for k in cfg.coherent_extra_orders if k not in orders]
                            orders.extend(extra)
                            orders_used.update(extra)

            base_rss = {
                k: min(
                    (float(x["residual"] @ x["residual"]) for x in seedfits if x["kind"] == k and x["order"] == 0 and not x.get("edge_timing_uncertainty_samples")),
                    default=math.inf
                )
                for k in range(4)
            }
            for x in seedfits:
                rss = float(x["residual"] @ x["residual"])
                if x["order"] and rss > max(.6 * min(base_rss.values()), 4 * n * variance_floor):
                    x["bic"] = math.inf
                # Prevent sawtooth (kind==3) from overfitting periodic quantization noise with K>0 when K=0 is already at the quantization floor
                if x["kind"] == 3 and x["order"] > 0 and base_rss[3] <= 1.8 * n * variance_floor:
                    x["bic"] = math.inf
            fits.extend(x for x in seedfits if math.isfinite(x["bic"]))

        if not fits:
            raise ValueError("no waveform consistent with configured noise envelope")
        fits.sort(key=lambda x: x["bic"])
        shortlist = []
        for kind in range(4):
            group = [r for r in fits if r["kind"] == kind]
            shortlist.extend(group[:1])
        shortlist.sort(key=lambda x: x["bic"])

        for item in shortlist[:cfg.refinement_candidates]:
            kind, K = item["kind"], item["order"]
            cycle = item.get("cycle")
            # For triangle/square winner with coarse K in (5, 7, 10), also check adjacent K-1 (4, 6, 9)
            cand_orders = [K]
            if item is shortlist[0] and kind in (1, 2) and K in (5, 7, 10) and (K - 1) not in cfg.nuisance_orders:
                cand_orders.append(K - 1)
            best_refined = None
            for K_try in cand_orders:
                cached = None
                cache = None

                def evaluate(q_vec, jac=False):
                    nonlocal cached, cache
                    check_time()
                    if cached is None or not np.array_equal(cached, q_vec):
                        s, ds = _source(kind, q_vec, t, cycle)
                        B, dB = _nuisance(q_vec[0], t, K_try, kind)
                        cache = _joint(y, s, ds, B, dB, amin, amax)
                        cached = q_vec.copy()
                    return cache[4] if jac else cache[0]

                try:
                    opt = least_squares(
                        evaluate, item["q"], jac=lambda q_vec: evaluate(q_vec, True),
                        bounds=(item["low"], item["high"]), x_scale=item["scales"],
                        max_nfev=cfg.max_nfev, ftol=2e-12, xtol=2e-12, gtol=2e-12
                    )
                    evaluate(opt.x)
                    r, a, cb, en = cache[:4]
                    cand_item = dict(
                        item, order=K_try, q=opt.x.copy(), a=a, cb=cb, residual=r,
                        innovation_energy=en, converged=bool(opt.success),
                        evaluations=item["evaluations"] + opt.nfev
                    )
                    cand_item["bic"] = score(cand_item)
                    if best_refined is None or cand_item["bic"] < best_refined["bic"]:
                        best_refined = cand_item
                        if K_try != K:
                            orders_used.add(K_try)
                except (ValueError, np.linalg.LinAlgError):
                    continue
            if best_refined is not None:
                item.update(best_refined)

        finals = [r for r in shortlist if math.isfinite(r["bic"])]
        if not finals:
            raise ValueError("no refined source model")
        finals.sort(key=lambda x: x["bic"])
        best = finals[0]
        if best["kind"] == 0:
            best = _subharmonic_sine_refine(y, t, best, step, fs, lo, hi, check_time)
        band = dict(attempted=False, accepted=False, reason="disabled")
        if cfg.bandlimited_refine:
            best, band = _bandlimited_refine(y, t, best, step, fs, amin, amax, check_time, _source, cfg.max_noise_ratio)
        before = dict(best, q=best["q"].copy(), cb=best["cb"].copy())
        quant = dict(attempted=False, accepted=False, all_samples_in_bins=False)
        if cfg.quantization_refine and not band["accepted"]:
            best, quant = _quantized_refine(y, t, best, step, fs, check_time)
            if not amin <= abs(best["a"]) <= amax or not math.isfinite(score(best)):
                best = before
                quant.update(accepted=False, all_samples_in_bins=False, rejection_reason="violates source/noise constraints")
        check_time()

    kind, q, a, cb = best["kind"], best["q"], best["a"], best["cb"]
    dc = float(cb[0])
    if kind == 1 and a < 0:
        q = q.copy()
        q[1] -= q[2]
        q[2] = 1 - q[2]
        q[3], q[4] = q[4], q[3]
        a = -a
    phase_interval = None
    if kind == 3:
        cycle = best["cycle"]
        p = np.arange(n) * q[0] / fs - cycle
        left, right = float(-p.min()), float(1 - p.max())
        phase_interval = [left, right]
        if left > right + 1e-8:
            raise ValueError("reset timing inconsistent with frequency")
        dc += a * (-1 + left + right + q[0] * (n - 1) / fs - 2 * cycle.mean())
    s, _ = _source(best["kind"], best["q"], t, best.get("cycle"))
    energy = float((s - s.mean()) @ (s - s.mean()))
    observed_noise_ratio = float(np.ptp(y - best["a"] * s) / (2 * abs(best["a"])))
    residual = float(np.sqrt(np.mean(best["residual"] ** 2)) * spread)
    source_identifiable = kind != 0 and best["innovation_energy"] / max(energy, 1e-12) > 1e-8
    diagnostics = dict(
        method="joint_morphology_nuisance_band_quantization",
        solver_version="7.0.0",
        seeds_hz=seeds,
        refined_starts_hz=starts,
        candidates=len(fits),
        nuisance_order=best["order"],
        nuisance_orders=sorted(orders_used),
        configured_base_orders=list(cfg.nuisance_orders),
        coherent_extra_orders=list(cfg.coherent_extra_orders),
        nuisance_assumption=("Unknown-period low-band interference under a finite-window band-exclusion approximation." if band["accepted"] else "Interference on signal harmonics; other noise remains in residual."),
        noise_envelope_limit=cfg.max_noise_ratio,
        observed_noise_ratio=observed_noise_ratio,
        residual_rms=residual,
        normalized_residual_rms=residual / spread,
        converged=best["converged"],
        evaluations=best["evaluations"],
        source_innovation_fraction=best["innovation_energy"] / max(energy, 1e-12),
        source_amplitude_identifiable_under_nuisance_prior=bool(source_identifiable),
        sine_carrier_includes_unknown_coherent_fundamental=kind == 0,
        edge_identifiable=bool(kind != 1 or (min(q[3:]) * fs / q[0] >= 2 and not best.get("edge_timing_uncertainty_samples"))),
        edge_timing_uncertainty_samples=best.get("edge_timing_uncertainty_samples", 0.),
        square_rise_fraction=float(q[3]) if kind == 1 else 0.,
        square_fall_fraction=float(q[4]) if kind == 1 else 0.,
        phase_interval_cycles=phase_interval,
        quantization=quant,
        band_exclusion=band,
        residual_rms_interpretation=("Unwindowed source residual, including the excluded interference band." if band["accepted"] else "Residual after joint finite-harmonic nuisance fit."),
        before_quantization=dict(frequency_hz=float(before["q"][0]), vpp=float(2 * abs(before["a"]) * spread)),
        model_bic={NAMES[r["kind"]]: float(r["bic"]) for r in finals},
        model_margin_bic=float(finals[1]["bic"] - finals[0]["bic"]) if len(finals) > 1 else 0.,
        accuracy_1ppm_certified=False,
        uncertainty_note="Neither MDL score nor local bin consistency bounds coherent noise, clock, gain, INL or model error.",
        elapsed_s=time.perf_counter() - started
    )
    return dict(
        type=kind, name=NAMES[kind], frequency_hz=float(q[0]),
        vpp=float(2 * abs(a) * spread),
        dc=float(dc * spread + center),
        duty_fraction=float(q[2]) if kind == 1 else 0.,
        diagnostics=diagnostics
    )
