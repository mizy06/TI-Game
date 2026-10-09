"""Independent ARB playback transfer checks; never applied to competition R."""
from dataclasses import asdict, dataclass
import math

import numpy as np

from tools.acquisition_calibration import counts, sine_period


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

    def __post_init__(self):
        if (not all(math.isfinite(v) for v in asdict(self).values()) or
                not .8 <= self.amplitude_gain <= 1.2 or not .8 <= self.offset_gain <= 1.2 or
                abs(self.offset_bias_v) > .2):
            raise ValueError("implausible ARB playback response; check the generator/ADC path")

    def apply(self, config):
        result = dict(config)
        result["requested_amplitude_v"] = config["amplitude_v"]
        result["requested_offset_v"] = config["offset_v"]
        result["amplitude_v"] = config["amplitude_v"] / self.amplitude_gain
        result["offset_v"] = (config["offset_v"] - self.offset_bias_v) / self.offset_gain
        result["generator_calibration"] = asdict(self)
        return result


def fit_playback(observations, full_scale_v):
    """Separately estimate offset command and waveform amplitude response."""
    if len(observations) != 5:
        raise ValueError("ARB calibration requires five independent DC references")
    design, measured = [], []
    for item in observations:
        raw = counts(item["payload"])
        if np.any((raw <= 0) | (raw >= 4095)) or float(np.std(raw)) > 15:
            raise ValueError("ARB DC reference is clipped or unstable")
        config, ref = item["config"], item["reference"]
        design.append([ref["dc_v"] - config["offset_v"], config["offset_v"], 1.])
        measured.append(float(raw.mean()) * full_scale_v / 4095)
    coefficients, _, rank, _ = np.linalg.lstsq(design, measured, rcond=None)
    if rank != 3:
        raise ValueError("ARB calibration references do not separate amplitude and offset")
    profile = GeneratorCalibration(*map(float, coefficients))
    error = float(np.max(np.abs(np.asarray(design) @ coefficients - measured)))
    if error > max(.005, .002 * full_scale_v):
        raise ValueError("ARB voltage response is nonlinear; playback calibration rejected")
    return profile, {"dc_fit_max_error_v": error}


def verify_playback(payload, meta, expected):
    raw = counts(payload)
    if np.any((raw <= 0) | (raw >= 4095)):
        raise ValueError("independent ARB check is clipped")
    scale = meta.reference_microvolts / (meta.gain_ppm * 4095)
    dc_error = abs(float(raw.mean()) * scale - expected["dc_v"])
    if dc_error > max(.005, .002 * scale * 4095):
        raise ValueError("independent ARB DC check failed")
    result = {"reference": expected, "dc_error_v": dc_error}
    if expected["wave"] == "dc":
        if float(np.std(raw)) > 15:
            raise ValueError("independent ARB DC check is unstable")
    else:
        frequency = meta.sample_rate_millihz / 1000 / sine_period(payload)
        error = abs(frequency / expected["frequency_hz"] - 1)
        span = float(np.percentile(raw, 99) - np.percentile(raw, 1)) * scale
        if error > .0015 or not math.isclose(span, expected["vpp_v"], rel_tol=.04):
            raise ValueError("independent ARB frequency/amplitude check failed")
        result.update(frequency_error_fraction=error, amplitude_v=span)
    return result
