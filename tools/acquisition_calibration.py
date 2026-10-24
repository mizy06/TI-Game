"""Generator-referenced calibration at the UART input boundary, before analysis.

Wire samples/metadata remain archived unchanged. No matrix truth or pass limit
is used to fit a calibration, and saturated samples are never reconstructed.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import atomic_json

try:
    from threadpoolctl import threadpool_limits
except ModuleNotFoundError:
    def threadpool_limits(*_args, **_kwargs):
        return nullcontext()


def signature(meta):
    return {key: getattr(meta, key) for key in (
        "sample_rate_millihz", "reference_microvolts", "gain_ppm", "adc_full_scale")}


def counts(payload):
    import numpy as np
    if len(payload) not in (host.PAYLOAD_BYTES, host.MAX_PAYLOAD_BYTES):
        raise ValueError("calibration requires a complete ADC frame")
    data = np.frombuffer(payload, dtype="<u2").astype(float)
    if np.any(data > 4095):
        raise ValueError("calibration received invalid ADC codes")
    return data


def sine_harmonic_fit_counts(data, period):
    """Exact least-squares DC center and fundamental Vpp in ADC counts."""
    import numpy as np
    angles = 2.0 * math.pi * np.arange(len(data), dtype=float) / float(period)
    basis = np.column_stack((np.ones(len(data)), np.sin(angles), np.cos(angles)))
    coef = np.linalg.lstsq(basis, data, rcond=None)[0]
    return float(coef[0]), float(2.0 * math.hypot(float(coef[1]), float(coef[2])))


def sine_harmonic_vpp_counts(data, period):
    """Exact least-squares fundamental peak-to-peak amplitude in ADC counts."""
    return sine_harmonic_fit_counts(data, period)[1]


def sine_period(payload):
    """Estimate cycles in sample indices, independently of the host classifier."""
    import numpy as np
    data = counts(payload)
    low, high = np.percentile(data, [2, 98])
    if high - low < 128 or np.any((data <= 0) | (data >= 4095)):
        raise ValueError("calibration sine is absent, too small or clipped; check CH1/CH2, PA25 and GND")
    middle, hysteresis = .5 * (low + high), .1 * (high - low)
    crossings, armed = [], False
    for index in range(1, len(data)):
        if data[index] < middle - hysteresis:
            armed = True
        if armed and data[index - 1] < middle <= data[index]:
            crossings.append(index - 1 + (middle - data[index - 1]) /
                             (data[index] - data[index - 1]))
            armed = False
    if len(crossings) < 8:
        raise ValueError("calibration sine has too few stable cycles")
    intervals = np.diff(crossings)
    period = float(np.polyfit(np.arange(len(crossings)), crossings, 1)[0])
    if period <= 0 or float(np.max(np.abs(intervals / period - 1))) > .03:
        raise ValueError("calibration sine periods are unstable; check the input signal")
    from tools.precision_measurement import harmonic_frequency
    with threadpool_limits(limits=1, user_api="blas"):
        frequency, diagnostic = harmonic_frequency(data, 20000., 20000. / period, highest=3)
    if not diagnostic["accepted"]:
        raise ValueError("calibration sine fit failed")
    return 20000. / frequency


@dataclass(frozen=True)
class AcquisitionCalibration:
    sample_rate_millihz: int
    adc_scale: float
    adc_offset_counts: float
    nominal_metadata: dict
    sample_rate_hz_exact: float | None = None
    vpp_transfer: dict | None = None

    def __post_init__(self):
        if self.vpp_transfer is not None:
            from tools.amplitude_calibration import validate_transfer
            validate_transfer(self.vpp_transfer)
        nominal = self.nominal_metadata
        if (not 1 <= self.sample_rate_millihz <= 100000000 or
                not math.isfinite(self.adc_scale) or not .8 <= self.adc_scale <= 1.2 or
                not math.isfinite(self.adc_offset_counts) or abs(self.adc_offset_counts) > 205 or
                set(nominal) != {"sample_rate_millihz", "reference_microvolts", "gain_ppm", "adc_full_scale"} or
                nominal["adc_full_scale"] != 4095 or any(v <= 0 for v in nominal.values()) or
                not .8 <= self.sample_rate_millihz / nominal["sample_rate_millihz"] <= 1.2):
            raise ValueError("invalid acquisition calibration or implausible hardware response")
        if self.sample_rate_hz_exact is not None and (
                type(self.sample_rate_hz_exact) not in (int, float) or
                not math.isfinite(self.sample_rate_hz_exact) or
                abs(self.sample_rate_hz_exact - self.sample_rate_millihz / 1000) > .000501):
            raise ValueError("exact sampling rate disagrees with compatibility metadata")

    def apply_exact(self, payload, meta):
        """Keep wire codes bit-for-bit; apply the affine model only in float64."""
        rate = self.sample_rate_hz_exact or self.sample_rate_millihz / 1000
        if signature(meta) != self.nominal_metadata:
            # S/T/U change only the timer period, on the same external clock.
            # Recognize the advertised finite set; arbitrary metadata changes
            # still invalidate the saved hardware calibration.
            rates = {round(32000000000 / ticks): ticks for ticks in range(1500, 1701)}
            before = self.nominal_metadata['sample_rate_millihz']
            after = meta.sample_rate_millihz
            same_adc = all(getattr(meta, k) == v for k, v in self.nominal_metadata.items()
                           if k != 'sample_rate_millihz')
            if not (meta.flags & 8 and same_adc and before in rates and after in rates):
                raise ValueError("board metadata changed since calibration; run RO calibration again")
            rate *= rates[before] / rates[after]
        counts(payload)  # Validate codes before tagging the physical conversion.
        return payload, replace(meta, sample_rate_millihz=round(rate * 1000),
            sample_rate_hz_exact=rate,
            adc_scale=self.adc_scale, adc_offset_counts=self.adc_offset_counts,
            vpp_transfer=self.vpp_transfer,
            flags=meta.flags | 6)

    def apply(self, payload, meta):
        import numpy as np
        if signature(meta) != self.nominal_metadata:
            raise ValueError("board metadata changed since calibration; run RO calibration again")
        raw = counts(payload)
        calibrated = np.rint((raw - self.adc_offset_counts) / self.adc_scale)
        # Keep the original rail samples visible to the classifier's clipping
        # checks. An affine correction cannot recover lost analogue information.
        calibrated[raw == 0] = 0
        calibrated[raw == meta.adc_full_scale] = meta.adc_full_scale
        result = np.clip(calibrated, 0, meta.adc_full_scale).astype("<u2").tobytes()
        effective = replace(meta, sample_rate_millihz=self.sample_rate_millihz,
                            flags=meta.flags | 6, checksum=host.fnv1a(result))
        return result, effective

    def to_dict(self):
        value = asdict(self)
        if value["sample_rate_hz_exact"] is None:
            del value["sample_rate_hz_exact"]  # Preserve old schema-1 checksums.
        if value['vpp_transfer'] is None:
            del value['vpp_transfer']
        return value

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or
                type(value.get("sample_rate_millihz")) is not int or
                any(type(value.get(k)) not in (int, float) for k in ("adc_scale", "adc_offset_counts")) or
                not isinstance(value.get("nominal_metadata"), dict) or
                any(type(v) is not int for v in value["nominal_metadata"].values())):
            raise ValueError("malformed acquisition calibration parameters")
        return cls(**value)


@dataclass(frozen=True)
class CalibrationProfile:
    calibration: AcquisitionCalibration
    source: dict

    def to_dict(self):
        return {"schema": 1, "acquisition_calibration": self.calibration.to_dict(),
                "calibration_source": self.source}


def calibration_sha256(calibration):
    return hashlib.sha256(json.dumps(calibration.to_dict(), sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def load_latest_calibration(path):
    """Load portable algorithm parameters; archives are provenance, not a dependency."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ValueError(f"cannot read saved algorithm parameters in {path}: {error}") from error
    try:
        document = json.loads(text)
        if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
            raise ValueError("unsupported algorithm parameters schema")
        calibration, source = document["acquisition_calibration"], document["calibration_source"]
        if calibration is None and source is None:
            return None
        calibration = AcquisitionCalibration.from_dict(calibration)
        if (not isinstance(source, dict) or source.get("command") not in ("RO", "RS") or
                any(not isinstance(source.get(k), str) or not source[k] for k in
                    ("calibrated_utc", "calibration_report"))):
            raise ValueError("missing successful RO calibration provenance")
        stamp = datetime.fromisoformat(source["calibrated_utc"])
        if stamp.tzinfo is None:
            raise ValueError("calibration timestamp must include its timezone")
        if source.get("calibration_sha256") != calibration_sha256(calibration):
            raise ValueError("saved RO calibration checksum differs")
        return CalibrationProfile(calibration, source)
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError(f"invalid saved algorithm parameters in {path}: {error}") from error


