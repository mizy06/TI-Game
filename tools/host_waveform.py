"""Acquire ADC frames and run the independent unified estimator on the computer.

Output on stdout: type,frequency_hz,vpp_v,duty_pct,dc_offset_v.
The firmware sends DUMP v3 (8192 samples); legacy v2 (4096) remains readable.
"""
from __future__ import annotations

import ctypes
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SAMPLES = 4096
PAYLOAD_BYTES = SAMPLES * 2
MAX_PAYLOAD_BYTES = 8192 * 2
ABI_VERSION = 0x00020102
NAMES = ("sine", "square", "triangle", "sawtooth", "unknown")
ROOT = Path(__file__).resolve().parent
LIBRARY_NAME = ("waveform_analyzer_host.dll" if sys.platform == "win32" else
                "libwaveform_analyzer_host.dylib" if sys.platform == "darwin" else
                "libwaveform_analyzer_host.so")


class Frame(ctypes.Structure):
    _fields_ = [("samples", ctypes.POINTER(ctypes.c_uint16)),
                ("sample_count", ctypes.c_uint16),
                ("adc_full_scale", ctypes.c_uint16),
                ("sample_rate_millihz", ctypes.c_uint32),
                ("adc_reference_microvolts", ctypes.c_uint32),
                ("frontend_gain_ppm", ctypes.c_uint32),
                ("sequence", ctypes.c_uint32),
                ("flags", ctypes.c_uint32)]


class Result(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("status", ctypes.c_int),
                ("frequency_millihz", ctypes.c_uint32),
                ("vpp_millivolts", ctypes.c_uint32),
                ("duty_permille", ctypes.c_uint16),
                ("confidence_permille", ctypes.c_uint16),
                ("best_score_permille", ctypes.c_uint16),
                ("second_score_permille", ctypes.c_uint16),
                ("accepted_cycles", ctypes.c_uint16),
                ("high_clip_samples", ctypes.c_uint16),
                ("dc_offset_counts", ctypes.c_uint16),
                ("low_clip_samples", ctypes.c_uint16),
                ("invalid_samples", ctypes.c_uint16),
                ("algorithm_version", ctypes.c_uint32),
                ("sequence", ctypes.c_uint32),
                ("vpp_microvolts", ctypes.c_uint32),
                ("duty_ppm", ctypes.c_uint32),
                ("period_q16_samples", ctypes.c_uint32),
                ("vpp_counts_q16", ctypes.c_uint32),
                ("quality_flags", ctypes.c_uint16),
                ("model_margin_permille", ctypes.c_uint16)]


@dataclass(frozen=True)
class Metadata:
    sequence: int
    sample_rate_millihz: int
    reference_microvolts: int
    gain_ppm: int
    adc_full_scale: int
    flags: int
    checksum: int
    # Host-only exact calibration; never transmitted or passed through the ABI.
    sample_rate_hz_exact: float | None = None
    adc_scale: float = 1.
    adc_offset_counts: float = 0.
    payload_bytes: int = PAYLOAD_BYTES
    protocol_version: int = 2


def fnv1a(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def read_line(port) -> str:
    line = port.read_until(b"\n", 256)
    if not line.endswith(b"\n"):
        raise TimeoutError("serial line timeout or overlong line")
    return line.decode("ascii").rstrip("\r\n")


class FrameReceiveError(TimeoutError):
    """An incomplete UART frame; reacquisition is safe after resynchronizing."""


def read_exact(port, count: int, on_chunk=None) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = port.read(count - len(chunks))
        if not chunk:
            raise TimeoutError(f"serial timeout after {len(chunks)}/{count} bytes")
        chunks.extend(chunk)
        if on_chunk is not None:
            on_chunk(chunk)
    return bytes(chunks)


def _uint(fields: dict[str, str], key: str, low: int, high: int) -> int:
    value = fields[key]
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"invalid DUMP {key}")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"DUMP {key} outside expected range")
    return number


def parse_header(line: str) -> Metadata:
    parts = line.split(",")
    keys = {"proto", "bytes", "fnv1a", "seq", "fs_millihz", "vref_uv",
            "gain_ppm", "adc_max", "flags"}
    if parts[0] != "DUMP" or len(parts) != len(keys) + 1:
        raise ValueError("unsupported DUMP header; flash host protocol v2/v3 firmware")
    fields = {}
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        if sep != "=" or key in fields or key not in keys:
            raise ValueError("malformed DUMP header")
        fields[key] = value
    protocol = _uint(fields, "proto", 2, 3)
    size = _uint(fields, "bytes", PAYLOAD_BYTES, MAX_PAYLOAD_BYTES)
    if size not in (PAYLOAD_BYTES, MAX_PAYLOAD_BYTES) or (protocol == 2 and size != PAYLOAD_BYTES):
        raise ValueError("unexpected DUMP protocol or byte count")
    return Metadata(_uint(fields, "seq", 0, 0xffffffff),
                    _uint(fields, "fs_millihz", 1, 100000000),
                    _uint(fields, "vref_uv", 1, 10000000),
                    _uint(fields, "gain_ppm", 1, 10000000),
                    _uint(fields, "adc_max", 4095, 4095),
                    _uint(fields, "flags", 0, 15),
                    _uint(fields, "fnv1a", 0, 0xffffffff),
                    payload_bytes=size, protocol_version=protocol)


