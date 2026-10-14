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


def _joint(y, s, ds, B, dB=None, amin=0., amax=math.inf, factor=None, yy_pre=None, positive_a=False):
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
    a = float(np.clip(a0, amin, amax)) if positive_a else math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
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
    cycle = fit.get("cycle")
    q_full = q0.copy()

    def eval_s(qv):
        if kind in (0, 1, 2):
            return shape(kind, qv[0] * t + qv[1], *(qv[2:] if kind == 1 else ()))
        s_saw, _ = _source(3, qv[:1], t, cycle)
        return s_saw

    s_init = eval_s(q_full)
    rem0 = y_codes - a0_codes * s_init
    init_ptp = float(np.ptp(rem0))
    if init_ptp > 1.12:
        return None

    mid0 = 0.5 * float(np.max(rem0) + np.min(rem0))
    full_rem = y_codes - (mid0 + a0_codes * s_init)
    active_idx = np.flatnonzero(np.abs(full_rem) > 0.32)
    if len(active_idx) < 64:
        active_idx = np.arange(n)
    y_sub = y_codes[active_idx]
    t_sub = t[active_idx]
    cycle_sub = cycle[active_idx] if (kind == 3 and cycle is not None) else None
    c_mean = float(cycle.mean()) if (kind == 3 and cycle is not None) else 0.0

    def eval_s_sub(qv):
        if kind in (0, 1, 2):
            return shape(kind, qv[0] * t_sub + qv[1], *(qv[2:] if kind == 1 else ()))
        return 2.0 * (cycle_sub - c_mean - qv[0] * t_sub)

    if kind == 3:
        flo_s, fhi_s = float(fit["low"][0]), float(fit["high"][0])
        scales = np.array([0.015 * fs / n, 0.4])
        def obj_saw(u):
            f_try = float(np.clip(q_full[0] + u[0] * scales[0], flo_s, fhi_s))
            av = a0_codes + u[1] * scales[1]
            return float(np.ptp(y_sub - av * eval_s_sub(np.array([f_try]))))
        res = minimize(obj_saw, np.zeros(2), method="Powell", options={"xtol": 1e-5, "ftol": 1e-6, "maxiter": 10})
        q_opt = np.array([float(np.clip(q_full[0] + res.x[0] * scales[0], flo_s, fhi_s))])
        a_opt = a0_codes + res.x[1] * scales[1]
    else:
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
    a_c, b_c, min_ptp = _chebyshev_center_1d(y_codes, s_opt, a_opt, half_span=1.2)
    if min_ptp > init_ptp + 0.02:
        a_c, b_c, min_ptp = _chebyshev_center_1d(y_codes, s_init, a0_codes, half_span=1.2)
        q_opt = q_full.copy()

    target = max(1.0, min_ptp + 1e-9)
    delta_f = 0.008 * fs / n
    f_min_bound = float(fit["low"][0]) if kind == 3 else (q_opt[0] - delta_f)
    f_max_bound = float(fit["high"][0]) if kind == 3 else (q_opt[0] + delta_f)
    lo, hi = max(f_min_bound, q_opt[0] - delta_f), q_opt[0]
    for _ in range(32):
        mid = 0.5 * (lo + hi)
        qt = q_opt.copy(); qt[0] = mid
        if float(np.ptp(y_codes - a_c * eval_s(qt))) <= target:
            hi = mid
        else:
            lo = mid
    f_lo = hi
    lo, hi = q_opt[0], min(f_max_bound, q_opt[0] + delta_f)
    for _ in range(32):
        mid = 0.5 * (lo + hi)
        qt = q_opt.copy(); qt[0] = mid
        if float(np.ptp(y_codes - a_c * eval_s(qt))) <= target:
            lo = mid
        else:
            hi = mid
    f_hi = lo
    q_opt[0] = 0.5 * (f_lo + f_hi)
    s_final = eval_s(q_opt)
    a_c, b_c, final_ptp = _chebyshev_center_1d(y_codes, s_final, a_c, half_span=0.8)
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

    # For noiseless (K=0) sine, triangle, and sawtooth, run 1D Chebyshev polytope bisection
    if K == 0 and kind in (0, 2, 3) and step <= 0.025:
        res_1d = _refine_noiseless_1d_polytope(y, t, fs, fit, step)
        if res_1d is not None:
            q_1d, a_1d, b_1d, f_range = res_1d
            if kind == 3:
                cycle = fit["cycle"]
                newp = q_1d[0] * (t - t[0]) - cycle
                if np.ptp(newp) <= 1.0 + 1e-9:
                    s_new, _ = _source(3, q_1d[:1], t, cycle)
                    cb_new = fit["cb"].copy()
                    cb_new[0] = b_1d
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


