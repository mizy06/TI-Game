"""Independent waveform estimator: samples and acquisition metadata only.

All four families compete by one separable generalized least-squares objective.
FFT supplies starting points, never a class or final parameter. No legacy C
classifier, truth label, noise realization, or test threshold enters the fit.
The low-band covariance is an explicit engineering prior, not a measured error
bound. Each frequency start fixes its whitening operator before optimization.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import time
import numpy as np
from scipy.optimize import least_squares, minimize_scalar
from scipy.signal import find_peaks
from threadpoolctl import threadpool_limits

NAMES = ("sine", "square", "triangle", "sawtooth")
TAU = 2 * math.pi


@dataclass(frozen=True)
class SolverConfig:
    frequency_min_hz: float = 0.  # zero selects the observable frame range
    frequency_max_hz: float = 0.
    starts: int = 4
    max_nfev: int = 65
    max_seconds: float = 5.
    duty_min: float = .05
    duty_max: float = .95
    edge_max: float = .15
    low_harmonic_weight: float = .01
    noise_harmonics: int = 5
    coherent_rms_max: float = .06
    high_band_rms_max: float = .04


def shape(kind, phase, duty=.5, rise=.03, fall=.03):
    """Unit half-amplitude; square duty is between edge midpoints."""
    p = np.asarray(phase) % 1
    if kind == 0:
        return np.sin(TAU*p)
    if kind == 2:
        return 1-4*np.abs((p+.25) % 1-.5)
    if kind == 3:
        return 1-2*p
    out = np.where(p < duty, 1., -1.)
    a, b = (p+.5) % 1-.5, (p-duty+.5) % 1-.5
    out = np.where(np.abs(a) < rise/2, 2*a/rise, out)
    return np.where(np.abs(b) < fall/2, -2*b/fall, out)


def _basis(kind, q, t):
    p = (q[0]*t+q[1]) % 1
    s = shape(kind, p, *(q[2:] if kind == 1 else ()))
    if kind == 0:
        slope = TAU*np.cos(TAU*p)
    elif kind == 2:
        slope = np.where((p+.25) % 1 < .5, 4., -4.)
    else:
        duty, rise, fall = q[2:]
        a, b = (p+.5) % 1-.5, (p-duty+.5) % 1-.5
        mr, mf = np.abs(a) < rise/2, np.abs(b) < fall/2
        slope = np.where(mr, 2/rise, np.where(mf, -2/fall, 0.))
    ds = [slope*t, slope]
    if kind == 1:
        ds.extend((np.where(mf, 2/fall, 0.),
                   np.where(mr, -2*a/rise**2, 0.),
                   np.where(mf, 2*b/fall**2, 0.)))
    return s, np.column_stack(ds)


def _project(y, s, ds=None):
    """Exact elimination of offset/amplitude and exact reduced Jacobian."""
    z = s-s.mean()
    energy = float(z@z)
    if energy < 1e-12:
        raise ValueError("unobservable template")
    a = float((y-y.mean())@z/energy)
    b = float(y.mean()-a*s.mean())
    residual = b+a*s-y
    if ds is None:
        return residual, a, b
    dz = ds-ds.mean(axis=0)
    da = ((y-y.mean())@dz-2*a*(z@dz))/energy
    return residual, a, b, a*dz+z[:, None]*da


def _frequency_starts(y, fs, lo, hi, count):
    size = 1 << int(math.ceil(math.log2(len(y)*8)))
    spectrum = np.abs(np.fft.rfft((y-y.mean())*np.hanning(len(y)), size))
    f = np.fft.rfftfreq(size, 1/fs)
    spectrum[(f < lo) | (f > hi)] = 0
    peaks, _ = find_peaks(spectrum, distance=max(1, size//len(y)//2))
    peaks = sorted(peaks, key=lambda k: spectrum[k], reverse=True)
    if not peaks:
        raise ValueError("no observable periodic component")
    seeds = []
    # Include a subharmonic when a narrow-duty pulse's second harmonic wins.
    candidates = [f[peaks[0]], f[peaks[0]]/2]+[f[k] for k in peaks[1:count]]
    for value in candidates:
        if lo <= value <= hi and all(abs(value-s) > .65*fs/len(y) for s in seeds):
            seeds.append(float(value))
        if len(seeds) == count:
            break
    return seeds


def estimate(samples, sample_rate_hz, config=None):
    """Return float parameters plus diagnostics; raises on invalid/expired input."""
    cfg = config or SolverConfig()
    started = time.perf_counter()
    yraw = np.asarray(samples, dtype=np.float64)
    if (yraw.ndim != 1 or len(yraw) < 256 or not np.all(np.isfinite(yraw)) or
            not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0):
        raise ValueError("invalid samples or sample rate")
    if (not isinstance(cfg.starts, int) or cfg.starts < 1 or
            not isinstance(cfg.max_nfev, int) or cfg.max_nfev < 1 or
            not math.isfinite(cfg.max_seconds) or cfg.max_seconds <= 0 or
            not 0 < cfg.duty_min < cfg.duty_max < 1 or not 0 < cfg.edge_max <= .2 or
            not 0 < cfg.low_harmonic_weight <= 1 or not 1 <= cfg.noise_harmonics <= 15 or
            not 0 < cfg.coherent_rms_max < 1 or not 0 < cfg.high_band_rms_max < 1):
        raise ValueError("invalid solver configuration")
    spread = float(np.std(yraw))
    if spread <= 1e-12:
        raise ValueError("flat signal")
    center = float(yraw.mean())
    y = (yraw-center)/spread
    n, fs = len(y), float(sample_rate_hz)
    t = (np.arange(n)-(n-1)/2)/fs
    lo = cfg.frequency_min_hz or 4*fs/n
    hi = cfg.frequency_max_hz or fs/12
    if not math.isfinite(lo+hi) or not 0 < lo < hi < fs/2:
        raise ValueError("invalid frequency search range")
    width = .7*fs/n
    fits = []

    def check_time():
        if time.perf_counter()-started > cfg.max_seconds:
            raise TimeoutError("unified solver deadline exceeded")

    with threadpool_limits(limits=1, user_api="blas"):
        seeds = _frequency_starts(y, fs, lo, hi, cfg.starts)
        for seed in seeds:
            check_time()
            lower, upper = max(lo, seed-width), min(hi, seed+width)
            def sine_loss(f):
                check_time()
                design = np.column_stack((np.ones(n), np.sin(TAU*f*t), np.cos(TAU*f*t)))
                c = np.linalg.lstsq(design, y, rcond=None)[0]
                r = design@c-y
                return float(r@r)
            seed = float(minimize_scalar(sine_loss, bounds=(lower, upper),
                method="bounded", options={"xatol": 1e-10}).x)
            design = np.column_stack((np.ones(n), np.sin(TAU*seed*t), np.cos(TAU*seed*t)))
            c = np.linalg.lstsq(design, y, rcond=None)[0]
            phase = float(np.arctan2(c[2], c[1])/TAU)
            harmonics = np.arange(1,min(cfg.noise_harmonics,int(fs/(2*seed))-1)+1)
            angles = TAU*seed*t[:,None]*harmonics
            noise_basis = np.column_stack((np.sin(angles),np.cos(angles)))
            noise_basis -= noise_basis.mean(axis=0)
            noise_q = np.linalg.qr(noise_basis,mode="reduced")[0]
            # Estimate whether the record supports a low-band coherent-noise
            # covariance. Never use the case label, true bandwidth or replay size.
            probe_h = min(15,int(fs/(2*seed))-1)
            probe_angles = TAU*seed*t[:,None]*np.arange(1,probe_h+1)
            probe = np.column_stack((np.ones(n),np.sin(probe_angles),np.cos(probe_angles)))
            probe_c = np.linalg.lstsq(probe,y,rcond=None)[0]
            coherent_rms = float(np.std(y-probe@probe_c))
            high_part = (np.sin(probe_angles[:,cfg.noise_harmonics:])@probe_c[1+cfg.noise_harmonics:1+probe_h]
                +np.cos(probe_angles[:,cfg.noise_harmonics:])@probe_c[1+probe_h+cfg.noise_harmonics:])
            high_rms = float(np.std(high_part))
            weight = cfg.low_harmonic_weight if (coherent_rms < cfg.coherent_rms_max and
                high_rms < cfg.high_band_rms_max) else 1.
            alpha = 1-math.sqrt(weight)
            def whiten(v):
                return v-alpha*noise_q@(noise_q.T@v) if alpha else v
            yw = whiten(y)
            levels = np.percentile(y,[2,10,90,98])
            # At a duty bound, both central quantiles can be on one plateau.
            middle = .5*(levels[0]+levels[3]) if levels[2]-levels[1] < .2*(levels[3]-levels[0]) else .5*(levels[1]+levels[2])
            duty = float(np.clip(np.mean(y > middle), cfg.duty_min+.001, cfg.duty_max-.001))
            for kind in (0, 2, 1):
                base = phase+(duty/2-.25 if kind == 1 else 0)
                edge_bound = min(cfg.edge_max, 1.8*min(duty, 1-duty))
                for edge in ((.008, .07) if kind == 1 else (0.,)):
                    q = [seed, base]+([duty, min(edge, edge_bound*.8), min(edge, edge_bound*.8)] if kind == 1 else [])
                    low = [lower, base-.24]+([cfg.duty_min, 1e-5, 1e-5] if kind == 1 else [])
                    high = [upper, base+.24]+([cfg.duty_max, edge_bound, edge_bound] if kind == 1 else [])
                    def projected(q, jac=False):
                        check_time()
                        s, ds = _basis(kind, q, t)
                        out = _project(yw, whiten(s), whiten(ds))
                        return out[3] if jac else out[0]
                    try:
                        fit = least_squares(projected, q, jac=lambda q: projected(q, True),
                            bounds=(low, high), x_scale=[fs/n, .1]+([.1, .05, .05] if kind == 1 else []),
                            ftol=2e-12, xtol=2e-12, gtol=2e-12, max_nfev=cfg.max_nfev)
                        s, ds = _basis(kind, fit.x, t)
                        r, a, b = _project(yw, whiten(s))
                        if kind == 1 and a < 0:
                            # Negative carrier coefficients invert high/low duration.
                            fit.x[1] -= fit.x[2]
                            fit.x[2] = 1-fit.x[2]
                            fit.x[3], fit.x[4] = fit.x[4], fit.x[3]
                            a, b = -a, b
                        raw_r = b+a*shape(kind,fit.x[0]*t+fit.x[1],
                            *(fit.x[2:] if kind==1 else ()))-y
                        fits.append(dict(kind=kind, q=fit.x, a=a, b=b,
                            loss=float(r@r), raw_loss=float(raw_r@raw_r), parameters=len(q)+2,
                            effective_weight=weight,coherent_rms=coherent_rms,high_band_rms=high_rms,
                            converged=bool(fit.success), evaluations=int(fit.nfev)))
                    except ValueError:
                        continue
            # A descending saw's observed resets fix a discrete topology. Within
            # it, offset, ramp slope and jump height are all linear variables.
            span = float(np.percentile(y, 98)-np.percentile(y, 2))
            delta = np.diff(y)
            spacing = max(1, int(.55*fs/seed))
            jumps, _ = find_peaks(delta, height=.48*span, distance=spacing)
            # scipy.find_peaks omits endpoints; a reset between samples 0 and
            # 1 must still increment the topology for every later sample.
            if delta[0] > .48*span and (not len(jumps) or jumps[0] >= spacing):
                jumps = np.r_[0, jumps]
            if delta[-1] > .48*span and (not len(jumps) or len(delta)-1-jumps[-1] >= spacing):
                jumps = np.r_[jumps, len(delta)-1]
            if len(jumps) >= 3:
                cycle = np.searchsorted(jumps+1, np.arange(n), side="right")
                design = np.column_stack((np.ones(n), t, cycle-cycle.mean()))
                c = np.linalg.lstsq(whiten(design), yw, rcond=None)[0]
                f = float(-c[1]/c[2]) if c[2] > 0 else 0.
                if lower <= f <= upper:
                    # Enforce the reset timing intervals. Noise can bias the
                    # unconstrained slope ratio until it no longer agrees with
                    # the very jumps used in the fit; do not discard that family.
                    indices = np.unique(np.r_[0, n-1, jumps, jumps+1])
                    counts = cycle[indices]
                    di = indices[None, :]-indices[:, None]
                    dk = counts[None, :]-counts[:, None]
                    mask = di > 0
                    flo = max(lower, float(np.max((dk[mask]-1)*fs/di[mask])))
                    fhi = min(upper, float(np.min((dk[mask]+1)*fs/di[mask])))
                    if flo > fhi:
                        continue
                    if not flo <= f <= fhi:
                        def ramp_loss(trial):
                            check_time()
                            rr, _, _ = _project(yw, whiten(2*(cycle-trial*t)))
                            return float(rr@rr)
                        trial = minimize_scalar(ramp_loss, bounds=(flo,fhi), method="bounded",
                            options={"xatol":1e-10})
                        f = min((flo,fhi,float(trial.x)), key=ramp_loss)
                        rr, amp, offset = _project(yw,whiten(2*(cycle-f*t)))
                        c = np.array([offset+2*amp*cycle.mean(),-2*amp*f,2*amp])
                    p = np.arange(n)*f/fs-cycle
                    left, right = float(-p.min()), float(1-p.max())
                    if left <= right+2*f/fs:
                        r = whiten(design@c-y)
                        midpoint = float(c[0]+c[2]*(f*(n-1)/(2*fs)-cycle.mean()-.5+(left+right)/2))
                        raw_r = design@c-y
                        fits.append(dict(kind=3, q=np.array([f, (left+right)/2]),
                            a=float(c[2]/2), b=midpoint, loss=float(r@r),raw_loss=float(raw_r@raw_r),
                            parameters=3, converged=True, evaluations=1,
                            effective_weight=weight,coherent_rms=coherent_rms,high_band_rms=high_rms,
                            phase_interval_cycles=[left, right], resets=int(len(jumps))))
        check_time()
    fits = [r for r in fits if r["raw_loss"]/n <= .4 and abs(r["a"]) >= .05]
    if not fits:
        raise ValueError("no valid waveform model")
    for fit in fits:
        fit["bic"] = n*math.log(max(fit["loss"]/n, 1e-30))+fit["parameters"]*math.log(n)
    winners = [min((r for r in fits if r["kind"] == k), key=lambda r: r["bic"])
               for k in range(4) if any(r["kind"] == k for r in fits)]
    winners.sort(key=lambda r: r["bic"])
    best = winners[0]
    if best["raw_loss"]/n > .4 or abs(best["a"]) < .05:
        raise ValueError("signal outside supported periodic model")
    q, kind = best["q"], best["kind"]
    identifiable = kind != 1 or min(q[3:])*fs/q[0] >= 2
    return dict(type=kind, name=NAMES[kind], frequency_hz=float(q[0]),
        vpp=float(2*abs(best["a"])*spread),
        dc=float(best["b"]*spread+center),
        duty_fraction=float(q[2]) if kind == 1 else 0.,
        diagnostics=dict(method="unified_projected_least_squares", seeds_hz=seeds,
            low_harmonic_weight=best["effective_weight"],noise_harmonics=cfg.noise_harmonics,
            coherent_rms=best["coherent_rms"],high_band_rms=best["high_band_rms"],
            candidates=len(fits), converged=best["converged"],
            residual_rms=float(math.sqrt(best["raw_loss"]/n)*spread),
            normalized_residual_rms=float(math.sqrt(best["raw_loss"]/n)),
            weighted_residual_rms=float(math.sqrt(best["loss"]/n)*spread),
            model_bic={NAMES[r["kind"]]: r["bic"] for r in winners},
            model_margin_bic=float(winners[1]["bic"]-best["bic"]) if len(winners)>1 else 0.,
            edge_identifiable=bool(identifiable),
            square_rise_fraction=float(q[3]) if kind == 1 else 0.,
            square_fall_fraction=float(q[4]) if kind == 1 else 0.,
            phase_interval_cycles=best.get("phase_interval_cycles"),
            accuracy_1ppm_certified=False,
            uncertainty_note="Fit residual is not a bound on coherent noise, clock, gain or INL.",
            elapsed_s=time.perf_counter()-started))
