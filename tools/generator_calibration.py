"""Independent ARB playback transfer checks; never applied to competition R."""
from dataclasses import asdict, dataclass
import math

import numpy as np

from tools.acquisition_calibration import counts, sine_harmonic_vpp_counts, sine_period


def reference(full_scale_v, level=.5, *, wave="dc", offset_v=None):
    """Dedicated 4000-point references, unrelated to any RT matrix sample."""
    rate, size, frequency = 20000., 4000, 250.
    if wave == "dc":
        normalized = np.full(size, 2 * level - 1)
    elif wave == "sine":
        normalized = .3 * np.sin(2 * np.pi * frequency * np.arange(size) / rate)
    else:
        raise ValueError("unsupported ARB calibration reference")
    words = np.rint(normalized * 32767).astype("<i2")
    offset = .5 * full_scale_v if offset_v is None else offset_v
    config = {"amplitude_v": full_scale_v, "offset_v": offset,
              "sample_rate_hz": rate, "loop_frequency_hz": rate / size}
    volts = offset + .5 * full_scale_v * words.astype(float) / 32767
    expected = {"wave": wave, "dc_v": float(volts.mean()), "vpp_v": float(np.ptp(volts)),
                "frequency_hz": frequency if wave == "sine" else 0.}
    return words.tobytes(), config, expected


@dataclass(frozen=True)
class GeneratorCalibration:
    amplitude_gain: float
    offset_gain: float
    offset_bias_v: float
    offset_origin_v: float | None = None
    offset_span_v: float | None = None

    def __post_init__(self):
        working_bias = self.offset_bias_v
        if self.offset_origin_v is not None:
            working_bias += (self.offset_gain - 1) * self.offset_origin_v
        if (not all(math.isfinite(v) for v in asdict(self).values() if v is not None) or
                not .8 <= self.amplitude_gain <= 1.2 or not .8 <= self.offset_gain <= 1.2 or
                abs(working_bias) > .2):
            raise ValueError("implausible ARB playback response; check the generator/ADC path")
        if ((self.offset_origin_v is None) != (self.offset_span_v is None) or
                (self.offset_span_v is not None and self.offset_span_v <= 0)):
            raise ValueError("invalid ARB calibration working point")

    def apply(self, config):
        result = dict(config)
        result["requested_amplitude_v"] = config["amplitude_v"]
        result["requested_offset_v"] = config["offset_v"]
        result["amplitude_v"] = config["amplitude_v"] / self.amplitude_gain
        result["offset_v"] = (config["offset_v"] - self.offset_bias_v) / self.offset_gain
        if (self.offset_origin_v is not None and any(
                abs(value - self.offset_origin_v) > self.offset_span_v + 1e-9
                for value in (config["offset_v"], result["offset_v"]))):
            raise ValueError("ARB offset compensation exceeds the calibrated working point; "
                             "refusing to extrapolate the generator response")
        result["generator_calibration"] = asdict(self)
        return result