def receive_frame(port, *, first_line=None, store=None):
    """Receive without discarding buffered frames; archive before validation."""
    record = None
    for _ in range(16):
        line = first_line if first_line is not None else read_line(port)
        first_line = None
        if line.startswith("ERR_"):
            raise RuntimeError(f"board capture failed: {line}")
        if line.startswith("DUMP,"):
            meta = None
            received = bytearray()

            def collect(chunk):
                received.extend(chunk)
                # ASCII trailers cannot occur in LE16 12-bit ADC samples.
                # A trailer inside the declared payload proves bytes were lost;
                # waiting for the remaining count would only waste the deadline.
                trailer = b"DUMP_END\r\nREADY\r\n"
                if received.endswith(trailer):
                    raise FrameReceiveError(
                        f"incomplete ADC frame: {len(received) - len(trailer)}/{meta.payload_bytes} "
                        "payload bytes before DUMP_END / READY")

            def archive():
                if store is None:
                    return None
                saved = store.begin("uart", {"raw_header": line})
                if meta is not None:
                    saved.set_metadata(meta)
                if received:
                    saved.append(received)
                return saved

            try:
                meta = parse_header(line)
                payload = read_exact(port, meta.payload_bytes, collect)
                # Consume the trailer even on a checksum failure to allow resync.
                end, ready = read_line(port), read_line(port)
                # Keep JSON creation, metadata fsyncs and per-frame file opens
                # off the live receive path. RecordingSerial already archives
                # every wire read, including partial/error responses.
                record = archive()
                if fnv1a(payload) != meta.checksum:
                    raise ValueError("DUMP FNV-1a integrity check failed")
                if end != "DUMP_END" or ready != "READY":
                    raise ValueError("missing DUMP_END / READY")
                if record:
                    record.info["calculated_fnv1a"] = fnv1a(payload)
                    record.complete(payload)
                return payload, meta, record
            except Exception as error:
                if record is None:
                    record = archive()
                if record:
                    if record.info["payload_bytes"] in (PAYLOAD_BYTES, MAX_PAYLOAD_BYTES):
                        record.info["calculated_fnv1a"] = fnv1a(record.payload.read_bytes())
                    record.fail(error)
                if isinstance(error, TimeoutError) and not isinstance(error, FrameReceiveError):
                    raise FrameReceiveError(str(error)) from error
                raise
        if line in ("CAPTURING", "READY") or line.startswith(("TI-CUP ", "ADC=", "READY:")) or not line:
            continue
        raise ValueError(f"unexpected board response: {line}")
    raise ValueError("board did not send a DUMP frame")


def dump(port, *, store=None, command=b"D") -> tuple[bytes, Metadata]:
    port.write(command)
    port.flush()
    payload, meta, _ = receive_frame(port, store=store)
    return payload, meta


def load_api(path: str):
    dll = ctypes.CDLL(path)
    for name in ("ti_waveform_abi_version", "ti_waveform_frame_size", "ti_waveform_result_size"):
        getattr(dll, name).restype = ctypes.c_uint32
    sizes = (dll.ti_waveform_abi_version(), dll.ti_waveform_frame_size(),
             dll.ti_waveform_result_size())
    expected = (ABI_VERSION, ctypes.sizeof(Frame), ctypes.sizeof(Result))
    if sizes != expected:
        raise RuntimeError(f"host library ABI mismatch: got {sizes}, expected {expected}; rebuild it")
    dll.ti_waveform_analyze_frame.argtypes = [ctypes.POINTER(Frame), ctypes.POINTER(Result)]
    dll.ti_waveform_analyze_frame.restype = ctypes.c_bool
    return dll


