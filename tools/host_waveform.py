"""Dump one real ADC frame and classify it on the host with the v2 cascade DLL.

The board owns ADC+DMA.  The host owns classification and prints the same
five-column CSV used by the firmware: type,freq_hz,vpp_v,duty_pct,dc_offset_v.
The D protocol is: DUMP header, 8192 LE bytes, DUMP_END trailer.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

SAMPLES = 4096
PAYLOAD_BYTES = SAMPLES * 2
ROOT = Path(__file__).resolve().parent.parent


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


NAMES = ("sine", "square", "triangle", "sawtooth", "unknown")


def fnv1a(data: bytes) -> int:
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xffffffff
    return value


def read_line(port) -> str:
    line = port.readline()
    if not line:
        raise TimeoutError("serial timeout")
    return line.decode("ascii").rstrip("\r\n")


def read_exact(port, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = port.read(count - len(chunks))
        if not chunk:
            raise TimeoutError(f"serial timeout after {len(chunks)}/{count} bytes")
        chunks.extend(chunk)
    return bytes(chunks)


def dump(port) -> bytes:
    port.reset_input_buffer()
    port.write(b"D")
    port.flush()
    while True:
        line = read_line(port)
        if line == "CAPTURING":
            continue
        if line.startswith("DUMP,"):
            fields = dict(part.split("=", 1) for part in line.split(",")[1:])
            if int(fields["bytes"]) != PAYLOAD_BYTES:
                raise ValueError("unexpected DUMP byte count")
            expected = int(fields["fnv1a"])
            payload = read_exact(port, PAYLOAD_BYTES)
            if fnv1a(payload) != expected:
                raise ValueError("DUMP FNV-1a integrity check failed")
            if read_line(port) != "DUMP_END":
                raise ValueError("missing DUMP_END")
            return payload


def classify(dll, payload: bytes) -> str:
    values = (ctypes.c_uint16 * SAMPLES).from_buffer_copy(payload)
    frame = Frame(values, SAMPLES, 4095, 20000000, 3300000, 1000000, 1, 1)
    result = Result()
    ok = bool(dll.waveform_analyze_frame(ctypes.byref(frame), ctypes.byref(result)))
    if not ok:
        return "unknown,0,0,0,0"
    duty = result.duty_permille / 10 if result.type == 1 else 0
    offset = result.dc_offset_counts * 3.3 / 4095
    return (f"{NAMES[result.type]},{result.frequency_millihz / 1000:.3f},"
            f"{result.vpp_millivolts / 1000:.3f},{duty:.1f},{offset:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--dll", default=str(ROOT / "tools" / "waveform_analyzer_host.dll"))
    args = parser.parse_args()
    try:
        import serial
        dll = ctypes.CDLL(args.dll)
        dll.ti_waveform_analyze_frame.argtypes = [ctypes.POINTER(Frame), ctypes.POINTER(Result)]
        dll.ti_waveform_analyze_frame.restype = ctypes.c_bool
        with serial.Serial(args.port, 115200, timeout=2, write_timeout=2) as port:
            payload = dump(port)
        print(classify(dll, payload), flush=True)
        return 0
    except Exception as error:
        print(f"host_waveform: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
