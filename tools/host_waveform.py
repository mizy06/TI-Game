"""Acquire raw ADC frames over UART and run the v2 cascade on the computer.

Output on stdout: type,frequency_hz,vpp_v,duty_pct,dc_offset_v.
The firmware sends DUMP protocol v2, including the measured acquisition metadata.
"""
from __future__ import annotations

import ctypes
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

SAMPLES = 4096
PAYLOAD_BYTES = SAMPLES * 2
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
        raise ValueError("unsupported DUMP header; flash host protocol v2 firmware")
    fields = {}
    for part in parts[1:]:
        key, sep, value = part.partition("=")
        if sep != "=" or key in fields or key not in keys:
            raise ValueError("malformed DUMP header")
        fields[key] = value
    if _uint(fields, "proto", 2, 2) != 2 or _uint(fields, "bytes", PAYLOAD_BYTES, PAYLOAD_BYTES) != PAYLOAD_BYTES:
        raise ValueError("unexpected DUMP protocol or byte count")
    return Metadata(_uint(fields, "seq", 0, 0xffffffff),
                    _uint(fields, "fs_millihz", 1, 100000000),
                    _uint(fields, "vref_uv", 1, 10000000),
                    _uint(fields, "gain_ppm", 1, 10000000),
                    _uint(fields, "adc_max", 4095, 4095),
                    _uint(fields, "flags", 0, 15),
                    _uint(fields, "fnv1a", 0, 0xffffffff))


def receive_frame(port, *, first_line=None, store=None):
    """Receive without discarding buffered frames; archive before validation."""
    record = None
    for _ in range(16):
        line = first_line if first_line is not None else read_line(port)
        first_line = None
        if line.startswith("ERR_"):
            raise RuntimeError(f"board capture failed: {line}")
        if line.startswith("DUMP,"):
            record = store.begin("uart", {"raw_header": line}) if store else None
            try:
                meta = parse_header(line)
                if record:
                    record.set_metadata(meta)
                payload = read_exact(port, PAYLOAD_BYTES,
                                     record.append if record else None)
                # Consume the trailer even on a checksum failure to allow resync.
                end, ready = read_line(port), read_line(port)
                if fnv1a(payload) != meta.checksum:
                    raise ValueError("DUMP FNV-1a integrity check failed")
                if end != "DUMP_END" or ready != "READY":
                    raise ValueError("missing DUMP_END / READY")
                if record:
                    record.info["calculated_fnv1a"] = fnv1a(payload)
                    record.complete(payload)
                return payload, meta, record
            except Exception as error:
                if record:
                    if record.info["payload_bytes"] == PAYLOAD_BYTES:
                        record.info["calculated_fnv1a"] = fnv1a(record.payload.read_bytes())
                    record.fail(error)
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


def analyze(dll, payload: bytes, meta: Metadata) -> tuple[str, dict]:
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError("incomplete ADC payload")
    values = (ctypes.c_uint16 * SAMPLES)(*struct.unpack(f"<{SAMPLES}H", payload))
    frame = Frame(values, SAMPLES, meta.adc_full_scale,
                  meta.sample_rate_millihz, meta.reference_microvolts,
                  meta.gain_ppm, meta.sequence, meta.flags)
    result = Result()
    ok = bool(dll.ti_waveform_analyze_frame(ctypes.byref(frame), ctypes.byref(result)))
    details = {name: int(getattr(result, name)) for name, _ in Result._fields_}
    details["ok"] = ok
    if not ok:
        return "unknown,0,0,0,0", details
    if result.algorithm_version != ABI_VERSION or result.sequence != meta.sequence or not 0 <= result.type < len(NAMES) - 1:
        raise RuntimeError("host algorithm returned incompatible result")
    from tools.envelope_measurement import refine
    refine(payload, meta, details)
    duty = details["duty_permille"] / 10 if details["type"] == 1 else 0
    # The DC estimate is measured at the ADC pin; undo the front-end gain.
    offset = details["dc_offset_counts"] * meta.reference_microvolts / (meta.adc_full_scale * meta.gain_ppm)
    if result.type == 3:
        # With an abrupt saw reset between ADC samples, exact integer-period
        # sampling cannot identify sub-sample reset time independently of DC.
        # Report this phase uncertainty; it is not a total error/confidence bound.
        details["dc_sampling_uncertainty_microvolts"] = int(round(
            details["vpp_microvolts"] * result.frequency_millihz /
            (2 * meta.sample_rate_millihz)))
    return ((f"{NAMES[result.type]},{result.frequency_millihz / 1000:.3f},"
             f"{details['vpp_millivolts'] / 1000:.3f},{duty:.1f},{offset:.3f}"), details)


def classify(dll, payload: bytes, meta: Metadata) -> str:
    return analyze(dll, payload, meta)[0]


def main() -> int:
    # Historical entry point uses the same server and mandatory archive path.
    sys.path.insert(0, str(ROOT.parent))
    from tools.waveform_server import main as server_main
    argv = sys.argv[1:]
    if "--loop" in argv:
        argv.remove("--loop")
    elif not any(arg.upper() in ("R", "RT") for arg in argv):
        argv.insert(0, "R")
    return server_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