def analyze(dll, payload: bytes, meta: Metadata, *, config=None) -> tuple[str, dict]:
    """The retained DLL argument is ignored; no C stage supplies any seed."""
    import numpy as np
    from tools.unified_solver import estimate
    if len(payload) != meta.payload_bytes or len(payload) not in (PAYLOAD_BYTES, MAX_PAYLOAD_BYTES):
        raise ValueError("incomplete ADC payload")
    raw = np.frombuffer(payload, dtype="<u2")
    details = {name: 0 for name, _ in Result._fields_}
    details.update(ok=False, type=4, algorithm_version=0x00080000,
        sequence=meta.sequence, host_pipeline_version="8.0.0",
        classification_sample_count=len(raw), measurement_sample_count=len(raw),
        frequency_hz=0., vpp_v=0., duty_fraction=0., dc_offset_v=0.)
    def reject(status, reason):
        details.update(status=status, rejection_reason=reason)
        return "unknown,0,0,0,0", details
    fs = meta.sample_rate_hz_exact if meta.sample_rate_hz_exact is not None else meta.sample_rate_millihz/1000
    if (not np.isfinite(fs) or fs <= 0 or meta.adc_full_scale != 4095 or
            meta.reference_microvolts <= 0 or meta.gain_ppm <= 0 or
            not np.isfinite(meta.adc_scale) or meta.adc_scale <= 0 or
            not np.isfinite(meta.adc_offset_counts)):
        return reject(6, "invalid acquisition metadata")
    if not meta.flags & 1:
        return reject(6, "noncontiguous acquisition")
    details["invalid_samples"] = int(np.count_nonzero(raw > meta.adc_full_scale))
    if details["invalid_samples"]:
        return reject(6, "invalid ADC code")
    details["raw_high_rail_samples"] = int(np.count_nonzero(raw == 4095))
    details["raw_low_rail_samples"] = int(np.count_nonzero(raw == 0))
    details["high_clip_samples"] = int(np.count_nonzero(raw >= 4092))
    details["low_clip_samples"] = int(np.count_nonzero(raw <= 3))
    if details["high_clip_samples"] > len(raw)//20:
        return reject(2, "high rail saturation")
    if details["low_clip_samples"] > len(raw)//20:
        return reject(5, "low rail saturation")
    samples = (raw.astype(float)-meta.adc_offset_counts)/meta.adc_scale
    if np.percentile(samples, 98)-np.percentile(samples, 2) < 8:
        return reject(1, "insufficient dynamic range")
    try:
        result = estimate(samples, fs, config, quantization_step=1/meta.adc_scale)
    except ValueError as error:
        return reject(7, str(error))
    unit = meta.reference_microvolts/(meta.adc_full_scale*meta.gain_ppm)
    f, vpp, duty, dc = result["frequency_hz"], result["vpp"]*unit, result["duty_fraction"], result["dc"]*unit
    kind = result["type"]
    flags = (0 if meta.flags & 2 else 1) | (0 if meta.flags & 4 else 2)
    if kind == 3:
        flags |= 1 << 5  # ramp/jump ratio, amplitude extrapolated to reset
    details.update(ok=True, status=0, type=kind, frequency_hz=f, vpp_v=vpp,
        duty_fraction=duty, dc_offset_v=dc, frequency_millihz=round(f*1000),
        vpp_microvolts=round(vpp*1e6), vpp_millivolts=round(vpp*1000),
        duty_ppm=round(duty*1e6), duty_permille=round(duty*1000),
        dc_offset_counts=round(result["dc"]), period_q16_samples=round(fs/f*65536),
        vpp_counts_q16=round(result["vpp"]*65536),
        accepted_cycles=int(len(raw)*f/fs), quality_flags=flags,
        unified_solver=result["diagnostics"])
    return f"{NAMES[kind]},{f:.9f},{vpp:.9f},{duty*100:.7f},{dc:.9f}", details


def analyze_legacy(dll, payload: bytes, meta: Metadata) -> tuple[str, dict]:
    """Archived 3.0 pipeline, for comparison only; never called by R/RO/RT."""
    # Small regressions are slower with a large BLAS thread pool. This also
    # keeps the contest latency predictable on many-core laptops.
    from threadpoolctl import threadpool_limits
    with threadpool_limits(limits=1, user_api="blas"):
        return _analyze(dll, payload, meta)