def save_latest_calibration(path, report):
    """Publish only a verified RO calibration; an interrupted write keeps the old one."""
    path, report = Path(path), Path(report).resolve()
    document = json.loads(report.read_text(encoding="utf-8"))
    if (document.get("complete") is not True or document.get("error") or
            document.get("references_independent_of_matrix") is not True or
            not document.get("verification", {}).get("independent_checks")):
        raise ValueError("only a successful independent RO calibration can update algorithm parameters")
    calibration = AcquisitionCalibration.from_dict(document["calibration"])
    profile = CalibrationProfile(calibration, {
        "command": "RS" if report.parent.name.startswith('rs-') else "RO", "calibrated_utc": datetime.now(timezone.utc).isoformat(),
        "calibration_report": str(report),
        "calibration_sha256": calibration_sha256(calibration)})
    atomic_json(path, profile.to_dict())
    return profile


def fit_calibration(observations):
    """Fit only dedicated references; require independent voltage/time checks."""
    import numpy as np
    if not observations:
        raise ValueError("no calibration reference captures")
    nominal = signature(observations[0]["meta"])
    if any(signature(x["meta"]) != nominal for x in observations):
        raise ValueError("inconsistent board metadata during calibration")
    dc = [x for x in observations if x["reference"]["wave"] == "dc" and not x["validation"]]
    tones = [x for x in observations if x["reference"]["wave"] == "sine" and not x["validation"]]
    checks = [x for x in observations if x["validation"]]
    if (len(dc) != 3 or len(tones) != 2 or
            {x["reference"]["wave"] for x in checks} != {"dc", "sine"} or
            sum(x["reference"]["wave"] == "dc" for x in checks) < 2):
        raise ValueError("calibration requires three DC levels, two tones and independent checks")
    dc_values = [counts(x["payload"]) for x in dc]
    if any(np.any((v <= 0) | (v >= 4095)) or float(np.std(v)) > 15 for v in dc_values):
        raise ValueError("calibration DC is clipped or unstable; check the generator/ADC connection")
    volts = np.array([x["reference"]["offset_v"] for x in dc])
    means = np.array([v.mean() for v in dc_values])
    slope, offset = np.polyfit(volts, means, 1)
    if not np.isfinite(slope) or slope <= 0 or np.ptp(volts) <= 0:
        raise ValueError("ADC did not track the reference DC levels; check the connection")
    quad_curvature = float(np.polyfit(volts, means, 2)[0]) if len(volts) >= 3 else 0.0
    nominal_codes_per_volt = nominal["adc_full_scale"] * nominal["gain_ppm"] / nominal["reference_microvolts"]
    periods = [sine_period(x["payload"]) for x in tones]
    rates = [p * x["reference"]["frequency_hz"] for p, x in zip(periods, tones)]
    # Fisher information for stationary sinusoid frequency estimation scales with f^2.
    fisher_weights = [float(x["reference"]["frequency_hz"]) ** 2 for x in tones]
    rate = float(np.average(rates, weights=fisher_weights))
    unweighted_rate = float(np.mean(rates))
    if max(abs(r / rate - 1) for r in rates) > .0015:
        raise ValueError("reference tones disagree on the ADC timebase; check the input signal")
    tone_ac_gains = []
    tone_centers = []
    tone_offsets_v = []
    for p, x in zip(periods, tones):
        raw_tone = counts(x["payload"])
        c0, vpp_counts = sine_harmonic_fit_counts(raw_tone, p)
        tone_centers.append(c0)
        tone_offsets_v.append(float(x["reference"]["offset_v"]))
        exact_vpp = vpp_counts / float(slope)
        tone_ac_gains.append(float(exact_vpp / x["reference"]["amplitude_v"])
                             if x["reference"]["amplitude_v"] > 0 else 1.0)
    # When DDS SINE and DDS DC share the same physical offset DAC within 0.5 LSB,
    # include the phase-unbiased harmonic-projected sine centers in the midpoint fit.
    if all(abs(c0 - (slope * v_off + offset)) <= 0.5 for c0, v_off in zip(tone_centers, tone_offsets_v)):
        slope, offset = np.polyfit(np.r_[volts, tone_offsets_v], np.r_[means, tone_centers], 1)
    calibration = AcquisitionCalibration(round(rate * 1000), float(slope / nominal_codes_per_volt),
                                         float(offset), nominal, sample_rate_hz_exact=rate)
    full_scale_v = nominal["reference_microvolts"] / nominal["gain_ppm"]
    voltage_limit = max(.005, .002 * full_scale_v)
    residual = float(np.max(np.abs((means - offset) / slope - volts)))
    if residual > voltage_limit:
        raise ValueError("ADC reference DC response is nonlinear; calibration rejected")
    verification = []
    for observation in checks:
        ref, raw = observation["reference"], counts(observation["payload"])
        if ref["wave"] == "dc":
            error = abs(float((raw.mean() - offset) / slope) - ref["offset_v"])
            if (np.any((raw <= 0) | (raw >= 4095)) or float(np.std(raw)) > 15 or
                    error > voltage_limit):
                raise ValueError("independent DC calibration check failed")
            verification.append({"reference": ref, "dc_error_v": error})
        else:
            check_period = sine_period(observation["payload"])
            measured_rate = check_period * ref["frequency_hz"]
            error = abs(measured_rate / rate - 1)
            if error > .0015:
                raise ValueError("independent frequency calibration check failed")
            # Independently check the AC path too: DC-only calibration must not
            # conceal a generator amplitude or analogue bandwidth problem.
            span = float(np.percentile(raw, 99) - np.percentile(raw, 1)) / slope
            exact_vpp = sine_harmonic_vpp_counts(raw, check_period) / float(slope)
            if not math.isclose(span, ref["amplitude_v"], rel_tol=.04):
                raise ValueError("independent sine amplitude calibration check failed")
            verification.append({"reference": ref, "sample_rate_error_fraction": error,
                                 "amplitude_v": span, "harmonic_amplitude_v": exact_vpp,
                                 "ac_gain_ratio": float(exact_vpp / ref["amplitude_v"])
                                                  if ref["amplitude_v"] > 0 else 1.0})
    return calibration, {"dc_fit_max_error_v": residual,
                         "dc_quadratic_curvature_counts_per_v2": quad_curvature,
                         "reference_sample_rates_hz": rates,
                         "unweighted_sample_rate_hz": unweighted_rate,
                         "timebase_weighting": "fisher_f2",
                         "reference_rate_spread_ppm": float(np.ptp(rates) / rate * 1e6),
                         "reference_tone_ac_gains": tone_ac_gains,
                         "absolute_accuracy_verified": False,
                         "independent_checks": verification,
                         "note": "Dedicated generator references only; matrix results are not fitted. "
                                 "Voltage calibration is relative to the generator; clipped data stay clipped."}