def fit_playback(observations, full_scale_v):
    """Fit AMP and local OFST response around the matrix's operating point."""
    if len(observations) != 5:
        raise ValueError("ARB calibration requires five independent DC references")
    origin = observations[1]["config"]["offset_v"]
    if any(not math.isclose(x["config"]["offset_v"], origin, abs_tol=1e-9)
           for x in observations[:3]):
        raise ValueError("ARB amplitude references must share the matrix working point")
    lower, upper = sorted(x["config"]["offset_v"] for x in observations[3:])
    span = min(origin - lower, upper - origin)
    if (span <= 0 or max(origin - lower, upper - origin) > .05 * full_scale_v or
            any(not math.isclose(x["reference"]["dc_v"], x["config"]["offset_v"], abs_tol=1e-9)
                for x in observations[3:])):
        raise ValueError("ARB OFST references must bracket the matrix working point locally")
    design, measured = [], []
    for item in observations:
        raw = counts(item["payload"])
        if np.any((raw <= 0) | (raw >= 4095)) or float(np.std(raw)) > 15:
            raise ValueError("ARB DC reference is clipped or unstable")
        config, ref = item["config"], item["reference"]
        design.append([ref["dc_v"] - config["offset_v"], config["offset_v"] - origin, 1.])
        voltage = float(item.get("measured_dc_v", float(raw.mean()) * full_scale_v / 4095))
        if not math.isfinite(voltage) or not 0 < voltage < full_scale_v:
            raise ValueError("invalid calibrated ARB reference voltage")
        measured.append(voltage)
    design_arr = np.asarray(design, dtype=float)
    coefficients, _, rank, singular = np.linalg.lstsq(design_arr, measured, rcond=None)
    if rank != 3:
        raise ValueError("ARB calibration references do not separate amplitude and offset")
    amplitude_gain, offset_gain, center = map(float, coefficients)
    profile = GeneratorCalibration(amplitude_gain, offset_gain, center - offset_gain * origin,
                                   offset_origin_v=origin, offset_span_v=span)
    residual = design_arr @ coefficients - measured
    error = float(np.max(np.abs(residual)))
    limit = max(.005, .002 * full_scale_v)
    if error > limit:
        raise ValueError(f"ARB response at OFST={origin:.6g} V is nonlinear: "
                         f"maximum fit error {error * 1000:.3f} mV exceeds {limit * 1000:.3f} mV; "
                         "playback calibration rejected")
    cond = float(singular[0] / singular[-1]) if len(singular) and singular[-1] > 0 else float("inf")
    return profile, {"dc_fit_max_error_v": error, "dc_fit_residuals_v": residual.tolist(),
                     "measured_dc_v": measured, "offset_origin_v": origin,
                     "offset_valid_range_v": [origin - span, origin + span],
                     "design_condition_number": cond}


def playback_voltages(raw, meta):
    """Use the same unrounded ADC voltage conversion for fitting and checks."""
    if getattr(meta, "vpp_transfer", None) is not None:
        from tools.amplitude_calibration import voltage_values
        return voltage_values(raw, meta.vpp_transfer)
    scale = meta.reference_microvolts / (meta.gain_ppm * 4095)
    return ((raw - getattr(meta, "adc_offset_counts", 0.)) /
            getattr(meta, "adc_scale", 1.)) * scale


def verify_playback(payload, meta, expected):
    raw = counts(payload)
    if np.any((raw <= 0) | (raw >= 4095)):
        raise ValueError("independent ARB check is clipped")
    scale = meta.reference_microvolts / (meta.gain_ppm * 4095)
    volts = playback_voltages(raw, meta)
    dc_error = abs(float(volts.mean()) - expected["dc_v"])
    if dc_error > max(.005, .002 * scale * 4095):
        raise ValueError("independent ARB DC check failed")
    result = {"reference": expected, "dc_error_v": dc_error}
    if expected["wave"] == "dc":
        if float(np.std(raw)) > 15:
            raise ValueError("independent ARB DC check is unstable")
    else:
        period = sine_period(payload)
        fs_hz = (meta.sample_rate_hz_exact if getattr(meta, "sample_rate_hz_exact", None) is not None
                 else meta.sample_rate_millihz / 1000)
        frequency = fs_hz / period
        error = abs(frequency / expected["frequency_hz"] - 1)
        span = float(np.percentile(volts, 99) - np.percentile(volts, 1))
        exact_vpp = sine_harmonic_vpp_counts(volts, period)
        if error > .0015 or not math.isclose(span, expected["vpp_v"], rel_tol=.04):
            raise ValueError("independent ARB frequency/amplitude check failed")
        result.update(frequency_error_fraction=error, amplitude_v=span,
                      harmonic_amplitude_v=exact_vpp,
                      ac_amplitude_error_v=abs(exact_vpp - expected["vpp_v"]))
    return result