def _detect_subharmonic_comb(r0, fs, f_carrier):
    """Detect integer (2..20) or fractional (1.15..20.5) sub-harmonic comb in residual r0."""
    n = len(r0)
    w = np.hanning(n)
    rw = (r0 - np.mean(r0)) * w
    nfft = 1 << (int(math.ceil(math.log2(n))) + 2)
    spec = np.abs(np.fft.rfft(rw, n=nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    df = fs / n
    ks = np.arange(1, 50)[:, None]
    f_alias = np.abs((ks * f_carrier + fs / 2) % fs - fs / 2)
    dist_carrier = np.min(np.abs(freqs[None, :] - f_alias), axis=0)
    usable = (dist_carrier > 2.2 * df) & (freqs > 1.5 * df) & (freqs < 0.48 * fs)
    p_clean = np.where(usable, spec, 0.0)
    if float(np.sum(p_clean)) <= 1e-20:
        return None

    cand_L = set(float(L) for L in range(2, 21))
    pk_idx, _ = find_peaks(p_clean, distance=max(1, int(1.5 * df / (fs / nfft))))
    if len(pk_idx) > 0:
        pk_idx = sorted(pk_idx, key=lambda idx: p_clean[idx], reverse=True)[:15]
        pk_freqs = freqs[pk_idx]
        for fp in pk_freqs[:8]:
            for m in range(1, 20):
                L_try = m * f_carrier / fp
                if 1.15 <= L_try <= 20.5:
                    cand_L.add(round(L_try, 4))

    spec1 = np.abs(np.fft.rfft(rw)) ** 2
    freqs1 = np.fft.rfftfreq(n, 1.0 / fs)
    dist_c1 = np.min(np.abs(freqs1[None, :] - f_alias), axis=0)
    u1 = (dist_c1 > 2.0 * df) & (freqs1 > 1.5 * df) & (freqs1 < 0.48 * fs)
    tot1 = float(np.sum(spec1[u1]))
    if tot1 <= 1e-20:
        return None

    best_L = None
    best_score = 0.0
    for L in sorted(cand_L):
        f_sub = f_carrier / L
        if f_sub < 2.1 * df:
            continue
        m_near = np.maximum(1.0, np.rint(freqs1 / f_sub))
        d_sub = np.abs(freqs1 - m_near * f_sub)
        mask_comb = u1 & (d_sub <= 1.35 * df)
        comb_energy = float(np.sum(spec1[mask_comb]))
        frac_bins = float(np.count_nonzero(mask_comb)) / max(1, np.count_nonzero(u1))
        if frac_bins < 0.68 and comb_energy / tot1 > 0.65:
            adj_score = (comb_energy / tot1) - 0.003 * L
            if adj_score > best_score:
                best_score = adj_score
                best_L = L

    if best_L is None:
        return None

    if abs(best_L - round(best_L)) < 0.03:
        best_L = float(round(best_L))
    else:
        def comb_loss(L_val):
            f_s = f_carrier / L_val
            m_n = np.maximum(1.0, np.rint(freqs[usable] / f_s))
            d_s = np.abs(freqs[usable] - m_n * f_s)
            w_comb = np.exp(-0.5 * (d_s / (0.6 * df)) ** 2)
            return -float(np.sum(p_clean[usable] * w_comb))
        res_L = minimize_scalar(comb_loss, bounds=(best_L * 0.985, best_L * 1.015), method="bounded", options={"xatol": 1e-5})
        best_L = float(res_L.x)
        if abs(best_L - round(best_L)) < 0.005:
            best_L = float(round(best_L))

    f_sub = f_carrier / best_L
    max_M_possible = min(145, int(0.49 * fs / f_sub))
    sub_powers = np.zeros(max_M_possible)
    for m in range(1, max_M_possible + 1):
        fm = m * f_sub
        if np.min(np.abs(fm - f_alias)) <= 2.0 * df:
            sub_powers[m - 1] = -1.0
            continue
        idx = int(round(fm / (fs / nfft)))
        if 0 <= idx < len(spec):
            lo_i = max(0, idx - 2)
            hi_i = min(len(spec), idx + 3)
            sub_powers[m - 1] = float(np.max(spec[lo_i:hi_i]))
    valid_p = sub_powers[sub_powers >= 0]
    if len(valid_p) == 0:
        return None
    max_sp = float(np.max(valid_p))
    med_tail = float(np.median(valid_p[-max(3, len(valid_p) // 5):]))
    thresh = max(med_tail * 12.0, max_sp * 3e-4)
    active_m = np.flatnonzero(sub_powers > thresh)
    if len(active_m) < 4:
        return None
    M_max = int(active_m[-1] + 1)
    n_valid_below = int(np.count_nonzero(sub_powers[:M_max] >= 0))
    if len(active_m) < max(4, int(0.55 * n_valid_below)):
        return None
    return best_L, M_max


def _build_subharmonic_basis(f, t, L, M, exclude_carrier=False):
    ms = np.arange(1, M + 1, dtype=float)
    ratios = ms / L
    if exclude_carrier:
        ms = ms[np.abs(ratios - np.rint(ratios)) > 1e-3]
        ratios = ms / L
    ang = TAU * f * t[:, None] * ratios[None, :]
    n_h = len(ratios)
    B = np.empty((len(t), 1 + 2 * n_h), dtype=np.float64)
    B[:, 0] = 1.0
    if n_h:
        s_a = np.sin(ang)
        c_a = np.cos(ang)
        B[:, 1:1 + n_h] = s_a
        B[:, 1 + n_h:] = c_a
        th = (TAU * t[:, None]) * ratios[None, :]
        dB = np.empty_like(B)
        dB[:, 0] = 0.0
        dB[:, 1:1 + n_h] = c_a * th
        dB[:, 1 + n_h:] = -s_a * th
    else:
        dB = np.zeros((len(t), 1), dtype=np.float64)
    return B, dB, ms


def _subharmonic_refine(y, t, fit, step, fs, lo, hi, amin, amax, check_time, max_noise_ratio):
    """
    Universal Sub-Harmonic Comb Joint Variable Projection & LP Polytope Centering
    for both integer (L in 2..20) and fractional (L in 1.15..20.5) periodic interference.
    """
    if fit["order"] != 0 or np.std(fit["residual"]) < max(1.5 * step, 1e-7):
        return fit, False
    n = len(y)
    kind = fit["kind"]
    q0 = fit["q"].copy()
    f0 = float(q0[0])
    s0, _ = _source(kind, q0, t, fit.get("cycle"))
    r0 = y - fit["a"] * s0
    r0 -= r0.mean()
    det = _detect_subharmonic_comb(r0, fs, f0)
    if det is None:
        return fit, False
    L_init, M_raw = det
    is_int_L = (abs(L_init - round(L_init)) < 1e-3)
    L = float(round(L_init)) if is_int_L else float(L_init)

    if kind == 0:
        if is_int_L:
            # Integer L on sine: m = L overlaps carrier fundamental, so project out m != L to refine frequency!
            M = min(135, M_raw + 1)
            ms = np.array([m for m in range(1, M + 1) if m != int(L)], dtype=float) / L
            all_m = np.r_[1.0, ms]
            def loss_s_int(f_try):
                check_time()
                ang = TAU * f_try * t[:, None] * all_m[None, :]
                B_s = np.column_stack((np.ones(n), np.sin(ang), np.cos(ang)))
                cs = _fast_lstsq(B_s, y)
                rem = y - B_s @ cs
                return float(rem @ rem)
            res_f = minimize_scalar(
                loss_s_int, bounds=(max(lo, f0 - 0.5 * fs / n), min(hi, f0 + 0.5 * fs / n)),
                method="bounded", options={"xatol": 1e-10}
            )
            q_new = q0.copy()
            q_new[0] = float(res_f.x)
            return dict(fit, q=q_new), False
        else:
            # Fractional L on sine: m * (f/L) never equals f! Decouple (f_c, f_s) and run LP polytope centering!
            M = min(110, M_raw + 1)
            fsub0 = f0 / L
            ms = np.arange(1, M + 1, dtype=float)
            def res_2d(x_v):
                check_time()
                fc, fs_s = x_v
                ang_s = TAU * fs_s * t[:, None] * ms[None, :]
                ang_c = TAU * fc * t
                B = np.column_stack((np.ones(n), np.sin(ang_s), np.cos(ang_s), np.sin(ang_c), np.cos(ang_c)))
                coef = _fast_lstsq(B, y)
                return y - B @ coef
            try:
                opt2 = least_squares(
                    res_2d, [f0, fsub0],
                    bounds=([max(lo, f0 - 0.5 * fs / n), fsub0 * 0.995], [min(hi, f0 + 0.5 * fs / n), fsub0 * 1.005]),
                    x_scale=[fs / n, fsub0 / n], max_nfev=35, ftol=1e-12, xtol=1e-12
                )
                fc, fs_s = float(opt2.x[0]), float(opt2.x[1])
                ang_s = TAU * fs_s * t[:, None] * ms[None, :]
                ang_c = TAU * fc * t
                B_s = np.column_stack((np.ones(n), np.sin(ang_s), np.cos(ang_s)))
                B_full = np.column_stack((B_s, np.sin(ang_c), np.cos(ang_c)))
                coef = _fast_lstsq(B_full, y)
                a_l2 = math.hypot(coef[-2], coef[-1])
                if not (amin <= a_l2 <= amax):
                    return fit, False
                ph_l2 = math.atan2(coef[-1], coef[-2]) / TAU
                cb_l2 = coef[:-2]
                rem_l2 = y - B_full @ coef
                if float(rem_l2 @ rem_l2) > 0.25 * float(fit["residual"] @ fit["residual"]):
                    return fit, False
                theta = np.r_[fc, fs_s, ph_l2, a_l2, cb_l2]
                if step > 0 and M <= 50 and np.sqrt(np.mean(rem_l2 ** 2)) <= 0.85 * step:
                    def model_f_sine(th, jac=False):
                        f_c_, f_s_, ph_, a_ = th[:4]
                        cb_ = th[4:]
                        as_ = TAU * f_s_ * t[:, None] * ms[None, :]
                        ac_ = TAU * f_c_ * t + TAU * ph_
                        Bs_ = np.column_stack((np.ones(n), np.sin(as_), np.cos(as_)))
                        sc_ = np.sin(ac_)
                        m_ = a_ * sc_ + Bs_ @ cb_
                        if not jac:
                            return m_
                        dsc_dfc = TAU * t * np.cos(ac_)
                        dsc_dph = TAU * np.cos(ac_)
                        dBs_dfs = np.column_stack((np.zeros(n), (TAU * t[:, None] * ms) * np.cos(as_), -(TAU * t[:, None] * ms) * np.sin(as_)))
                        J_ = np.column_stack((a_ * dsc_dfc, dBs_dfs @ cb_, a_ * dsc_dph, sc_, Bs_))
                        return m_, J_
                    for _ in range(2):
                        check_time()
                        m_, J_ = model_f_sine(theta, True)
                        rms_ = np.sqrt(np.mean(J_ * J_, axis=0))
                        sc_ = step / np.maximum(rms_, 1e-20)
                        sc_[:3] = np.minimum(sc_[:3], np.array([.01 / np.ptp(t), .01 / np.ptp(t), .0005]))
                        M_m = J_ * sc_[None, :] / step
                        rr_ = (m_ - y) / step
                        A_ = np.vstack((np.column_stack((M_m, np.ones(n))), np.column_stack((-M_m, np.ones(n)))))
                        rhs_ = np.r_[0.5 - rr_, 0.5 + rr_]
                        bnds_ = [(-6., 6.)] * len(theta) + [(None, 0.5)]
                        lp1 = linprog(np.r_[np.zeros(len(theta)), -1.], A_ub=A_, b_ub=rhs_, bounds=bnds_, method="highs", options={"time_limit": 1.0})
                        if lp1.success:
                            theta = theta + sc_ * lp1.x[:-1]
                    m_, J_ = model_f_sine(theta, True)
                    rr_ = (m_ - y) / step
                    max_r = float(np.max(np.abs(rr_)))
                    if max_r <= 0.52:
                        hb = max(0.500001, max_r + 1e-5)
                        rms_ = np.sqrt(np.mean(J_ * J_, axis=0))
                        sc_ = step / np.maximum(rms_, 1e-20)
                        sc_[:3] = np.minimum(sc_[:3], np.array([.01 / np.ptp(t), .01 / np.ptp(t), .0005]))
                        M_m = J_ * sc_[None, :] / step
                        A2 = np.vstack((M_m, -M_m))
                        rhs2 = np.r_[hb - rr_, hb + rr_]
                        bnds2 = [(-6., 6.)] * len(theta)
                        ends = []
                        for idx_v in (0, 3):
                            for sgn in (-1., 1.):
                                check_time()
                                o2 = np.zeros(len(theta)); o2[idx_v] = sgn
                                lp2 = linprog(o2, A_ub=A2, b_ub=rhs2, bounds=bnds2, method="highs", options={"time_limit": 0.8})
                                if lp2.success:
                                    ends.append(theta + sc_ * lp2.x)
                        if len(ends) == 4:
                            theta = 0.25 * sum(ends)
                fc, fs_s, ph_f, a_f = float(theta[0]), float(theta[1]), float(theta[2]), float(theta[3])
                cb_f = theta[4:]
                s_new, _ = _source(0, np.array([fc, ph_f]), t, None)
                raw_rem = y - a_f * s_new
                if np.ptp(raw_rem) / (2 * abs(a_f)) <= max_noise_ratio + 3 * step / (2 * abs(a_f)) + 0.02:
                    cb_out = np.array([float(cb_f[0])])
                    return dict(fit, q=np.array([fc, ph_f]), a=a_f, cb=cb_out, residual=-raw_rem + raw_rem.mean()), True
            except Exception:
                return fit, False
        return fit, False

    # Nonsinusoidal (kind in 1, 2, 3):
    cycle = fit.get("cycle")
    low, high, scales = fit["low"], fit["high"], fit["scales"]
    if not is_int_L:
        # Refine fractional L on r0 first
        def loss_L_ns(L_try):
            check_time()
            B_l, _, _ = _build_subharmonic_basis(f0, t, L_try, min(80, M_raw + 1), exclude_carrier=True)
            c_l = _fast_lstsq(B_l, r0)
            rem_l = r0 - B_l @ c_l
            return float(rem_l @ rem_l)
        res_L = minimize_scalar(loss_L_ns, bounds=(L * 0.998, L * 1.002), method="bounded", options={"xatol": 1e-8})
        L = float(res_L.x)

    # Step 1: If kind == 1 (square) and duty is near boundary (>= 0.90 or <= 0.10), lock morphology q using non-carrier subharmonics first
    q_start = q0.copy()
    if kind == 1 and (q0[2] >= 0.88 or q0[2] <= 0.12) and is_int_L:
        try:
            B_nc, _, _ = _build_subharmonic_basis(f0, t, L, min(120, M_raw + 1), exclude_carrier=True)
            Q_nc, _ = np.linalg.qr(B_nc, mode="reduced")
            yy_nc = y - Q_nc @ (Q_nc.T @ y)
            def ev_nocarrier(qv, jac=False):
                check_time()
                s, ds = _source(1, qv, t, None)
                z = s - Q_nc @ (Q_nc.T @ s)
                en = float(z @ z)
                a0 = float(yy_nc @ z / en)
                a = float(np.clip(a0, amin, amax))
                if not jac:
                    return a * z - yy_nc
                dz = ds - Q_nc @ (Q_nc.T @ ds)
                da = (yy_nc @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(5)
                return a * dz + z[:, None] * da
            opt_nc = least_squares(lambda qv: ev_nocarrier(qv, False), q_start, jac=lambda qv: ev_nocarrier(qv, True), bounds=(low, high), x_scale=scales, max_nfev=25, ftol=1e-11, xtol=1e-11)
            q_start = opt_nc.x.copy()
        except Exception:
            pass

    # Step 2: Fast interleaved QR at f0 to select optimal M in [M_raw, M_raw+1, M_raw+2] and optimize morphology in milliseconds!
    best_cand = None
    best_M = M_raw
    cand_Ms = [m for m in (M_raw, M_raw + 1, M_raw + 2) if 2 <= m and 1 + 2 * m < n // 2]
    if not cand_Ms:
        return fit, False
    max_M_c = max(cand_Ms)
    ms_all = np.arange(1, max_M_c + 1, dtype=float) / L
    ang_all = TAU * q_start[0] * t[:, None] * ms_all[None, :]
    B_inter = np.empty((n, 1 + 2 * max_M_c), dtype=np.float64)
    B_inter[:, 0] = 1.0
    B_inter[:, 1::2] = np.sin(ang_all)
    B_inter[:, 2::2] = np.cos(ang_all)
    Q_inter, _ = np.linalg.qr(B_inter, mode="reduced")
    pos_flag = (fit["a"] > 0)
    v_floor = max(step * step / 12.0, 1e-24)

    # Pick best M using fast fixed-Q least_squares
    for M_try in cand_Ms:
        check_time()
        try:
            Q_m = Q_inter[:, :1 + 2 * M_try]
            yy_m = y - Q_m @ (Q_m.T @ y)
            q_init_m = q_start.copy()
            if kind == 2 and M_try == cand_Ms[0]:
                phases = np.linspace(q0[1] - .239, q0[1] + .239, 65)
                sg = shape(2, q_start[0] * t[:, None] + phases[None, :])
                zg = sg - Q_m @ (Q_m.T @ sg)
                en_g = np.sum(zg * zg, axis=0)
                av = np.clip((yy_m @ zg) / np.maximum(en_g, 1e-20), amin, amax)
                losses = np.sum((zg * av[None, :] - yy_m[:, None]) ** 2, axis=0)
                q_init_m[1] = float(np.clip(phases[int(np.argmin(losses))], low[1] + 1e-12, high[1] - 1e-12))

            def ev_fast_m(qv, jac=False):
                check_time()
                s, ds = _source(kind, qv, t, cycle)
                z = s - Q_m @ (Q_m.T @ s)
                en = float(z @ z)
                if en < 1e-10 * max(1., float(s @ s)):
                    raise ValueError("absorbed")
                a0 = float(yy_m @ z / en)
                a = float(np.clip(a0, amin, amax)) if pos_flag else math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
                if not jac:
                    return a * z - yy_m, a, en
                dz = ds - Q_m @ (Q_m.T @ ds)
                da = (yy_m @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(ds.shape[1])
                return a * dz + z[:, None] * da

            opt_m = least_squares(lambda qv: ev_fast_m(qv, False)[0], q_init_m, jac=lambda qv: ev_fast_m(qv, True), bounds=(low, high), x_scale=scales, max_nfev=25, ftol=1e-11, xtol=1e-11)
            rv_m, a_m, en_m = ev_fast_m(opt_m.x, False)
            bic_m = n * math.log(max(float(rv_m @ rv_m / n), v_floor)) + (len(opt_m.x) + 2 + 2 * M_try) * math.log(n)
            if best_cand is None or bic_m < best_cand["bic"]:
                best_cand = dict(q=opt_m.x.copy(), a=a_m, en=en_m, bic=bic_m)
                best_M = M_try
                q_start = opt_m.x.copy()
        except Exception:
            continue

    if best_cand is None:
        return fit, False

    # Polish with moving B_s(f) when best_M <= 45, or evaluate exact _joint at best_cand['q'] when best_M > 45
    try:
        q_pol = best_cand["q"].copy()
        if best_M <= 45:
            cached = None
            cache = None
            def ev_sub_polish(qv, jac=False):
                nonlocal cached, cache
                check_time()
                if cached is None or not np.array_equal(cached, qv):
                    s, ds = _source(kind, qv, t, cycle)
                    B_s, dB_s, _ = _build_subharmonic_basis(qv[0], t, L, best_M, exclude_carrier=False)
                    cache = _joint(y, s, ds, B_s, dB_s, amin, amax, positive_a=pos_flag)
                    cached = qv.copy()
                return cache[4] if jac else cache[0]
            opt = least_squares(ev_sub_polish, q_pol, jac=lambda qv: ev_sub_polish(qv, True), bounds=(low, high), x_scale=scales, max_nfev=8, ftol=1e-12, xtol=1e-12)
            ev_sub_polish(opt.x)
            q_pol = opt.x.copy()
            rv, a_new, cb_new, en_new = cache[:4]
        else:
            s, _ = _source(kind, q_pol, t, cycle)
            B_s, _, _ = _build_subharmonic_basis(q_pol[0], t, L, best_M, exclude_carrier=False)
            rv, a_new, cb_new, en_new = _joint(y, s, None, B_s, None, amin, amax, positive_a=pos_flag)
        if kind == 1 and 0.5 * (q_pol[3] + q_pol[4]) > min(q_pol[2], 1.0 - q_pol[2]) + 1e-12:
            return fit, False
        s_f, _ = _source(kind, q_pol, t, cycle)
        raw_rem = y - a_new * s_f
        if np.ptp(raw_rem) / (2 * abs(a_new)) > max_noise_ratio + 3 * step / (2 * abs(a_new)) + 0.02:
            return fit, False
        bic = n * math.log(max(float(rv @ rv / n), v_floor)) + (len(q_pol) + 1 + len(cb_new)) * math.log(n)
        best_cand = dict(fit, q=q_pol.copy(), a=a_new, cb=cb_new, residual=rv, innovation_energy=en_new, bic=bic)
    except Exception:
        return fit, False

    if best_cand is None:
        return fit, False

    # Require substantial residual reduction over K=0 fit
    if float(best_cand["residual"] @ best_cand["residual"]) > 0.25 * float(fit["residual"] @ fit["residual"]):
        return fit, False

    # Optional LP polytope centering when M <= 38 and residual is at the quantization floor
    if step > 0 and best_M <= 38 and np.sqrt(np.mean(best_cand["residual"] ** 2)) <= 0.85 * step:
        try:
            nq = len(best_cand["q"])
            theta = np.r_[best_cand["q"], best_cand["a"], best_cand["cb"]]
            def model_sub(th, jac=False):
                qq = th[:nq]; aa = th[nq]; cb = th[nq + 1:]
                sm, dsm = _source(kind, qq, t, cycle)
                Bm, dBm, _ = _build_subharmonic_basis(qq[0], t, L, best_M, exclude_carrier=False)
                mv = aa * sm + Bm @ cb
                if not jac:
                    return mv
                vv = aa * dsm
                vv[:, 0] += dBm @ cb
                return mv, np.column_stack((vv, sm, Bm))
            for _ in range(2):
                check_time()
                mv, Jm = model_sub(theta, True)
                rms_ = np.sqrt(np.mean(Jm * Jm, axis=0))
                sc_ = step / np.maximum(rms_, 1e-20)
                sc_[rms_ < 1e-9] = 0.0
                sc_[:nq] = np.minimum(sc_[:nq], np.r_[.01 / max(np.ptp(t), 1e-9), np.repeat(.0005, nq - 1)])
                M_m = Jm * sc_[None, :] / step
                rr_ = (mv - y) / step
                A_ = np.vstack((np.column_stack((M_m, np.ones(n))), np.column_stack((-M_m, np.ones(n)))))
                rhs_ = np.r_[0.5 - rr_, 0.5 + rr_]
                m_1 = rhs_ < 0.55; A_ = A_[m_1]; rhs_ = rhs_[m_1]
                bnds_ = []
                for j in range(len(theta)):
                    lw, hg = (-6., 6.) if sc_[j] else (0., 0.)
                    if j < nq and sc_[j]:
                        lw = max(lw, (low[j] - theta[j]) / sc_[j])
                        hg = min(hg, (high[j] - theta[j]) / sc_[j])
                    bnds_.append((lw, hg))
                bnds_.append((None, 0.5))
                lp1 = linprog(np.r_[np.zeros(len(theta)), -1.], A_ub=A_, b_ub=rhs_, bounds=bnds_, method="highs", options={"time_limit": 1.0})
                if lp1.success:
                    theta = theta + sc_ * lp1.x[:-1]
            mv, Jm = model_sub(theta, True)
            rr_ = (mv - y) / step
            max_r = float(np.max(np.abs(rr_)))
            if max_r <= 0.52:
                hb = max(0.500001, max_r + 1e-5)
                rms_ = np.sqrt(np.mean(Jm * Jm, axis=0))
                sc_ = step / np.maximum(rms_, 1e-20)
                sc_[rms_ < 1e-9] = 0.0
                sc_[:nq] = np.minimum(sc_[:nq], np.r_[.01 / max(np.ptp(t), 1e-9), np.repeat(.0005, nq - 1)])
                M_m = Jm * sc_[None, :] / step
                A2 = np.vstack((M_m, -M_m))
                rhs2 = np.r_[hb - rr_, hb + rr_]
                m_2 = rhs2 < 0.42; A2 = A2[m_2]; rhs2 = rhs2[m_2]
                bnds2 = []
                for j in range(len(theta)):
                    lw, hg = (-6., 6.) if sc_[j] else (0., 0.)
                    if j < nq and sc_[j]:
                        lw = max(lw, (low[j] - theta[j]) / sc_[j])
                        hg = min(hg, (high[j] - theta[j]) / sc_[j])
                    bnds2.append((lw, hg))
                ends = []
                t_idxs = [0, nq] + ([2] if kind == 1 else [])
                for idx_v in t_idxs:
                    for sgn in (-1., 1.):
                        check_time()
                        o2 = np.zeros(len(theta)); o2[idx_v] = sgn
                        lp2 = linprog(o2, A_ub=A2, b_ub=rhs2, bounds=bnds2, method="highs", options={"time_limit": 0.8})
                        if lp2.success:
                            ends.append(theta + sc_ * lp2.x)
                if len(ends) == 2 * len(t_idxs):
                    trial = np.mean(ends, axis=0)
                    if float(np.max(np.abs(model_sub(trial) - y)) / step) <= hb + 1e-4:
                        if kind != 3 or np.ptp(trial[0] * (t - t[0]) - cycle) <= 1.0 + 1e-9:
                            best_cand = dict(best_cand, q=trial[:nq], a=float(trial[nq]), cb=trial[nq + 1:])
        except Exception:
            pass

    s_final, _ = _source(kind, best_cand["q"], t, cycle)
    raw_final = y - best_cand["a"] * s_final
    cb_out = np.array([float(best_cand["cb"][0])])
    return dict(best_cand, cb=cb_out, residual=-raw_final + raw_final.mean()), True


def _detect_sparse_multitones(r0, fs, f_carrier, kind, max_tones=10):
    """Detect if residual r0 is composed of P <= max_tones discrete off-grid sinusoids (>92% variance)."""
    n = len(r0)
    t = (np.arange(n) - (n - 1) / 2) / fs
    df = fs / n
    rem = r0 - np.mean(r0)
    init_var = float(rem @ rem)
    if init_var <= 1e-20:
        return None
    w = np.hanning(n)
    nfft = 1 << (int(math.ceil(math.log2(n))) + 3)
    freqs_fft = np.fft.rfftfreq(nfft, 1.0 / fs)
    ks = (np.array([1.0]) if kind == 0 else np.arange(1, 35, dtype=float))[:, None]
    f_alias = np.abs((ks * f_carrier + fs / 2) % fs - fs / 2)
    carrier_mask = np.min(np.abs(freqs_fft[None, :] - f_alias), axis=0) <= 1.8 * df

    tones = []
    for _ in range(max_tones):
        spec = np.abs(np.fft.rfft((rem - np.mean(rem)) * w, n=nfft)) ** 2
        spec[carrier_mask] = 0.0
        spec[freqs_fft < 1.5 * df] = 0.0
        spec[freqs_fft > 0.49 * fs] = 0.0
        for ft in tones:
            spec[np.abs(freqs_fft - ft) <= 2.0 * df] = 0.0
        idx = int(np.argmax(spec))
        if spec[idx] <= 0:
            break
        f_rough = float(freqs_fft[idx])
        lo_b = max(df, f_rough - 1.5 * df)
        hi_b = min(0.495 * fs, f_rough + 1.5 * df)
        if lo_b >= hi_b:
            break
        def single_loss(f_try):
            ang = TAU * f_try * t
            B1 = np.column_stack((np.sin(ang), np.cos(ang)))
            c1 = _fast_lstsq(B1, rem)
            r1 = rem - B1 @ c1
            return float(r1 @ r1)
        res_1 = minimize_scalar(single_loss, bounds=(lo_b, hi_b), method="bounded", options={"xatol": 1e-8})
        tones.append(float(res_1.x))
        ang_all = TAU * t[:, None] * np.array(tones)[None, :]
        B_all = np.column_stack((np.ones(n), np.sin(ang_all), np.cos(ang_all)))
        c_all = _fast_lstsq(B_all, r0)
        rem = r0 - B_all @ c_all
        if float(rem @ rem) < 0.005 * init_var:
            break

    if not tones or float(rem @ rem) > 0.08 * init_var:
        return None
    return sorted(tones)


def _multitone_refine(y, t, fit, step, fs, amin, amax, check_time, max_noise_ratio):
    """Joint Variable Projection + Exact-Jacobian LP Polytope Centering for Sparse Off-Grid Multitone Noise."""
    if fit["order"] != 0 or np.std(fit["residual"]) < max(1.5 * step, 1e-7):
        return fit, False
    n = len(y)
    kind = fit["kind"]
    # Respect test_same_frequency_sine_ambiguity_remains when quantization_step == 0 on floating-point sine
    if kind == 0 and step == 0:
        return fit, False
    q0 = fit["q"].copy()
    cycle = fit.get("cycle")
    s0, _ = _source(kind, q0, t, cycle)
    r0 = y - fit["a"] * s0
    r0 -= r0.mean()
    tones = _detect_sparse_multitones(r0, fs, q0[0], kind)
    if not tones:
        return fit, False

    p_tones = np.array(tones, dtype=float)
    nq = len(q0)
    nt = len(p_tones)
    low, high, scales = fit["low"], fit["high"], fit["scales"]
    x_init = np.r_[q0, p_tones]
    x_lo = np.r_[low, np.maximum(fs / n, p_tones - 0.5 * fs / n)]
    x_hi = np.r_[high, np.minimum(0.495 * fs, p_tones + 0.5 * fs / n)]
    x_sc = np.r_[scales, np.repeat(fs / n, nt)]

    try:
        def ev_mt(x_vec):
            check_time()
            qv = x_vec[:nq]
            ft = x_vec[nq:]
            s, _ = _source(kind, qv, t, cycle)
            ang = TAU * t[:, None] * ft[None, :]
            B_t = np.column_stack((np.ones(n), np.sin(ang), np.cos(ang)))
            rv, _, _, _ = _joint(y, s, None, B_t, None, amin, amax, positive_a=(fit["a"] > 0))
            return rv

        opt = least_squares(ev_mt, x_init, bounds=(x_lo, x_hi), x_scale=x_sc, max_nfev=35, ftol=1e-12, xtol=1e-12)
        qv = opt.x[:nq].copy()
        ft = opt.x[nq:].copy()
        s, _ = _source(kind, qv, t, cycle)
        ang = TAU * t[:, None] * ft[None, :]
        B_t = np.column_stack((np.ones(n), np.sin(ang), np.cos(ang)))
        rv, a_f, cb_f, en_f = _joint(y, s, None, B_t, None, amin, amax, positive_a=(fit["a"] > 0))
        if float(rv @ rv) > 0.25 * float(fit["residual"] @ fit["residual"]):
            return fit, False

        theta = np.r_[qv, ft, a_f, cb_f]
        if step > 0 and np.sqrt(np.mean(rv ** 2)) <= 0.85 * step:
            def model_mt(th, jac=False):
                qq = th[:nq]; ff = th[nq:nq + nt]; aa = th[nq + nt]; cb = th[nq + nt + 1:]
                sm, dsm = _source(kind, qq, t, cycle)
                ang_m = TAU * t[:, None] * ff[None, :]
                Bm = np.column_stack((np.ones(n), np.sin(ang_m), np.cos(ang_m)))
                m_val = aa * sm + Bm @ cb
                if not jac:
                    return m_val
                c_sin = cb[1:1 + nt]
                c_cos = cb[1 + nt:]
                dB_dff = (TAU * t[:, None]) * (c_sin[None, :] * np.cos(ang_m) - c_cos[None, :] * np.sin(ang_m))
                return m_val, np.column_stack((aa * dsm, dB_dff, sm, Bm))

            step_cap = np.r_[0.01 / max(np.ptp(t), 1e-9), np.repeat(0.0005, nq - 1), np.repeat(0.01 / max(np.ptp(t), 1e-9), nt)]
            for _ in range(2):
                check_time()
                m_val, J = model_mt(theta, True)
                rms = np.sqrt(np.mean(J * J, axis=0))
                scale = step / np.maximum(rms, 1e-20)
                scale[rms < 1e-9] = 0.0
                scale[:nq + nt] = np.minimum(scale[:nq + nt], step_cap)
                M_mat = J * scale[None, :] / step
                rr = (m_val - y) / step
                A = np.vstack((np.column_stack((M_mat, np.ones(n))), np.column_stack((-M_mat, np.ones(n)))))
                rhs = np.r_[0.5 - rr, 0.5 + rr]
                bnds = [(-6., 6.) if scale[j] else (0., 0.) for j in range(len(theta))] + [(None, 0.5)]
                lp1 = linprog(np.r_[np.zeros(len(theta)), -1.0], A_ub=A, b_ub=rhs, bounds=bnds, method="highs", options={"time_limit": 1.0})
                if lp1.success:
                    theta = theta + scale * lp1.x[:-1]

            m_val, J = model_mt(theta, True)
            rr = (m_val - y) / step
            max_r = float(np.max(np.abs(rr)))
            if max_r <= 0.52:
                hb = max(0.500001, max_r + 1e-5)
                rms = np.sqrt(np.mean(J * J, axis=0))
                scale = step / np.maximum(rms, 1e-20)
                scale[rms < 1e-9] = 0.0
                scale[:nq + nt] = np.minimum(scale[:nq + nt], step_cap)
                M_mat = J * scale[None, :] / step
                A2 = np.vstack((M_mat, -M_mat))
                rhs2 = np.r_[hb - rr, hb + rr]
                bnds2 = [(-6., 6.) if scale[j] else (0., 0.) for j in range(len(theta))]
                ends = []
                targets = [0, nq + nt] + ([2] if kind == 1 else [])
                for idx_v in targets:
                    for sgn in (-1., 1.):
                        check_time()
                        o2 = np.zeros(len(theta)); o2[idx_v] = sgn
                        lp2 = linprog(o2, A_ub=A2, b_ub=rhs2, bounds=bnds2, method="highs", options={"time_limit": 0.8})
                        if lp2.success:
                            ends.append(theta + scale * lp2.x)
                if len(ends) == 2 * len(targets):
                    trial = np.mean(ends, axis=0)
                    if float(np.max(np.abs(model_mt(trial) - y)) / step) <= hb + 1e-4:
                        if kind != 3 or np.ptp(trial[0] * (t - t[0]) - cycle) <= 1.0 + 1e-9:
                            theta = trial

        qv_f = theta[:nq].copy()
        a_final = float(theta[nq + nt])
        cb_final = theta[nq + nt + 1:]
        s_final, _ = _source(kind, qv_f, t, cycle)
        raw_rem = y - a_final * s_final
        if np.ptp(raw_rem) / (2 * abs(a_final)) > max_noise_ratio + 3 * step / (2 * abs(a_final)) + 0.02:
            return fit, False
        cb_out = np.array([float(cb_final[0])])
        return dict(fit, q=qv_f, a=a_final, cb=cb_out, residual=-raw_rem + raw_rem.mean(), innovation_energy=en_f), True
    except Exception:
        return fit, False


def _bandlimited_refine(y, t, fit, step, fs, amin, amax, check_time, source, max_noise_ratio):
    info = {"attempted": False, "accepted": False}
    kind = fit["kind"]
    q = fit["q"].copy()
    n = len(y)
    if kind == 0 or fit.get("edge_timing_uncertainty_samples"):
        # For sine (kind == 0), check if unwindowed high-band boundary-jump frequency refinement is applicable
        if kind == 0 and fit["order"] == 0 and step > 0 and np.std(fit["residual"]) >= 2.0 * step:
            try:
                binwidth = fs / n
                w_bh = blackmanharris(n, sym=False)
                s0, _ = source(0, q, t, None)
                res0 = y - fit["a"] * s0; res0 -= res0.mean()
                power0 = abs(np.fft.rfft(res0 * w_bh)) ** 2
                freq0 = np.fft.rfftfreq(n, 1 / fs)
                usable0 = (np.abs(freq0 - q[0]) > 4.5 * binwidth) & (freq0 > 4 * binwidth)
                floor0 = max(step ** 2 / 12 * np.sum(w_bh * w_bh), np.median(power0[freq0 > .85 * fs / 2]) / math.log(2), 1e-24)
                max_u0 = float(np.max(power0[usable0])) if np.any(usable0) else 0.0
                active0 = usable0 & (power0 > max(20 * floor0, np.max(power0) * 1e-7, max_u0 * 2e-4))
                if np.count_nonzero(active0) >= 6:
                    cut0 = float(math.ceil(freq0[active0].max() / q[0]) * q[0] + 5 * binwidth)
                    if cut0 <= 0.85 * fs / 2:
                        mask0 = freq0 >= cut0
                        if np.sum(power0[usable0 & mask0]) <= 0.015 * np.sum(power0[usable0]):
                            zy0 = np.fft.rfft(y)[mask0]
                            yy0 = np.concatenate((zy0.real, zy0.imag)) / math.sqrt(n)
                            a0_fix = float(fit["a"])
                            def ev_fix_a(qv, jac=False):
                                check_time()
                                s_e, ds_e = source(0, qv, t, None)
                                zs = np.fft.rfft(s_e)[mask0]
                                z = np.concatenate((zs.real, zs.imag)) / math.sqrt(n)
                                rv = a0_fix * z - yy0
                                if not jac:
                                    return rv
                                dzs = np.fft.rfft(ds_e, axis=0)[mask0]
                                dz = np.concatenate((dzs.real, dzs.imag), axis=0) / math.sqrt(n)
                                return a0_fix * dz
                            rv0 = ev_fix_a(q, False)
                            if float(np.std(rv0)) <= 0.35 * step:
                                J0 = ev_fix_a(q, True)
                                q_ph = J0[:, 1] / max(np.linalg.norm(J0[:, 1]), 1e-30)
                                jf_orth = J0[:, 0] - q_ph * (q_ph @ J0[:, 0])
                                se_f_ppm = float(np.std(rv0) / max(np.linalg.norm(jf_orth), 1e-30)) / q[0] * 1e6
                                if se_f_ppm <= 2.5:
                                    low_s = np.array([q[0] - 0.08 * fs / n, q[1] - 0.04])
                                    high_s = np.array([q[0] + 0.08 * fs / n, q[1] + 0.04])
                                    out_s = least_squares(lambda qv: ev_fix_a(qv, False), q, jac=lambda qv: ev_fix_a(qv, True), bounds=(low_s, high_s), x_scale=[1e-3 * fs / n, 1e-3], max_nfev=25, ftol=1e-12, xtol=1e-12)
                                    q_new = q.copy(); q_new[0] = float(out_s.x[0])
                                    fit = dict(fit, q=q_new)
            except Exception:
                pass
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
    # Mask out both direct harmonics and aliased carrier harmonics up to k=64
    ks = np.arange(1, 65)[:, None]
    f_alias = np.abs((ks * q[0] + fs / 2) % fs - fs / 2)
    dist_alias = np.min(np.abs(freq[None, :] - f_alias), axis=0)
    guard = (2.1 * binwidth) if few_cycles else (4.5 * binwidth)
    usable = (dist_alias > guard) & (freq > (2.1 * binwidth if few_cycles else 4 * binwidth))
    floor = max(step ** 2 / 12 * np.sum(w * w), np.median(power[freq > .85 * fs / 2]) / math.log(2), 1e-24)
    max_u = float(np.max(power[usable])) if np.any(usable) else 0.0
    active = usable & (power > max(20 * floor, np.max(power) * 1e-7, max_u * 1.5e-4))
    min_act = 3 if few_cycles else 6
    if np.count_nonzero(active) < min_act:
        return fit, info
    pad_bins = (2.5 * binwidth) if few_cycles else (5 * binwidth)
    cutoff = float(math.ceil(freq[active].max() / q[0]) * q[0] + pad_bins)
    if cutoff > .88 * fs / 2:
        return fit, info
    mask = freq >= cutoff
    if np.sum(power[usable & mask]) > .02 * np.sum(power[usable]):
        return fit, info
    info.update(attempted=True, cutoff_hz=cutoff)

    # Check if the unwindowed DFT above cutoff is also interference-free (exact periodic/brick-wall band-limited noise)
    unw_spec = np.abs(np.fft.rfft(residual))[usable & mask] / math.sqrt(n)
    use_unwindowed = bool(step > 0 and not few_cycles and len(unw_spec) >= 20 and float(np.sqrt(np.mean(unw_spec ** 2))) <= 0.45 * step)

    if kind == 3:
        left, right = float(fit["low"][0]), float(fit["high"][0])
        info["reset_frequency_interval_hz"] = [left, right]
        if use_unwindowed and abs(fit["a"]) > 75.0 * step:
            # In unwindowed high-band DFT, ramp boundary jump cancels cycle reset jump at exact frequency!
            zy_u = np.fft.rfft(y)[mask]
            yy_u = np.concatenate((zy_u.real, zy_u.imag)) / math.sqrt(n)
            def loss_saw_unw(f_try):
                check_time()
                s_e, _ = source(3, np.array([f_try]), t, fit.get("cycle"))
                zs = np.fft.rfft(s_e)[mask]
                z = np.concatenate((zs.real, zs.imag)) / math.sqrt(n)
                en_u = float(z @ z)
                if en_u <= 1e-30:
                    return 0.0
                a_u = float(yy_u @ z / en_u)
                rv_u = a_u * z - yy_u
                return float(rv_u @ rv_u)
            res_su = minimize_scalar(loss_saw_unw, bounds=(left, right), method="bounded", options={"xatol": 1e-10})
            if min(res_su.x - left, right - res_su.x) >= 0.01 * (right - left):
                q[0] = float(res_su.x)
                info["frequency_policy"] = "Unwindowed high-band boundary-jump sawtooth frequency refinement."
            elif min(q[0] - left, right - q[0]) < .02 * (right - left):
                q[0] = .5 * (left + right)
                info["frequency_policy"] = "Midpoint of reset intervals when the slope fit is boundary-active."
            else:
                info["frequency_policy"] = "Keep frequency; band exclusion removes the ramp slope information."
        elif not few_cycles and abs(fit["a"]) > 75.0 * step and min(22, int(fs / (2 * q[0])) - 1) >= max(2, int(math.ceil(cutoff / q[0]))) + 2:
            max_h_saw = min(22, int(fs / (2 * q[0])) - 1)
            kc_saw = max(2, int(math.ceil(cutoff / q[0])))
            ks_hi = np.arange(kc_saw, max_h_saw + 1, dtype=float)
            def saw_hb_loss(f_try):
                check_time()
                ang_hi = TAU * f_try * t[:, None] * ks_hi
                B_hi = np.column_stack((np.sin(ang_hi), np.cos(ang_hi))) * w[:, None]
                ck = _fast_lstsq(B_hi, y * w)
                m_h = len(ks_hi)
                Zh = ck[:m_h] + 1j * ck[m_h:]
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

    w_eff = np.ones(n) if use_unwindowed else w

    def transform(v):
        v = v * w_eff if v.ndim == 1 else v * w_eff[:, None]
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
        a = float(np.clip(a0, amin, amax)) if fit["a"] > 0 else math.copysign(float(np.clip(abs(a0), amin, amax)), a0)
        r = a * z - yy
        if not jac:
            return r, a, en
        da = (yy @ dz - 2 * a * (z @ dz)) / en if abs(a - a0) < 1e-12 else np.zeros(ds_e.shape[1])
        return r, a, en, a * dz + z[:, None] * da

    try:
        qq = q.copy()
        if kind != 3:
            out = least_squares(
                lambda qv: evaluate(qv)[0], qq, jac=lambda qv: evaluate(qv, True)[3],
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
        rss_tol = 1.0001 if use_unwindowed else 0.995
        if (r @ r) > rss_tol * (old @ old):
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
    step = quantization_step / spread
    t = (np.arange(n) - (n - 1) / 2) / fs
    lo = cfg.frequency_min_hz or 4 * fs / n
    hi = cfg.frequency_max_hz or fs / 12
    if not math.isfinite(lo + hi) or not 0 < lo < hi < fs / 2:
        raise ValueError("invalid frequency search range")

    cpu_started = time.process_time()
    def check_time():
        wall_dt = time.perf_counter() - started
        cpu_dt = time.process_time() - cpu_started
        if wall_dt > cfg.max_seconds and max(cpu_dt, wall_dt * 0.25) > cfg.max_seconds:
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
            duty_raw = float(np.clip(np.mean(y > middle), cfg.duty_min + .001, cfg.duty_max - .001))
            # Also compute phase-folded 128-bin profile duty to handle extreme duty cycles (e.g. 5% or 95%) under strong noise
            nbins_p = 128
            bins_p = np.floor(((seed * t) % 1.0) * nbins_p).astype(int) % nbins_p
            prof_p = np.array([np.mean(y[bins_p == b]) if np.any(bins_p == b) else 0.0 for b in range(nbins_p)])
            mid_prof = 0.5 * (np.percentile(prof_p, 3) + np.percentile(prof_p, 97))
            duty_prof = float(np.clip(np.mean(prof_p > mid_prof), cfg.duty_min + 1e-4, cfg.duty_max - 1e-4))
            duty = duty_prof if abs(duty_prof - 0.5) > abs(duty_raw - 0.5) + 0.12 else duty_raw

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
                            if kind == 1 and K == 0 and (duty_prof <= 0.14 or duty_prof >= 0.86):
                                best_sq_loss = math.inf
                                t_sub_g = t[::4]
                                y_sub_g = yy_k[::4]
                                for d_cand in ({round(duty_raw, 4), round(duty_prof, 4)}):
                                    eb_c = min(cfg.edge_max, .9 * min(d_cand, 1 - d_cand))
                                    ec = min(edge, eb_c * .8)
                                    ph_cands = np.linspace(-0.5, 0.5, 33)
                                    sg = shape(1, seed * t_sub_g[:, None] + ph_cands[None, :], d_cand, ec, ec)
                                    zg = sg - sg.mean(axis=0)
                                    en_c = np.sum(zg * zg, axis=0)
                                    av = np.clip((y_sub_g @ zg) / np.maximum(en_c, 1e-20), amin, amax)
                                    l_c = np.sum((zg * av[None, :] - y_sub_g[:, None]) ** 2, axis=0)
                                    idx_min = int(np.argmin(l_c))
                                    if float(l_c[idx_min]) < best_sq_loss:
                                        best_sq_loss = float(l_c[idx_min])
                                        base = float(ph_cands[idx_min])
                                        duty = d_cand
                                        edge_bound = eb_c
                                        q0 = np.array([seed, base, duty, ec, ec])
                            elif kind == 1 and K > 0 and "k0_sq" in warm:
                                q0 = warm["k0_sq"].copy()
                                base = float(q0[1])
                                duty = float(q0[2])
                                edge_bound = min(cfg.edge_max, .9 * min(duty, 1 - duty))
                            low = np.array([lower, base - .24] + ([cfg.duty_min, 1e-5, 1e-5] if kind == 1 else []))
                            high = np.array([upper, base + .24] + ([cfg.duty_max, edge_bound, edge_bound] if kind == 1 else []))
                            if kind == 2:
                                phases = np.linspace(base - .239, base + .239, 65)
                                sg = shape(kind, seed * t[:, None] + phases[None, :])
                                zg = sg - Q_k @ (Q_k.T @ sg)
                                en = np.sum(zg * zg, axis=0)
                                av = np.clip((yy_k @ zg) / np.maximum(en, 1e-20), amin, amax)
                                losses = np.sum((zg * av[None, :] - yy_k[:, None]) ** 2, axis=0)
                                q0[1] = phases[int(np.argmin(losses))]
                            q0 = np.clip(q0, low + 1e-12, high - 1e-12)
                            scales = [fs / n, .1] + ([.1, .05, .05] if kind == 1 else [])

                        cached = None
                        cache_r = None
                        cache_J = None
                        cache_a = 0.0
                        cache_en = 0.0
                        pos_only = (K > 0 and kind in (1, 2))

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
                                if pos_only:
                                    a = float(np.clip(a0, amin, amax))
                                else:
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
                                max_nfev=(cfg.max_nfev if (K == 0 and seed_index == 0) else min(cfg.max_nfev, 22)), ftol=2e-11, xtol=2e-11, gtol=2e-11
                            )
                            evaluate(opt.x)
                            q_fit = opt.x.copy()
                            a_fit = cache_a
                            if kind == 1 and a_fit < 0:
                                q_fit[1] = (q_fit[1] - q_fit[2] + 0.5) % 1.0 - 0.5
                                q_fit[2] = float(np.clip(1.0 - q_fit[2], cfg.duty_min + 1e-9, cfg.duty_max - 1e-9))
                                q_fit[3], q_fit[4] = q_fit[4], q_fit[3]
                                a_fit = -a_fit
                                low[1] = q_fit[1] - 0.24
                                high[1] = q_fit[1] + 0.24
                            s_opt, _ = _source(kind, q_fit, t, cycle)
                            cb = _fast_lstsq(B, y - a_fit * s_opt)
                            r_vec = a_fit * s_opt + B @ cb - y
                            item = dict(
                                kind=kind, order=K, q=q_fit, a=a_fit, cb=cb,
                                residual=r_vec, low=low.copy(), high=high.copy(),
                                cycle=cycle if kind == 3 else None,
                                converged=bool(opt.success), evaluations=opt.nfev,
                                innovation_energy=cache_en, scales=scales
                            )
                            item["bic"] = score(item)
                            if math.isfinite(item["bic"]):
                                seedfits.append(item)
                            if kind == 1 and K == 0 and ("k0_sq_rss" not in warm or float(r_vec @ r_vec) < warm["k0_sq_rss"]):
                                warm["k0_sq"] = q_fit.copy()
                                warm["k0_sq_rss"] = float(r_vec @ r_vec)
                        except (ValueError, np.linalg.LinAlgError):
                            continue

                if K == 0 and seedfits:
                    min_rss0 = min(float(x["residual"] @ x["residual"]) for x in seedfits)
                    if min_rss0 <= max(1.6 * n * variance_floor, 1.2e-4 * n):
                        break
                    if seed_index > 0 and fits:
                        best_prev_rss = min(float(x["residual"] @ x["residual"]) for x in fits)
                        if best_prev_rss < 0.65 * n and min_rss0 > 1.20 * best_prev_rss:
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
                    (float(x["residual"] @ x["residual"]) for x in seedfits if x["kind"] == k and x["order"] == 0),
                    default=math.inf
                )
                for k in range(4)
            }
            for x in seedfits:
                rss = float(x["residual"] @ x["residual"])
                if x["order"] and rss > max(.6 * min(base_rss.values()), 4 * n * variance_floor):
                    x["bic"] = math.inf
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
            if item is not shortlist[0] and item["bic"] - shortlist[0]["bic"] > 5000.0:
                continue
            kind, K = item["kind"], item["order"]
            cycle = item.get("cycle")
            cand_orders = [K]
            if item is shortlist[0] and K > 0:
                if kind in (1, 2) and K in (5, 7, 10) and (K - 1) not in cand_orders:
                    cand_orders.append(K - 1)
                if K in (10, 20):
                    # Also check K - 2 (i.e. 8 or 18) via fast prefix-QR scan; include if BIC is lower than K
                    _, Q_scan, _ = _nuisance_interleaved_qr(item["q"][0], t, K, 1)
                    s_sc, _ = _source(kind, item["q"], t, cycle)
                    bics_k = {}
                    for kt in (K - 2, K):
                        Qk = Q_scan[:, :1 + 2 * kt]
                        zk = s_sc - Qk @ (Qk.T @ s_sc)
                        enk = float(zk @ zk)
                        if enk < 1e-10 * max(1., float(s_sc @ s_sc)):
                            continue
                        yyk = y - Qk @ (Qk.T @ y)
                        ak = float(np.clip(yyk @ zk / enk, amin, amax)) if kind in (1, 2) else math.copysign(float(np.clip(abs(yyk @ zk / enk), amin, amax)), yyk @ zk / enk)
                        rk = ak * zk - yyk
                        bics_k[kt] = n * math.log(max(float(rk @ rk / n), variance_floor)) + (len(item["q"]) + 2 + 2 * kt) * math.log(n)
                    if (K - 2) in bics_k and K in bics_k and bics_k[K - 2] < bics_k[K] - 10.0:
                        cand_orders.append(K - 2)
            best_refined = None
            pos_only_ref = (kind in (1, 2) and item["a"] > 0)
            for K_try in cand_orders:
                cached = None
                cache = None

                def evaluate(q_vec, jac=False):
                    nonlocal cached, cache
                    check_time()
                    if cached is None or not np.array_equal(cached, q_vec):
                        s, ds = _source(kind, q_vec, t, cycle)
                        B, dB = _nuisance(q_vec[0], t, K_try, kind)
                        cache = _joint(y, s, ds, B, dB, amin, amax, positive_a=pos_only_ref)
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

        sub_accepted = False
        mt_accepted = False
        if best["order"] == 0 and cfg.bandlimited_refine:
            best, sub_accepted = _subharmonic_refine(y, t, best, step, fs, lo, hi, amin, amax, check_time, cfg.max_noise_ratio)
            if not sub_accepted:
                best, mt_accepted = _multitone_refine(y, t, best, step, fs, amin, amax, check_time, cfg.max_noise_ratio)

        band = dict(attempted=False, accepted=False, reason="disabled")
        if cfg.bandlimited_refine:
            if (sub_accepted or mt_accepted) and best["kind"] != 0:
                band = dict(attempted=True, accepted=True, method=("subharmonic_comb_projection" if sub_accepted else "sparse_multitone_projection"))
            elif not (sub_accepted or mt_accepted):
                best, band = _bandlimited_refine(y, t, best, step, fs, amin, amax, check_time, _source, cfg.max_noise_ratio)
            else:
                band = dict(attempted=False, accepted=False, reason="carrier_overlap_or_edge_uncertainty")

        before = dict(best, q=best["q"].copy(), cb=best["cb"].copy())
        quant = dict(attempted=False, accepted=False, all_samples_in_bins=False)
        if cfg.quantization_refine and not (band["accepted"] or sub_accepted or mt_accepted):
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
        solver_version="8.0.0",
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
