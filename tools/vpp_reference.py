"""Vpp from synchronous independent noise observations and fixed calibration.

Runtime has no case IDs, source settings, truth, or error thresholds. Transfer
functions are learned ONLY from separate source-off / precision-standard runs.
No coefficients are adapted against a source-on measurement.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import time

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.optimize import least_squares
from scipy.signal import lfilter

from tools.unified_solver import SolverConfig, estimate, shape, threadpool_limits


def _vector(value, name, minimum=1):
    a = np.asarray(value, dtype=float)
    if a.ndim != 1 or len(a) < minimum or not np.all(np.isfinite(a)):
        raise ValueError(f"invalid {name}")
    return a


def _digest(*arrays):
    h = hashlib.sha256()
    for a in arrays:
        v = np.asarray(a, dtype='<f8')
        h.update(str(v.shape).encode('ascii'))
        h.update(v.tobytes())
    return h.hexdigest()


@dataclass(frozen=True)
class CodeCalibration:
    """Monotone inverse ADC curve from independent DC standards, in volts.

    Nodes are mean ADC codes at DC levels measured by a separate calibrated
    instrument. They are not a fit to waveform-test nominal amplitudes.
    """
    mean_codes: tuple
    standard_volts: tuple
    evidence: str

    def __post_init__(self):
        c = _vector(self.mean_codes, 'DC codes', 4)
        v = _vector(self.standard_volts, 'DC standards', 4)
        if (len(c) != len(v) or np.any(np.diff(c) <= 0) or
                np.any(np.diff(v) <= 0) or c[0] < 0 or c[-1] > 4095 or
                not self.evidence):
            raise ValueError('DC calibration must be monotone and have evidence')

    def convert(self, codes):
        c = _vector(codes, 'ADC codes', 256)
        if (np.any(c != np.rint(c)) or np.any(c <= 0) or np.any(c >= 4095)):
            raise ValueError('invalid or saturated raw ADC codes')
        if np.min(c) < self.mean_codes[0] or np.max(c) > self.mean_codes[-1]:
            raise ValueError('ADC data outside calibrated DC range')
        return PchipInterpolator(self.mean_codes, self.standard_volts, extrapolate=False)(c)


@dataclass(frozen=True)
class SynchronousObservation:
    reference_codes: object
    sample_rate_hz: float
    clock_id: str
    main_start_tick: int
    reference_start_tick: int
    main_channel: str
    reference_channel: str
    frontend_id: str
    reference_is_source_free: bool


@dataclass(frozen=True)
class ReferenceCalibration:
    sample_rate_hz: float
    main_adc: CodeCalibration
    reference_adc: CodeCalibration
    noise_taps: tuple
    signal_taps: tuple
    main_channel: str
    reference_channel: str
    frontend_id: str
    clock_id: str
    noise_training_rms_v: float
    evidence: dict

    def __post_init__(self):
        if not math.isfinite(self.sample_rate_hz) or self.sample_rate_hz <= 0:
            raise ValueError('invalid calibration sample rate')
        for taps in (self.noise_taps, self.signal_taps):
            h = _vector(taps, 'FIR taps')
            if len(h) > 65:
                raise ValueError('FIR order too large')
        # Prevent unsupported inversion near a zero; this is admission only,
        # not a certificate of an analog front-end model or absolute accuracy.
        response = np.fft.rfft(self.signal_taps, 4096)
        if np.min(np.abs(response)) < .1 or np.max(np.abs(response)) > 10:
            raise ValueError('signal path is ill-conditioned')
        if (not all((self.main_channel, self.reference_channel, self.frontend_id, self.clock_id))
                or self.main_channel == self.reference_channel or
                not math.isfinite(self.noise_training_rms_v) or self.noise_training_rms_v <= 0):
            raise ValueError('invalid calibration channel identity or excitation')
        if not self.evidence.get('source_off') or not self.evidence.get('independent_standard'):
            raise ValueError('independent source-off and standard evidence required')

    def to_dict(self):
        return dict(schema='vpp-reference-1', **asdict(self))

    @classmethod
    def from_dict(cls, value):
        d = dict(value)
        if d.pop('schema', None) != 'vpp-reference-1':
            raise ValueError('unknown Vpp calibration schema')
        d['main_adc'] = CodeCalibration(**d['main_adc'])
        d['reference_adc'] = CodeCalibration(**d['reference_adc'])
        return cls(**d)

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _fir_design(x, taps):
    # rows refer to real, contiguous samples; no wraparound or concatenated gaps.
    return np.lib.stride_tricks.sliding_window_view(x, taps)[:, ::-1]


def _calibrate_fir(x, y, orders, rms_limit):
    x, y = _vector(x, 'FIR excitation', 2048), _vector(y, 'FIR response', 2048)
    if len(x) != len(y) or np.std(x) < .01:
        raise ValueError('insufficient independent calibration excitation')
    cut = len(x) * 3 // 5
    candidates = []
    for order in orders:
        if not isinstance(order, int) or not 1 <= order <= 65:
            raise ValueError('invalid calibration FIR order')
        X = _fir_design(x[:cut], order)
        A = np.column_stack((np.ones(len(X)), X))
        if np.linalg.cond(A) > 1e6:
            continue
        c, _, rank, _ = np.linalg.lstsq(A, y[order-1:cut], rcond=None)
        if rank != A.shape[1]:
            continue
        r = y[order-1:cut] - A@c
        # Order selection sees training only. Holdout is never refitted.
        bic = len(r)*np.log(max(float(r@r)/len(r), 1e-24)) + len(c)*np.log(len(r))
        candidates.append((bic, order, c))
    if not candidates:
        raise ValueError('calibration excitation is rank-deficient')
    _, order, c = min(candidates, key=lambda item: item[0])
    held = y[cut+order-1:] - c[0] - _fir_design(x[cut:], order)@c[1:]
    rms = float(np.sqrt(np.mean(held**2)))
    if rms > rms_limit:
        raise ValueError(f'calibration held-out RMS {rms:g} V exceeds {rms_limit:g} V')
    return tuple(map(float, c[1:])), dict(order=order, validation_rms_v=rms,
        validation_max_abs_v=float(np.max(np.abs(held))), training_samples=cut,
        validation_samples=len(held), training_sha256=_digest(x[:cut], y[:cut]),
        validation_sha256=_digest(x[cut:], y[cut:]), fitted_intercept_v=float(c[0]))


def calibrate(*, sample_rate_hz, main_adc, reference_adc,
              noise_main_codes, noise_reference_codes,
              standard_main_codes, standard_input_v,
              main_channel, reference_channel, frontend_id, clock_id,
              source_off_evidence, standard_evidence,
              orders=(1, 3, 5, 9, 17), validation_rms_limit_v=.002):
    """Learn two fixed FIR paths from two SEPARATE independent experiments.

    noise_*: source disconnected, independently excited reference path.
    standard_*: interference off, main input measured by a precision reference
    digitizer sharing the sample clock/timestamps (not the generator setting).
    DC tables must already be obtained from independent precision DC standards.
    """
    if not source_off_evidence or not standard_evidence:
        raise ValueError('calibration experiments must be identified')
    if not math.isfinite(validation_rms_limit_v) or validation_rms_limit_v <= 0:
        raise ValueError('invalid calibration validation limit')
    nm = main_adc.convert(noise_main_codes)
    nr = reference_adc.convert(noise_reference_codes)
    sm = main_adc.convert(standard_main_codes)
    sv = _vector(standard_input_v, 'independent standard waveform', 2048)
    with threadpool_limits(limits=1):
        noise, ndiag = _calibrate_fir(nr, nm, orders, validation_rms_limit_v)
        signal, sdiag = _calibrate_fir(sv, sm, orders, validation_rms_limit_v)
    return ReferenceCalibration(sample_rate_hz, main_adc, reference_adc, noise, signal,
        main_channel, reference_channel, frontend_id, clock_id, float(np.std(nr)),
        dict(source_off=source_off_evidence, independent_standard=standard_evidence,
            noise=ndiag, signal=sdiag, no_source_on_adaptation=True,
            absolute_accuracy_verified=False))


def _check_observation(main_codes, fs, observation, calibration):
    o, c = observation, calibration
    if (o.reference_is_source_free is not True or not o.clock_id or
            not isinstance(o.main_start_tick, int) or not isinstance(o.reference_start_tick, int)
            or o.main_start_tick < 0 or o.main_start_tick != o.reference_start_tick):
        raise ValueError('source-free reference and identical hardware start ticks required')
    if (o.clock_id != c.clock_id or o.frontend_id != c.frontend_id or
            o.main_channel != c.main_channel or o.reference_channel != c.reference_channel):
        raise ValueError('channel, clock or front-end differs from calibration')
    if (not math.isfinite(fs) or not math.isfinite(o.sample_rate_hz) or
            abs(fs/c.sample_rate_hz-1) > 1e-9 or abs(fs/o.sample_rate_hz-1) > 1e-9):
        raise ValueError('reference and calibrated sample clocks differ')
    main = c.main_adc.convert(main_codes)
    ref = c.reference_adc.convert(o.reference_codes)
    if len(main) != len(ref):
        raise ValueError('synchronous channels have unequal lengths')
    if np.std(ref) > 2*c.noise_training_rms_v:
        raise ValueError('reference excitation exceeds calibrated amplitude range')
    return main, ref


def _forward_fit(y, fs, taps, seed, *, harmonics=0, deadline=math.inf):
    """Variable-projection source fit through an independently calibrated FIR.

    Harmonics, when requested, belong to the clean source, not to noise. The
    reconstructed envelope includes them. A fixed ideal waveform is not imposed
    on a distorted source. The four families remain the project's prior.
    """
    kind, frequency = seed['type'], seed['frequency_hz']
    def check_time():
        if time.perf_counter() >= deadline:
            raise TimeoutError('Vpp reference total deadline exceeded')
    check_time()
    n = len(y)
    t = (np.arange(n)-(n-1)/2)/fs
    burn = max(32, 2*len(taps))
    valid = slice(burn, n-burn)
    # Regularized inverse is used only for timing initialization. All final
    # amplitudes fit the original observations through the forward FIR.
    H = np.fft.rfft(taps, n)
    rough = np.fft.irfft(np.fft.rfft(y-y.mean())*H.conj()/(abs(H)**2+1e-10), n)
    angles = 2*np.pi*frequency*t
    first = np.linalg.lstsq(np.column_stack((np.ones(n), np.sin(angles), np.cos(angles))), rough, rcond=None)[0]
    phase = math.atan2(first[2], first[1])/(2*np.pi)
    diag = seed['diagnostics']
    if kind == 1:
        phase += seed['duty_fraction']/2-.25
    q0 = np.array([frequency, phase] + ([seed['duty_fraction'],
        max(diag['square_rise_fraction'], 1e-4),
        max(diag['square_fall_fraction'], 1e-4)] if kind == 1 else []))
    width = max(.01*frequency, 2*fs/n)
    lo = [max(.001, frequency-width), phase-.15] + ([.01, 1e-6, 1e-6] if kind == 1 else [])
    hi = [min(fs/4, frequency+width), phase+.15] + ([.99, .2, .2] if kind == 1 else [])
    # Saw phase is only an interval between observed resets. Use reset labels
    # from inverse-filtered observations; fit slope and jump height separately.
    cycles = None
    if kind == 3:
        differences = np.diff(rough)
        jumps = np.flatnonzero(abs(differences) > .45*np.ptp(rough))
        jumps = jumps[(jumps > 8) & (jumps < n-8)]
        if len(jumps) < 4 or np.max(abs(np.diff(jumps)*frequency/fs-1)) > .25:
            raise ValueError('unresolved sawtooth reset topology')
        cycles = np.searchsorted(jumps+1, np.arange(n), side='right')
        # Intersect all pairwise reset-time intervals. Midpoints of individual
        # transitions need not lie on one common phase lattice.
        ij = np.triu_indices(len(jumps), 1)
        distance = jumps[ij[1]]-jumps[ij[0]]
        dc = ij[1]-ij[0]
        flo = max(lo[0], float(np.max(dc*fs/(distance+1))))
        fhi = min(hi[0], float(np.min(dc*fs/(distance-1))))
        if flo >= fhi:
            raise ValueError('sawtooth reset intervals have no common frequency')
        q0 = np.array([np.clip(frequency, flo+1e-10, fhi-1e-10)])
        lo, hi = [flo], [fhi]
    hs = np.arange(2 if kind == 0 else 1, harmonics+1)
    hs = hs[hs*frequency < .4*fs]

    def design(q):
        if kind == 3:
            s = 2*(cycles-cycles.mean()-q[0]*t)
        else:
            s = shape(kind, q[0]*t+q[1], *(q[2:] if kind == 1 else ()))
        B = [np.ones(n), lfilter(taps, [1.], s)]
        a = 2*np.pi*q[0]*t[:, None]*hs
        for column in np.column_stack((np.sin(a), np.cos(a))).T:
            B.append(lfilter(taps, [1.], column))
        return np.column_stack(B)

    target = y[valid]
    def project(q, extra=False):
        check_time()
        A = design(q)[valid]
        coef = np.linalg.lstsq(A, target, rcond=None)[0]
        r = A@coef-target
        check_time()
        return (coef, r, A) if extra else r

    scales = [width, .01] + ([.03, .03, .03] if kind == 1 else [])
    fit = least_squares(project, q0, bounds=(lo, hi), x_scale=scales[:len(q0)],
        max_nfev=400, ftol=1e-12, xtol=1e-12, gtol=1e-12)
    coef, r, A = project(fit.x, True)
    if not fit.success or np.linalg.cond(A) > 1e7 or abs(coef[1]) < 1e-9:
        raise ValueError('Vpp forward model did not converge or is unidentifiable')
    # Reconstruct the source before its calibrated observation response. Square
    # corners and one-sided saw endpoints are explicitly included as extrema.
    from tools.precision_measurement import continuous_extrema
    q = fit.x
    if kind == 3:
        left = float(np.max(np.arange(1, len(jumps)+1)-q[0]*t[jumps+1]))
        right = float(np.min(np.arange(1, len(jumps)+1)-q[0]*t[jumps]))
        if left > right:
            raise ValueError('sawtooth reset phase is inconsistent')
        source_phase = (left+right)/2
    else:
        source_phase = q[1]
    knots = ([0., 1.] if kind == 3 else [.25, .75] if kind == 2 else
        [-q[3]/2, q[3]/2, q[2]-q[4]/2, q[2]+q[4]/2] if kind == 1 else [])
    def source(p):
        aa = 2*np.pi*(p-source_phase)[:, None]*hs
        return (coef[1]*shape(kind, p, *(q[2:] if kind == 1 else ())) +
            np.column_stack((np.sin(aa), np.cos(aa)))@coef[2:])
    low, high = continuous_extrema(source, 1., knots)
    check_time()
    vpp = high-low
    halves = []
    for section in (slice(0, len(target)//2), slice(len(target)//2, None)):
        cc = np.linalg.lstsq(A[section], target[section], rcond=None)[0]
        halves.append(float(cc[1]/coef[1]))
    rms = float(np.sqrt(np.mean(r*r)))
    check_time()
    return dict(vpp_v=float(vpp), frequency_hz=float(q[0]), residual_rms_v=rms,
        half_gain_ratio=halves, harmonics=len(hs), source_samples=len(target),
        evaluations=int(fit.nfev), model_bic=float(len(r)*np.log(max(rms*rms, 1e-24))+
            (len(q)+len(coef))*np.log(len(r))), converged=True)


def estimate_reference(main_codes, sample_rate_hz, observation, calibration, *,
                       config=None, expected_kind=None, harmonic_orders=(0, 3, 7),
                       timing_seed=None):
    """Estimate only source Vpp, without receiving any source truth/setting.

    No claim of absolute accuracy is inferred from residuals or repeatability.
    Runtime changes to the transfer path cannot in general be identified in a
    coherent source-on frame; repeat independent calibration when it changes.
    """
    started = time.perf_counter()
    cfg0 = config or SolverConfig(max_seconds=14.)
    if not math.isfinite(cfg0.max_seconds) or cfg0.max_seconds <= 0:
        raise ValueError('invalid Vpp reference time budget')
    deadline = started + cfg0.max_seconds
    def check_time():
        if time.perf_counter() >= deadline:
            raise TimeoutError('Vpp reference total deadline exceeded')
    main, ref = _check_observation(main_codes, sample_rate_hz, observation, calibration)
    check_time()
    if not harmonic_orders or any(not isinstance(h, int) or not 0 <= h <= 24 for h in harmonic_orders):
        raise ValueError('invalid clean-source harmonic orders')
    c = calibration
    burn = max(len(c.noise_taps), len(c.signal_taps))-1
    cancelled = main-lfilter(c.noise_taps, [1.], ref)
    # Trim genuine FIR history once, not an invented reference continuation.
    cancelled = cancelled[burn:]
    n = len(cancelled)
    if n < 1024:
        raise ValueError('insufficient synchronous samples after FIR history')
    H = np.fft.rfft(c.signal_taps, n)
    rough = np.fft.irfft(np.fft.rfft(cancelled-cancelled.mean())*H.conj()/(abs(H)**2+1e-10), n)
    margin = max(32, 2*len(c.signal_taps))
    from dataclasses import replace
    cfg = replace(cfg0, nuisance_orders=(0,),
        coherent_extra_orders=(), bandlimited_refine=False, quantization_refine=False,
        observation_aperture_samples=0.)
    with threadpool_limits(limits=1):
        if timing_seed is not None:
            # Reuse timing/morphology only. Contaminated amplitude and DC
            # never enter the calibrated forward projection.
            kind = timing_seed['type']
            frequency = float(timing_seed['frequency_hz'])
            duty = float(timing_seed['duty_fraction'])
            diag = timing_seed['diagnostics']
            rise = float(diag['square_rise_fraction'])
            fall = float(diag['square_fall_fraction'])
            if (type(kind) is not int or kind not in range(4) or
                    not all(math.isfinite(v) for v in (frequency, duty, rise, fall)) or
                    not 0 < frequency < sample_rate_hz/4 or
                    (kind == 1 and not (0 < duty < 1 and 0 <= rise <= .2 and 0 <= fall <= .2))):
                raise ValueError('invalid Vpp timing seed')
            seed = dict(type=kind, frequency_hz=frequency, duty_fraction=duty,
                diagnostics=dict(square_rise_fraction=rise, square_fall_fraction=fall))
            seed_polarity = None
        else:
            try:
                cfg = replace(cfg, max_seconds=max(.001, deadline-time.perf_counter()))
                check_time()
                seed = estimate(rough[margin:-margin], sample_rate_hz, cfg)
                seed_polarity = 1
            except ValueError:
                # Reversed saws need the opposite timing seed; retain signed
                # original observations in the final amplitude projection.
                check_time()
                cfg = replace(cfg, max_seconds=max(.001, deadline-time.perf_counter()))
                seed = estimate(-rough[margin:-margin], sample_rate_hz, cfg)
                seed_polarity = -1
        if expected_kind is not None and seed['type'] != expected_kind:
            raise ValueError('clean source class disagrees with original classification')
        candidates = [_forward_fit(cancelled, sample_rate_hz, c.signal_taps, seed,
                                 harmonics=h, deadline=deadline)
                      for h in harmonic_orders]
    best = min(candidates, key=lambda value: value['model_bic'])
    if (best['residual_rms_v'] > .01*best['vpp_v'] or
            max(abs(x-1) for x in best['half_gain_ratio']) > .005):
        raise ValueError('unmodelled residual or unstable source amplitude')
    check_time()
    result = dict(best, method='independent_reference_fixed_FIR_forward_projection',
        type=seed['type'], calibration_sha256=c.sha256,
        timing_seed_polarity=seed_polarity,
        reused_timing_seed=timing_seed is not None,
        total_budget_s=cfg0.max_seconds,
        observations_sha256=_digest(main_codes, observation.reference_codes),
        source_on_adaptation=False, absolute_accuracy_verified=False,
        accuracy_0p1_percent_certified=False,
        calibration_validation=c.evidence,
        note='Residual and time-half agreement are diagnostics, not absolute-error bounds.')
    check_time()
    result['total_elapsed_s'] = time.perf_counter()-started
    return result