def _analyze(dll, payload: bytes, meta: Metadata) -> tuple[str, dict]:
    if len(payload) != meta.payload_bytes or len(payload) not in (PAYLOAD_BYTES, MAX_PAYLOAD_BYTES):
        raise ValueError("incomplete ADC payload")
    import numpy as np
    raw = np.frombuffer(payload, dtype="<u2")
    classification = raw
    if np.all(raw <= meta.adc_full_scale) and (meta.adc_scale != 1. or meta.adc_offset_counts != 0.):
        # Preserve the legacy C classifier's calibrated near-rail thresholds.
        # This temporary integer copy is NEVER the precision measurement input.
        classification = np.clip(np.rint((raw.astype(float)-meta.adc_offset_counts)/meta.adc_scale),
                                 0, meta.adc_full_scale).astype("<u2")
        classification[raw == 0] = 0
        classification[raw == meta.adc_full_scale] = meta.adc_full_scale
    values = (ctypes.c_uint16 * SAMPLES).from_buffer_copy(classification[:SAMPLES].tobytes())
    frame = Frame(values, SAMPLES, meta.adc_full_scale,
                  int(round(meta.sample_rate_hz_exact*1000)) if meta.sample_rate_hz_exact else meta.sample_rate_millihz, meta.reference_microvolts,
                  meta.gain_ppm, meta.sequence, meta.flags)
    result = Result()
    ok = bool(dll.ti_waveform_analyze_frame(ctypes.byref(frame), ctypes.byref(result)))
    details = {name: int(getattr(result, name)) for name, _ in Result._fields_}
    details["ok"] = ok
    details["host_pipeline_version"] = "3.0.0"
    details["classification_sample_count"] = SAMPLES
    details["measurement_sample_count"] = int(len(raw))
    details["raw_high_rail_samples"] = int(np.count_nonzero(raw == meta.adc_full_scale))
    details["raw_low_rail_samples"] = int(np.count_nonzero(raw == 0))
    if np.any(raw > meta.adc_full_scale):
        details.update(ok=False, status=6, invalid_samples=int(np.count_nonzero(raw > meta.adc_full_scale)))
        return "unknown,0,0,0,0", details
    if len(raw) > SAMPLES:
        guard = max(1, meta.adc_full_scale//1024)
        details["high_clip_samples"] = int(np.count_nonzero(classification >= meta.adc_full_scale-guard))
        details["low_clip_samples"] = int(np.count_nonzero(classification <= guard))
        if np.count_nonzero(classification >= meta.adc_full_scale-guard) > len(raw)//20:
            details.update(ok=False, status=2)
            return "unknown,0,0,0,0", details
        if np.count_nonzero(classification <= guard) > len(raw)//20:
            details.update(ok=False, status=5)
            return "unknown,0,0,0,0", details
    if not ok:
        return "unknown,0,0,0,0", details
    if result.algorithm_version != ABI_VERSION or result.sequence != meta.sequence or not 0 <= result.type < len(NAMES) - 1:
        raise RuntimeError("host algorithm returned incompatible result")
    from tools.envelope_measurement import refine
    from tools.precision_measurement import refine_timing
    fs = meta.sample_rate_hz_exact or meta.sample_rate_millihz/1000
    samples = (np.frombuffer(payload, dtype="<u2").astype(float)-meta.adc_offset_counts)/meta.adc_scale
    frequency, duty, timing = refine_timing(samples, fs,
        details["frequency_millihz"]/1000, result.type, details["duty_ppm"]/1e6)
    details["frequency_hz"] = frequency
    details["duty_fraction"] = duty if result.type == 1 else 0.
    details["precision_timing"] = timing
    refine(payload, meta, details)
    details["c_timing_result"] = {"frequency_millihz": details["frequency_millihz"],
        "duty_ppm": details["duty_ppm"]}
    details["frequency_millihz"] = int(round(frequency*1000))
    details["duty_ppm"] = int(round(details["duty_fraction"]*1e6))
    details["duty_permille"] = int(round(details["duty_fraction"]*1000))
    duty_pct = details["duty_fraction"]*100
    # The DC estimate is measured at the ADC pin; undo the front-end gain.
    offset = details["dc_offset_v"]
    if result.type == 3:
        # With an abrupt saw reset between ADC samples, exact integer-period
        # sampling cannot identify sub-sample reset time independently of DC.
        # Report this phase uncertainty; it is not a total error/confidence bound.
        details["dc_sampling_uncertainty_microvolts"] = int(round(
            details["vpp_microvolts"] * result.frequency_millihz /
            (2 * meta.sample_rate_millihz)))
    return ((f"{NAMES[result.type]},{frequency:.9f},"
             f"{details['vpp_v']:.9f},{duty_pct:.7f},{offset:.9f}"), details)


def classify(dll, payload: bytes, meta: Metadata) -> str:
    return analyze(dll, payload, meta)[0]


def main() -> int:
    # Historical entry point uses the same server and mandatory archive path.
    sys.path.insert(0, str(ROOT.parent))
    from tools.waveform_server import main as server_main
    argv = sys.argv[1:]
    if "--loop" in argv:
        argv.remove("--loop")
    elif not any(arg.upper() in ("R", "RT", "RO") for arg in argv):
        argv.insert(0, "R")
    return server_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
