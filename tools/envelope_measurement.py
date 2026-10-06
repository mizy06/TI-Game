"""Blind source-envelope measurement for the host R/RT pipeline.

The C cascade supplies class, period and duty. Joint least squares separates a
periodic carrier, reproducible shape distortion, and a coherent residual or AM.
The reconstructed clean envelope supplies total Vpp and its midpoint (the source
DC setting). No test case, label, truth value or impairment parameters enter here.
Only NumPy is needed; these larger workspaces stay on the computer, not the MCU.
"""
from __future__ import annotations

import math
import numpy as np

TAU = 2*math.pi


def _minimum(function, lower, upper, tolerance):
    """Bounded golden-section search; include endpoints for locked ADC phases."""
    ratio = (math.sqrt(5)-1)/2
    a, b = lower, upper
    x1, x2 = b-ratio*(b-a), a+ratio*(b-a)
    f1, f2 = function(x1), function(x2)
    for _ in range(48):
        if b-a <= tolerance:
            break
        if f1 <= f2:
            b, x2, f2 = x2, x1, f1
            x1 = b-ratio*(b-a)
            f1 = function(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a+ratio*(b-a)
            f2 = function(x2)
    return min((lower, upper, x1, x2), key=function)


def _fit(design, values):
    coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
    return coefficients, values-design@coefficients


def _shape(kind, phase, duty, rise=0., fall=0.):
    phase = phase % 1
    if kind == 2:
        return 1-4*np.abs((phase+.25)%1-.5)
    if kind == 3:
        return 1-2*phase
    values = np.where(phase < duty, 1., -1.)
    if rise:
        distance = (phase+.5)%1-.5
        values = np.where(np.abs(distance) <= rise/2, 2*distance/rise, values)
    if fall:
        distance = (phase-duty+.5)%1-.5
        values = np.where(np.abs(distance) <= fall/2, -2*distance/fall, values)
    return values


def reconstruct(samples, sample_rate_hz, frequency_hz, kind, duty=.5):
    """Return Vpp/counts, midpoint/counts and serializable model diagnostics."""
    values = np.asarray(samples, dtype=float)
    time = np.arange(len(values))/sample_rate_hz
    phase_time = time*frequency_hz
    fundamental = np.column_stack((np.ones(len(values)),
        np.sin(TAU*phase_time), np.cos(TAU*phase_time)))
    first, _ = _fit(fundamental, values)
    phase = float(np.arctan2(first[2], first[1])/TAU)
    if kind == 1:
        phase += duty/2-.25
    rise = fall = 0.
    fit_mask = np.ones(len(values), dtype=bool)
    if kind == 3:
        # ADC samples at a locked reset can fall on either side of the jump.
        # Infer its time from observed jump intervals, then omit its vicinity
        # from every regression. The linear ramp supplies amplitude by
        # extrapolation; a discontinuity must never become fitted harmonics.
        differences = np.diff(values)
        approximate_vpp = math.pi*float(np.hypot(first[1], first[2]))
        jumps = np.flatnonzero(np.abs(differences) > .55*approximate_vpp)
        if len(jumps) >= max(3, int(len(values)*frequency_hz/sample_rate_hz)//2):
            signed = float(np.median(differences[jumps]))
            jumps = jumps[differences[jumps]*signed > 0]
            jump_phase = (jumps+.5)*frequency_hz/sample_rate_hz
            angle = np.mean(np.exp(1j*TAU*jump_phase))
            if abs(angle) > .9:
                phase = float(-np.angle(angle)/TAU)
        fit_mask = np.abs(_shape(3, phase_time+phase, .5)) < .8

    def fit_record(design):
        coefficients, _ = _fit(design[fit_mask], values[fit_mask])
        residual = values-design@coefficients
        residual[~fit_mask] = 0.
        return coefficients, residual

    if kind == 1:
        # Fit each edge independently; unequal ramps must not become harmonic
        # distortion or create spurious narrowband residuals.
        parameters = [phase, duty, .03, .03]

        def edge_loss(trial):
            basis = np.column_stack((np.ones(len(values)), _shape(kind,
                phase_time+trial[0], trial[1], trial[2], trial[3])))
            _, residual = fit_record(basis)
            return float(residual@residual)

        for _ in range(3):
            for index, width in ((0, .02), (1, .02), (2, .10), (3, .10)):
                lower, upper = parameters[index]-width, parameters[index]+width
                if index == 1:
                    lower, upper = max(.1, lower), min(.9, upper)
                elif index > 1:
                    lower, upper = 0., .15

                def cost(value):
                    trial = parameters.copy()
                    trial[index] = value
                    return edge_loss(trial)

                parameters[index] = _minimum(cost, lower, upper, .00005)
        phase, duty, rise, fall = parameters
    highest = min(9 if kind else 15, int(sample_rate_hz/(2*frequency_hz))-1)
    harmonics = np.arange(2 if kind else 1, highest+1)
    angles = TAU*phase_time[:, None]*harmonics
    harmonic_basis = np.column_stack((np.sin(angles), np.cos(angles)))
    if kind:
        def phase_model(trial):
            basis = np.column_stack((np.ones(len(values)),
                _shape(kind, phase_time+trial, duty, rise, fall), harmonic_basis))
            coefficients, residual = fit_record(basis)
            return float(residual@residual), basis, coefficients

        offsets = np.linspace(-.02, .02, 17)
        candidates = [phase]
        if kind != 3:
            choices = []
            for center in candidates:
                losses = [phase_model(center+offset)[0] for offset in offsets]
                index = int(np.argmin(losses))
                trial = _minimum(lambda q: phase_model(q)[0],
                    center+offsets[max(0, index-1)], center+offsets[min(16, index+1)], 1e-7)
                choices.append((phase_model(trial)[0], trial))
            _, phase = min(choices)
        _, basis, coefficients = phase_model(phase)
    else:
        basis = np.column_stack((np.ones(len(values)), harmonic_basis))
        coefficients, _ = fit_record(basis)

    def design(at_time):
        p = np.asarray(at_time)*frequency_hz
        angle = TAU*p[:, None]*harmonics
        harmonic = np.column_stack((np.sin(angle), np.cos(angle)))
        if kind:
            return np.column_stack((np.ones(len(p)),
                _shape(kind, p+phase, duty, rise, fall), harmonic))
        return np.column_stack((np.ones(len(p)), harmonic))

    # Dense periodic reconstruction preserves continuous carrier extrema. For
    # a time-varying signal, the defined measurement covers this actual record.
    grid = np.arange(4096)/(4096*frequency_hz)
    periodic_signal = design(grid)@coefficients
    midpoint = float((periodic_signal.max()+periodic_signal.min())/2)
    initial_vpp = float(np.ptp(periodic_signal))
    residual = values-basis@coefficients
    residual[~fit_mask] = 0.
    residual_energy = float(residual@residual)
    if initial_vpp <= 0 or residual_energy <= 1e-12:
        return initial_vpp, midpoint, {"model": "periodic", "retained_harmonics": []}

    # Find an independent sinusoidal residual using the data alone. FFT is just
    # a seed; joint regression removes finite-window leakage from the carrier.
    frequencies = np.fft.rfftfreq(32768, 1/sample_rate_hz)
    spectrum = np.abs(np.fft.rfft(residual, 32768))
    eligible = (frequencies > 3) & (frequencies < min(8*frequency_hz, sample_rate_hz/4))
    for harmonic in range(1, 16):
        eligible &= np.abs(frequencies-harmonic*frequency_hz) > 5
    candidate = float(frequencies[np.argmax(np.where(eligible, spectrum, 0))])

    def sine_model(frequency):
        augmented = np.column_stack((basis, np.sin(TAU*frequency*time),
                                               np.cos(TAU*frequency*time)))
        fit, remaining = fit_record(augmented)
        return float(remaining@remaining), augmented, fit, remaining

    extra_frequency = _minimum(lambda f: sine_model(f)[0],
                               max(1., candidate-2), candidate+2, .005)
    sine_loss, sine_basis, sine_fit, sine_residual = sine_model(extra_frequency)
    sine_explained = 1-sine_loss/residual_energy
    sine_amplitude = float(np.hypot(sine_fit[-2], sine_fit[-1]))

    # Fit both AM sidebands jointly. A split-record check rejects incoherent
    # noise peaks even if searching many modulation frequencies finds a fit.
    centered_carrier = basis@coefficients-midpoint

    def am_model(frequency):
        augmented = np.column_stack((basis,
            centered_carrier*np.sin(TAU*frequency*time),
            centered_carrier*np.cos(TAU*frequency*time)))
        fit, remaining = fit_record(augmented)
        return float(remaining@remaining), augmented, fit, remaining

    modulation_grid = np.arange(3., 42., 2.)
    losses = [am_model(f)[0] for f in modulation_grid]
    seed = float(modulation_grid[np.argmin(losses)])
    modulation = _minimum(lambda f: am_model(f)[0], max(2., seed-2), seed+2, .005)
    am_loss, am_basis, am_fit, am_residual = am_model(modulation)
    depth = float(np.hypot(am_fit[-2], am_fit[-1]))
    indices = np.arange(len(values))
    halves = (fit_mask & (indices < len(values)//2),
              fit_mask & (indices >= len(values)//2))
    split_am = [_fit(am_basis[half], values[half])[0][-2:] for half in halves]
    coherence_error = float(np.linalg.norm(split_am[0]-split_am[1])/max(depth, 1e-12))
    am_explained = 1-am_loss/residual_energy
    # A smaller variance fraction can still be significant in strong noise
    # when two independently fitted half-records reproduce the same AM vector.
    credible_am = ((am_explained > .10 and coherence_error < .7) or
                   (am_explained > .06 and coherence_error < .25))
    model, extra = "periodic", None
    original_coefficients = coefficients.copy()
    if (credible_am and .025 < depth < .30 and am_loss < sine_loss*.96):
        model, extra = "am", float(modulation)
        coefficients, residual = am_fit, am_residual
    elif sine_explained > .25 and 2*sine_amplitude/initial_vpp > .02:
        # The same temporal coherence requirement also applies to additive tones.
        split_sine = [_fit(sine_basis[half], values[half])[0][-2:] for half in halves]
        if np.linalg.norm(split_sine[0]-split_sine[1]) < .7*sine_amplitude:
            model, extra = "coherent_sine", float(extra_frequency)
            coefficients, residual = sine_fit, sine_residual

    # Suppress random periodic residuals in an otherwise ideal carrier. Smooth
    # nonlinear shapes have a harmonic tail: once a reproducible low harmonic
    # establishes this shape, retain its full tail to reconstruct clipped peaks.
    base_columns = 2 if kind else 1
    split = [_fit(basis[half], values[half])[0] for half in halves]
    periodic_coefficients = coefficients[:basis.shape[1]].copy()
    evidence = []
    for index, harmonic in enumerate(harmonics):
        columns = [base_columns+index, base_columns+len(harmonics)+index]
        magnitude = float(np.linalg.norm(periodic_coefficients[columns]))
        difference = float(np.linalg.norm(split[0][columns]-split[1][columns]))
        evidence.append((int(harmonic), columns, magnitude, difference))
    smooth_distortion = kind == 0 and any(2 <= h <= 7 and mag > .0075*initial_vpp
        and diff < .7*mag for h, _, mag, diff in evidence)
    retained = []
    for harmonic, columns, magnitude, difference in evidence:
        keep = (kind == 0 and harmonic == 1) or smooth_distortion or (
            magnitude >= .0025*initial_vpp and difference <= .7*magnitude)
        if keep:
            retained.append(harmonic)
        else:
            periodic_coefficients[columns] = 0.
    if model == "periodic":
        clean = design(grid)@periodic_coefficients
    else:
        record_basis = design(time)
        clean = record_basis@periodic_coefficients
        if model == "coherent_sine":
            clean += coefficients[-2]*np.sin(TAU*extra*time)+coefficients[-1]*np.cos(TAU*extra*time)
        else:
            carrier = record_basis@original_coefficients-midpoint
            clean += carrier*(coefficients[-2]*np.sin(TAU*extra*time)
                             +coefficients[-1]*np.cos(TAU*extra*time))
    return float(np.ptp(clean)), float((clean.max()+clean.min())/2), {
        "model": model, "extra_frequency_hz": extra,
        "retained_harmonics": retained,
        "residual_rms_counts": float(np.sqrt(np.mean(residual*residual))),
        "am_depth": depth if model == "am" else 0.,
        "candidate_am_depth": depth,
        "am_explained_fraction": float(1-am_loss/residual_energy),
        "am_coherence_error_ratio": coherence_error,
        "sine_explained_fraction": float(sine_explained),
        "square_rise_fraction": float(rise), "square_fall_fraction": float(fall)}


def refine(payload, metadata, result):
    """Update the same output fields used by both R and RT; retain the C result."""
    vpp, midpoint, diagnostics = reconstruct(np.frombuffer(payload, dtype="<u2"),
        metadata.sample_rate_millihz/1000, result["frequency_millihz"]/1000,
        result["type"], result["duty_ppm"]/1e6)
    if not (math.isfinite(vpp) and math.isfinite(midpoint) and vpp > 0):
        raise ValueError("envelope reconstruction did not produce a finite signal")
    factor = metadata.reference_microvolts*1e6/(metadata.adc_full_scale*metadata.gain_ppm)
    result["carrier_result"] = {name: result[name] for name in
        ("vpp_microvolts", "vpp_millivolts", "vpp_counts_q16", "dc_offset_counts")}
    result["vpp_counts_q16"] = int(round(vpp*65536))
    result["vpp_microvolts"] = int(round(vpp*factor))
    result["vpp_millivolts"] = int((result["vpp_microvolts"]+500)//1000)
    result["dc_offset_counts"] = int(round(np.clip(midpoint, 0, metadata.adc_full_scale)))
    result["quality_flags"] |= 1 << 7
    result["envelope_measurement"] = {**diagnostics, "vpp_counts": vpp,
        "midpoint_counts": midpoint,
        "vpp_definition": "reconstructed clean signal peak-to-peak",
        "dc_definition": "configured source offset (clean envelope midpoint)",
        "scope": "clean_period" if diagnostics["model"] == "periodic" else "clean_record"}
