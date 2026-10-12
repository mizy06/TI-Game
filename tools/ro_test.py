"""RO: replay the unchanged RT samples on a Siglent, then acquire through R."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import socket
import struct
import time
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from tools import host_waveform as host
from tools.capture_store import atomic_json, atomic_text
from tools.self_test import clean_signal, distorted_shape, evaluate, generate, matrix


def close_record(case, adc):
    """Keep all RT samples; append the remainder of the carrier's last cycle."""
    import numpy as np
    cycles = math.ceil(len(adc) * case.frequency_hz / case.fs_hz)
    length = round(cycles * case.fs_hz / case.frequency_hz)
    extra = length - len(adc)
    if extra <= 0:
        return adc
    t = np.arange(len(adc)) / case.fs_hz
    raw = distorted_shape(case, (case.phase + t * case.frequency_hz) % 1, t)
    clean, _, _ = clean_signal(case)
    slope = float(np.ptp(clean) / np.ptp(raw))
    intercept = float(clean.min() - slope * raw.min())
    tail_t = np.arange(len(adc), length) / case.fs_hz
    tail = distorted_shape(case, (case.phase + tail_t * case.frequency_hz) % 1, tail_t)
    tail = slope * tail + intercept
    if case.inverted:
        clean, tail = -clean, -tail
    to_adc = case.gain * 4095 / case.vref_v
    baseline = (case.dc_v + .5 * case.vpp_v * clean) * to_adc
    tail_baseline = (case.dc_v + .5 * case.vpp_v * tail) * to_adc
    if np.any((tail_baseline < 1) | (tail_baseline > 4094)):
        raise ValueError(f"RO cannot close {case.id} within the ADC envelope")
    # Carry the original noise/INL residual into the short appended tail.
    # Only the appended residual is bounded; original RT codes remain exact.
    residual = adc[:extra] - baseline[:extra]
    residual = np.clip(residual, 1 - tail_baseline, 4094 - tail_baseline)
    return np.r_[adc, np.rint(tail_baseline + residual)]


def board_levels():
    """Read this project's calibration without changing firmware or RT."""
    text = (Path(__file__).resolve().parents[1] / "acquisition_config.h").read_text()
    values = []
    for name in ("ACQ_REFERENCE_MICROVOLTS", "ACQ_FRONTEND_GAIN_PPM"):
        match = re.search(r"^\s*#define\s+" + name + r"\s+\(?([0-9]+)[uUlL]*\)?\s*$", text, re.M)
        if not match or int(match[1]) <= 0:
            raise ValueError(f"RO cannot read {name} from acquisition_config.h")
        values.append(int(match[1]) / 1e6)
    return tuple(values)


def encode(case, vref, gain, level_mode="adc"):
    """Invert RT's ADC quantization, then encode signed little-endian DAC words."""
    import numpy as np
    if level_mode not in ("adc", "source"):
        raise ValueError("RO level mode must be adc or source")
    if not all(math.isfinite(x) and x > 0 for x in (vref, gain, case.fs_hz)):
        raise ValueError("RO calibration/sample rate must be positive and finite")
    payload, meta, facts = generate(case)  # Includes original noise, distortion and INL.
    adc = np.frombuffer(payload, dtype="<u2").astype(float)
    if len(adc) != host.SAMPLES or np.any(adc > meta.adc_full_scale):
        raise ValueError(f"RO cannot replay invalid ADC samples: {case.id}")
    adc = close_record(case, adc)
    full_scale = vref / gain if level_mode == "adc" else case.vref_v / case.gain
    volts = adc * full_scale / meta.adc_full_scale
    if float(volts.min()) < 0 or float(volts.max()) > vref / gain:
        raise ValueError(f"RO {case.id} exceeds the board input range; use --ro-level-mode adc")
    dac = np.rint((2 * adc / meta.adc_full_scale - 1) * 32767).astype("<i2")
    # Round-trip check prevents a sign, byte order or factor-of-two mistake.
    restored = .5 * full_scale * (dac.astype(float) / 32767 + 1)
    error = float(np.max(np.abs(restored - volts)))
    if error > full_scale / (4 * 32767) + 1e-12:
        raise ValueError("RO DAC round-trip check failed")
    scale = full_scale / (case.vref_v / case.gain)
    truth = dict(facts["truth"])
    truth.update(vpp_v=truth["vpp_v"] * scale, dc_v=truth["dc_v"] * scale)
    return payload, dac.tobytes(), {
        "rt_metadata": asdict(meta), "rt_generated": facts, "truth": truth,
        "level_mode": level_mode, "voltage_scale": scale,
        "amplitude_v": full_scale, "offset_v": .5 * full_scale,
        "sample_rate_hz": case.fs_hz, "loop_frequency_hz": case.fs_hz / len(adc),
        "waveform_samples": len(adc), "seam_bridge_samples": len(adc) - host.SAMPLES,
        "input_min_v": float(volts.min()), "input_max_v": float(volts.max()),
        "dac_roundtrip_error_v": error, "dac_encoding": "signed int16 little-endian",
        "note": "Original RT codes are unchanged. A short carrier continuation closes the loop; "
                "aperiodic noise/AM/interference still repeats, and R is not phase-locked."}


def parameters(response, command):
    """Accept the instrument's normal headers or COMM_HEADER OFF replies."""
    response = response.strip()
    response = re.sub(r"^" + re.escape(command) + r"\s+", "", response, flags=re.I)
    return [word.strip().upper() for word in response.split(",")]


def numeric(words, key):
    try:
        value = words[words.index(key) + 1]
        match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?)(?:V|HZ|SA/S)?", value)
        if not match:
            raise ValueError(value)
        result = float(match[1])
        if not math.isfinite(result):
            raise ValueError(value)
        return result
    except (ValueError, IndexError) as error:
        raise RuntimeError(f"RO invalid {key} readback: {','.join(words)}") from error


WAVEFORM_RESPONSE_LIMIT = 64 * 1024 * 1024 + 4096


def waveform_header(prefix):
    """Read the instrument's header, never infer its LENGTH from the upload."""
    marker = re.search(rb"\bWAVEDATA\s*,", prefix, re.I)
    if marker is None:
        return None
    header = prefix[:marker.start()].decode("ascii", errors="replace")
    length = re.search(r"(?:^|,|\s)\s*LENGTH\s*,\s*([0-9]+)\s*(KB|MB|B)?\s*(?:,|$)",
                       header, re.I)
    declared = None
    if length:
        declared = int(length[1]) * {None: 1, "B": 1, "KB": 1024, "MB": 1024**2}[length[2].upper() if length[2] else None]
    # Keep the returned file identity. A rejected USER argument can be echoed
    # verbatim in POS while WVNM is empty, followed by unrelated default data.
    def field(key):
        match = re.search(r"(?:^|,|\s)" + key + r"\s*,\s*([^,]*)", header, re.I)
        return match[1].strip().strip('"') if match else None
    return {"header": header, "header_bytes": marker.end(), "declared_bytes": declared,
            "position": field("POS"), "waveform_name": field("WVNM"),
            "waveform_type": field("TYPE")}


def waveform_name(value):
    """Compare a returned filename, not an echoed query containing commas."""
    value = (value or "").strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1]
    if "," in value:
        return None
    for suffix in (".bin", ".arb"):
        if value.lower().endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value


def waveform_payload(raw):
    """Separate LENGTH bytes from optional CR/LF; keep binary trailing LF intact."""
    info = waveform_header(raw[:4096])
    if info is None:
        raise RuntimeError("RO WVDT readback has no WAVEDATA header in its first 4096 bytes")
    begin, declared = info["header_bytes"], info["declared_bytes"]
    if declared is None or declared <= 0:
        raise RuntimeError(f"RO WVDT readback has no positive byte LENGTH; header={info['header']!r}")
    body = raw[begin:]
    info.update(response_bytes=len(raw), received_body_bytes=len(body), framing="raw")
    # A leading '#' can also be a real DAC byte. Prefer exact raw LENGTH framing.
    if len(body) >= declared and body[declared:] in (b"", b"\n", b"\r\n"):
        data = body[:declared]
    else:
        # Also accept an explicit IEEE definite-length block, without ever stripping
        # arbitrary binary bytes or treating their embedded LF as message ends.
        digits = int(chr(body[1])) if len(body) > 1 and body[:1] == b"#" and 49 <= body[1] <= 57 else 0
        field = body[2:2 + digits]
        size = int(field) if digits and len(field) == digits and field.isdigit() else None
        start = 2 + digits
        if size != declared or len(body) < start + declared or body[start + declared:] not in (b"", b"\n", b"\r\n"):
            raise RuntimeError(f"RO WVDT readback length/framing differs: LENGTH={declared}, "
                               f"received_body={len(body)}; header={info['header']!r}")
        data = body[start:start + declared]
        info["framing"] = "IEEE definite-length block"
    info.update(payload_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return data, info


def verify_waveform(raw, expected, expected_name=None):
    data, info = waveform_payload(raw)
    info.update(uploaded_bytes=len(expected), uploaded_sha256=hashlib.sha256(expected).hexdigest(),
                payload_prefix_hex=data[:32].hex(), uploaded_prefix_hex=expected[:32].hex(),
                verified=False)
    if expected_name is not None and not any(
            waveform_name(info[key]) == expected_name for key in ("waveform_name", "position")):
        error = RuntimeError(f"RO WVDT did not resolve uploaded waveform {expected_name!r}: "
                             f"POS={info['position']!r}, WVNM={info['waveform_name']!r}, "
                             f"LENGTH={info['declared_bytes']}; query syntax/name must be resolved "
                             "before comparing waveform bytes")
        error.readback_info, error.unresolved_waveform = info, True
        raise error
    # An integer number of byte-exact repeats gives the identical infinite
    # sample stream in TrueArb at the same sample rate. Padding/resampling does not.
    repetitions = len(data) // len(expected) if expected and len(data) % len(expected) == 0 else 0
    if not repetitions or any(data[i:i + len(expected)] != expected
                              for i in range(0, len(data), len(expected))):
        first = next((i for i, (a, b) in enumerate(zip(data, expected)) if a != b), None)
        error = RuntimeError(f"RO waveform readback differs: uploaded={len(expected)} bytes, "
                             f"LENGTH={info['declared_bytes']}, payload={len(data)} bytes, "
                             f"first_different_byte={first}; header={info['header']!r}")
        error.readback_info = info
        raise error
    info.update(bytes=len(expected), readback_bytes=len(data), repetitions=repetitions,
                uploaded_sha256=hashlib.sha256(expected).hexdigest(), verified=True)
    return info


def xdr_words(*values):
    return struct.pack(f">{len(values)}I", *values)


def xdr_bytes(data):
    return xdr_words(len(data)) + data + b"\0" * (-len(data) % 4)


class XdrReader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ConnectionError("RO truncated VXI-11/XDR reply")
        data = self.data[self.offset:end]
        self.offset = end
        return data

    def word(self):
        return struct.unpack(">I", self.take(4))[0]

    def opaque(self):
        size = self.word()
        value = self.take(size)
        self.take(-size % 4)
        return value


class RpcConnection:
    """ONC RPC v2 over TCP, including TCP record fragments (RFC 5531)."""
    def __init__(self, address, port, program, timeout):
        self.program, self.timeout = program, timeout
        self.xid = int.from_bytes(os.urandom(4), "big")
        self.healthy = True
        self.sock = socket.create_connection((address, port), timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def receive(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RPC reply deadline exceeded")
            self.sock.settimeout(remaining)
            part = self.sock.recv(size - len(data))
            if not part:
                raise ConnectionError("VXI-11 RPC connection closed")
            data.extend(part)
        return bytes(data)

    def call(self, procedure, arguments=b"", version=1):
        self.xid = (self.xid + 1) & 0xffffffff
        # CALL, RPC version 2, program/version/procedure, AUTH_NULL credentials/verifier.
        payload = xdr_words(self.xid, 0, 2, self.program, version, procedure, 0, 0, 0, 0) + arguments
        deadline = time.monotonic() + self.timeout
        try:
            self.sock.settimeout(self.timeout)
            self.sock.sendall(xdr_words(0x80000000 | len(payload)) + payload)
            result = bytearray()
            while True:
                marker = struct.unpack(">I", self.receive(4, deadline))[0]
                size = marker & 0x7fffffff
                if len(result) + size > 2 * 1024 * 1024:
                    raise ConnectionError("RO VXI-11 RPC reply exceeds 2 MiB")
                result.extend(self.receive(size, deadline))
                if marker & 0x80000000:
                    break
            reply = XdrReader(bytes(result))
            if reply.word() != self.xid or reply.word() != 1:
                raise ConnectionError("RO unexpected VXI-11 RPC transaction")
            if reply.word() != 0:
                raise ConnectionError("RO VXI-11 RPC request denied")
            reply.word()  # Verifier flavor.
            reply.opaque()
            status = reply.word()
            if status != 0:
                raise ConnectionError(f"RO VXI-11 RPC accept status {status}")
            return reply
        except (OSError, ValueError, struct.error) as error:
            self.healthy = False
            detail = f"VXI-11 RPC program=0x{self.program:x} procedure={procedure}: {error}"
            if isinstance(error, TimeoutError):
                raise TimeoutError(detail) from error
            raise ConnectionError(detail) from error

    def close(self):
        self.sock.close()


class Vxi11:
    """Small dependency-free VXI-11 core client for the instrument's inst0 link.

    Wire definitions: VXI-11 core program 0x0607af, create_link=10,
    device_write=11, device_read=12, destroy_link=23; END flag=8.
    No xdrlib dependency (removed in Python 3.13).
    """
    def __init__(self, address, port, timeout):
        self.address, self.port, self.timeout = address, port, timeout
        self.core, self.link, self.abort_port = None, None, 0
        self.io_ms = max(1, math.ceil(timeout * 1000))

    @staticmethod
    def check(reply, operation):
        error = reply.word()
        if error:
            if error == 15:
                raise TimeoutError(f"VXI-11 {operation}: instrument I/O timeout (15)")
            raise RuntimeError(f"VXI-11 {operation}: instrument error {error}")

    def open(self):
        mapper = RpcConnection(self.address, self.port, 100000, self.timeout)
        try:
            port = mapper.call(3, xdr_words(0x0607af, 1, 6, 0), version=2).word()
        finally:
            mapper.close()
        if not 1 <= port <= 65535:
            raise ConnectionError("RO VXI-11 core service is not registered on portmapper")
        self.core = RpcConnection(self.address, port, 0x0607af, self.timeout)
        try:
            client_id = int.from_bytes(os.urandom(4), "big") & 0x7fffffff
            reply = self.core.call(10, xdr_words(client_id, 0, 0) + xdr_bytes(b"inst0"))
            self.check(reply, "create_link(inst0)")
            self.link, self.abort_port, size = reply.word(), reply.word(), reply.word()
            if size <= 0:
                raise ConnectionError("RO invalid VXI-11 maxRecvSize")
            self.write_size = min(size, 1024 * 1024)
            return self
        except BaseException:
            self.close()
            raise

    def write(self, data):
        for offset in range(0, len(data), self.write_size):
            part = data[offset:offset + self.write_size]
            flags = 8 if offset + len(part) == len(data) else 0
            reply = self.core.call(11, xdr_words(self.link, self.io_ms, 0, flags) + xdr_bytes(part))
            self.check(reply, "device_write")
            size = reply.word()
            if size != len(part):
                raise ConnectionError(f"RO incomplete VXI-11 write: {size}/{len(part)} bytes")

    def read(self, limit=4096, text=False, on_chunk=None):
        result = bytearray()
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RO VXI-11 read deadline exceeded")
            capacity = limit - len(result)
            if capacity <= 0:
                raise RuntimeError(f"RO VXI-11 response exceeds {limit} bytes")
            # TermChar is enabled only for ASCII. Binary readback relies on RX_END.
            reply = self.core.call(12, xdr_words(self.link, min(capacity, 65536),
                max(1, math.ceil(remaining * 1000)), 0, 128 if text else 0, 10 if text else 0))
            self.check(reply, "device_read")
            reason, part = reply.word(), reply.opaque()
            if len(part) > capacity:
                raise ConnectionError("RO oversized VXI-11 device_read response")
            result.extend(part)
            if on_chunk is not None:
                on_chunk(part, len(result), reason)
            if reason & 4 or (text and reason & 2):
                return bytes(result)
            if not part:
                raise ConnectionError("RO VXI-11 read returned no data and no END")

    def close(self):
        if self.core is not None:
            try:
                if self.link is not None and self.core.healthy:
                    self.core.call(23, xdr_words(self.link))
            finally:
                self.core.close()
                self.core, self.link = None, None

    def discard(self):
        # A timed-out reply must never be consumed as a later query's answer.
        if self.core is not None:
            self.core.close()
            self.core = None


class Generator:
    """SDG6022X-E WVDT via VXI-11 message END, not line-framed binary TCP."""
    def __init__(self, address, port=111, channel=1, timeout=10.):
        if channel not in (1, 2) or not 1 <= port <= 65535:
            raise ValueError("RO invalid generator channel/port")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RO timeout must be positive and finite")
        self.address, self.port, self.timeout = address, port, timeout
        self.channel = f"C{channel}"
        self.name = f"RO_C{channel}"
        if port in (5024, 5025):
            raise ValueError("RO now uses VXI-11: remove --ro-port 5025/5024 or use --ro-port 111")
        self.transport = None
        self.controlled = False
        self.shutdown_error = ""
        self.stage, self.command = "connect", "VXI-11 inst0"
        self.failure = None
        self.trace = None
        self.shutting_down = False
        self.readback_info = None
        self.readback_command = None
        self.readback_supported = None
        self.readback_attempts = []
        self.stderr = None
        self.playback_calibration = None

    def record(self, event, **details):
        if self.trace is not None:
            self.trace.write(json.dumps({"time_ns": time.time_ns(), "stage": self.stage,
                "command": self.command, "event": event, **details}, ensure_ascii=False) + "\n")
            self.trace.flush()

    def operation(self, command, action):
        self.command = command
        self.record("start")
        start = time.perf_counter()
        try:
            result = action()
            details = {"duration_s": time.perf_counter() - start}
            if isinstance(result, bytes):
                details.update(bytes=len(result), sha256=hashlib.sha256(result).hexdigest())
            elif isinstance(result, str):
                details["response"] = result
            self.record("ok", **details)
            return result
        except Exception as error:
            detail = f"RO {self.stage}; command={command}; {error}"
            if self.failure is None and not self.shutting_down:
                self.failure = {"stage": self.stage, "command": command, "error": detail}
            self.record("error", error=detail, duration_s=time.perf_counter() - start)
            if isinstance(error, TimeoutError):
                raise TimeoutError(detail) from error
            raise RuntimeError(detail) from error

    def send(self, command):
        self.operation(command, lambda: self.transport.write(command.encode("ascii") + b"\n"))

    def query(self, command):
        def execute():
            self.transport.write(command.encode("ascii") + b"\n")
            return self.transport.read(text=True).decode("ascii").strip()
        return self.operation(command, execute)

    def query_binary(self, command, limit=WAVEFORM_RESPONSE_LIMIT, record_path=None):
        def execute():
            prefix = bytearray()
            self.readback_info = None
            with (Path(record_path).open("wb") if record_path else nullcontext()) as sink:
                def observe(part, received, reason):
                    if sink is not None:
                        sink.write(part)
                        sink.flush()
                    if len(prefix) < 4096:
                        prefix.extend(part[:4096 - len(prefix)])
                    if self.readback_info is None:
                        info = waveform_header(bytes(prefix))
                        if info is not None:
                            self.readback_info = info
                            self.record("waveform_header", **info)
                    if self.readback_info is not None:
                        self.readback_info.update(received_bytes=received, vxi_reason=reason)
                self.transport.write(command.encode("ascii") + b"\n")
                return self.transport.read(limit=limit, on_chunk=observe)
        return self.operation(command, execute)

    def ready(self):
        if self.query("*OPC?") != "1":
            raise RuntimeError("RO generator did not acknowledge operation completion")

    def read_waveform(self, data, readback_dir=None):
        """Use the vendor's Python spelling; resolve compatibility once per run.

        PG02-E05B section 5.1.4 uses lowercase ``user`` even though the command
        table prints USER. Some firmware treats unrecognized arguments as POS.
        Format 3 explicitly quotes the Local path (EN06A section 3.9.2).
        Only an unresolved file identity permits another query; a named file
        with different bytes is a real verification failure and stops the run.
        """
        if self.readback_supported is False:
            return self.verify_uploaded_file(data, readback_dir)
        commands = [self.readback_command] if self.readback_command else [
            f"WVDT? user,{self.name}",
            f'WVDT? user,"Local",{self.name}',
            f'WVDT? USER,"Local",{self.name}',
            f"WVDT? USER,{self.name}",
        ]
        attempts, last_error, raw = [], None, None
        path = readback_dir / "generator.readback.bin" if readback_dir else None
        try:
            for index, command in enumerate(commands):
                if index and path is not None and path.exists():
                    path.replace(readback_dir / f"generator.readback.attempt-{index:02d}.bin")
                raw = None
                raw = self.query_binary(command, record_path=path)
                try:
                    info = verify_waveform(raw, data, expected_name=self.name)
                except RuntimeError as error:
                    self.readback_info = getattr(error, "readback_info", self.readback_info)
                    attempts.append({"query": command, "error": str(error),
                                     "readback": self.readback_info})
                    self.record("waveform_query_rejected", query=command, error=str(error),
                                readback=self.readback_info)
                    # Never accept a different waveform just because its NAME is right.
                    if not getattr(error, "unresolved_waveform", False):
                        raise
                    last_error = error
                    if self.stderr is not None and index + 1 < len(commands):
                        print(f"RO: {command} did not resolve {self.name}; trying explicit query",
                              file=self.stderr, flush=True)
                    continue
                info.update(query=command, query_attempts=attempts)
                self.readback_command, self.readback_info = command, info
                self.readback_supported = True
                self.record("waveform_verified", **info)
                return info
            # SDG6022X-E 6.01.01.37R6 parses USER queries as built-in indices:
            # it echoes the whole argument in POS and returns Sine with empty WVNM.
            # Do not call those unrelated bytes verified. On this firmware use
            # acknowledged VXI writes, OPC, the user-file list and the separately
            # checked ARWV/SRATE/BSWV/OUTP settings. Named byte mismatches still fail.
            if (self.identity.rsplit(",", 1)[-1].strip() == "6.01.01.37R6" and
                    attempts and all(a["readback"] and
                        a["readback"]["position"] == "USER" and
                        not a["readback"]["waveform_name"] for a in attempts)):
                self.readback_supported = False
                self.readback_attempts = attempts
                if self.stderr is not None:
                    print("RO: this firmware does not resolve USER waveform byte queries; "
                          "verifying acknowledged upload, user file and playback settings "
                          "(byte readback is unavailable)", file=self.stderr, flush=True)
                return self.verify_uploaded_file(data, readback_dir)
            raise last_error
        except BaseException as error:
            self.readback_info = getattr(error, "readback_info", self.readback_info)
            if self.readback_info is not None:
                self.readback_info = dict(self.readback_info, query=self.command,
                                          query_attempts=attempts)
            self.record("waveform_verification_error", error=f"{type(error).__name__}: {error}",
                        readback=self.readback_info)
            raise
        finally:
            if readback_dir is not None:
                atomic_json(readback_dir / "generator.readback.json", {
                    "complete": raw is not None, "uploaded_bytes": len(data),
                    "response_limit_bytes": WAVEFORM_RESPONSE_LIMIT, "readback": self.readback_info})

    def verify_uploaded_file(self, data, readback_dir=None):
        """Firmware compatibility path; explicitly distinguish it from byte equality."""
        words = parameters(self.query("STL? USER"), "STL")
        if not words or words[0] != "WVNM" or not any(
                waveform_name(name) == self.name for name in words[1:]):
            raise RuntimeError(f"RO uploaded waveform {self.name!r} is missing from user files")
        info = {"query": "STL? USER", "waveform_name": self.name, "user_files": words[1:],
                "uploaded_bytes": len(data), "uploaded_sha256": hashlib.sha256(data).hexdigest(),
                "verified": False, "byte_readback_supported": False,
                "configuration_verified": True,
                "verification": "VXI-11 write acknowledgement, OPC, user file and playback settings",
                "query_attempts": self.readback_attempts}
        self.readback_info = info
        self.record("waveform_upload_confirmed", **info)
        if readback_dir is not None:
            atomic_json(readback_dir / "generator.readback.json", {
                "complete": True, "uploaded_bytes": len(data), "readback": info})
        return info

    def __enter__(self):
        try:
            self.transport = Vxi11(self.address, self.port, self.timeout)
            self.operation(f"{self.address}:{self.port} portmapper / inst0", self.transport.open)
            self.stage = "identify instrument"
            self.identity = self.query("*IDN?")
            if not re.search(r"\bSDG6022X(?:-E)?\b", self.identity, re.I):
                raise RuntimeError(f"RO unexpected generator: {self.identity}")
            self.controlled = True
            self.stage = "initialize output"
            for command in ("OUTP OFF", "OUTP LOAD,HZ,PLRT,NOR", "MDWV STATE,OFF",
                            "SWWV STATE,OFF", "BTWV STATE,OFF", "NOISE_ADD STATE,OFF"):
                self.send(f"{self.channel}:{command}")
            self.ready()
            return self
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            raise

    def configure(self, data, config, readback_dir=None):
        c = self.channel
        self.readback_info = None
        self.stage = "upload waveform"
        self.send(f"{c}:OUTP OFF")
        self.send(f"{c}:BSWV WVTP,ARB")
        # Select point-by-point playback before loading and querying the wave.
        self.send(f"{c}:SRATE MODE,TARB,VALUE,{config['sample_rate_hz']:.12g},INTER,HOLD")
        # Vendor WVDT/write_raw example: ASCII header + signed int16 LE data.
        # No extra LF, TYPE or IEEE # block is part of this binary payload.
        header = (f"{c}:WVDT WVNM,{self.name},"
                  f"FREQ,{config['loop_frequency_hz']:.12g},"
                  f"AMPL,{config['amplitude_v']:.12g},OFST,{config['offset_v']:.12g},"
                  "PHASE,0,WAVEDATA,")
        self.record("binary_upload", bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.operation(header + f"<{len(data)} binary bytes>",
                       lambda: self.transport.write(header.encode("ascii") + data))
        self.ready()
        self.stage = "configure TrueArb"
        self.send(f"{c}:ARWV NAME,{self.name}")
        self.send(f"{c}:SRATE MODE,TARB,VALUE,{config['sample_rate_hz']:.12g},INTER,HOLD")
        self.send(f"{c}:BSWV AMP,{config['amplitude_v']:.12g},OFST,{config['offset_v']:.12g}")
        self.ready()
        arb = parameters(self.query(f"{c}:ARWV?"), f"{c}:ARWV")
        rate = parameters(self.query(f"{c}:SRATE?"), f"{c}:SRATE")
        wave = parameters(self.query(f"{c}:BSWV?"), f"{c}:BSWV")
        if "NAME" not in arb or waveform_name(arb[arb.index("NAME") + 1]) != self.name:
            raise RuntimeError("RO generator did not select the uploaded waveform")
        if "MODE" not in rate or rate[rate.index("MODE") + 1] != "TARB":
            raise RuntimeError("RO generator did not enter TrueArb mode")
        if "INTER" not in rate or rate[rate.index("INTER") + 1] != "HOLD":
            raise RuntimeError("RO generator did not select zero-order hold")
        if "WVTP" not in wave or wave[wave.index("WVTP") + 1] != "ARB":
            raise RuntimeError("RO generator did not select ARB output")
        for words, key, expected in ((rate, "VALUE", config["sample_rate_hz"]),
                (wave, "AMP", config["amplitude_v"]), (wave, "OFST", config["offset_v"])):
            if not math.isclose(numeric(words, key), expected, rel_tol=1e-5, abs_tol=1e-6):
                raise RuntimeError(f"RO generator {key} differs from requested value")
        output = parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")
        if "LOAD" not in output or output[output.index("LOAD") + 1] != "HZ":
            raise RuntimeError("RO generator load is not Hi-Z")
        if "PLRT" not in output or output[output.index("PLRT") + 1] != "NOR":
            raise RuntimeError("RO generator polarity is not normal")
        self.stage = "verify waveform bytes"
        readback_dir = Path(readback_dir) if readback_dir is not None else None
        waveform = self.read_waveform(data, readback_dir)
        self.stage = "enable output"
        self.send(f"{c}:OUTP ON")
        self.ready()
        if parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")[0] != "ON":
            raise RuntimeError("RO generator output is not enabled")
        return {"arb": arb, "rate": rate, "wave": wave, "output": output,
                "waveform": waveform, "transport": "VXI-11"}

    def reference(self, wave, offset_v, amplitude_v=0., frequency_hz=0.):
        """Configure independent built-in references, never an RT matrix case."""
        if wave not in ("dc", "sine"):
            raise ValueError("unsupported calibration reference")
        c, kind = self.channel, "DC" if wave == "dc" else "SINE"
        self.stage = "configure calibration reference"
        self.send(f"{c}:OUTP OFF")
        self.send(f"{c}:SRATE MODE,DDS")
        command = f"{c}:BSWV WVTP,{kind},OFST,{offset_v:.12g}"
        if wave == "sine":
            command += f",FRQ,{frequency_hz:.12g},AMP,{amplitude_v:.12g}"
        self.send(command)
        self.ready()
        words = parameters(self.query(f"{c}:BSWV?"), f"{c}:BSWV")
        if "WVTP" not in words or words[words.index("WVTP") + 1] != kind:
            raise RuntimeError("RO generator did not select the calibration reference")
        expected = [("OFST", offset_v)]
        if wave == "sine":
            expected += [("FRQ", frequency_hz), ("AMP", amplitude_v)]
        for key, value in expected:
            if not math.isclose(numeric(words, key), value, rel_tol=1e-5, abs_tol=1e-6):
                raise RuntimeError(f"RO calibration reference {key} readback differs")
        self.send(f"{c}:OUTP ON")
        self.ready()
        if parameters(self.query(f"{c}:OUTP?"), f"{c}:OUTP")[0] != "ON":
            raise RuntimeError("RO calibration reference output is not enabled")
        return {"wave": wave, "offset_v": numeric(words, "OFST"),
                "amplitude_v": numeric(words, "AMP") if wave == "sine" else 0.,
                "frequency_hz": numeric(words, "FRQ") if wave == "sine" else 0.}

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None and self.failure is None:
            self.failure = {"stage": self.stage, "command": self.command,
                            "error": f"{type(exc).__name__}: {exc}"}
        if exc is not None:
            self.record("failure", failure=self.failure)
        self.shutting_down = True
        self.stage = "shutdown output"
        if self.transport is not None:
            try:
                if self.controlled:
                    if exc is not None:
                        # Use a fresh RPC stream after failure. Never reuse pending replies.
                        old = self.transport
                        old_link = old.link
                        old.discard()
                        self.transport = Vxi11(self.address, self.port, self.timeout)
                        self.operation("reconnect VXI-11 for OUTP OFF", self.transport.open)
                    self.send(f"{self.channel}:OUTP OFF")
                    self.ready()
                    if parameters(self.query(f"{self.channel}:OUTP?"), f"{self.channel}:OUTP")[0] != "OFF":
                        raise RuntimeError("RO generator did not confirm output OFF")
                    if exc is not None and old_link is not None:
                        # Release the old logical link on the healthy core connection.
                        self.transport.core.call(23, xdr_words(old_link))
            except Exception as error:
                self.shutdown_error = f"{type(error).__name__}: {error}"
                if exc is None:
                    raise
            finally:
                try:
                    self.transport.close()
                except Exception as error:
                    self.record("close_error", error=f"{type(error).__name__}: {error}")
                self.transport = None
        self.shutting_down = False


def calibrate_link(service, store, args, stderr, instrument, report, vref, gain):
    """Verify the analogue connection and calibrate before any matrix verdict."""
    from tools.acquisition_calibration import fit_calibration
    observations = []
    info = {"complete": False, "captures": [], "calibration": None,
            "error": "", "references_independent_of_matrix": True}
    path = report / "calibration.json"
    atomic_json(path, info)
    try:
        print("RO: checking board metadata and calibrating the ADC with independent references",
              file=stderr, flush=True)
        instrument.stage, instrument.command = "calibration metadata acquisition", "UART R (output OFF)"
        _, meta, record = service.capture(args.ro_timeout)
        record.info["context"]["ro_calibration"] = {"role": "metadata only; output OFF"}
        record.save()
        info["metadata_capture"] = record.path.relative_to(store.path).as_posix()
        if (not math.isclose(meta.reference_microvolts / 1e6, vref, abs_tol=1e-6) or
                not math.isclose(meta.gain_ppm / 1e6, gain, abs_tol=1e-6)):
            raise ValueError("RO board voltage/gain metadata differs from acquisition_config.h; "
                             "use matching board firmware before driving the matrix")
        full_scale = vref / gain
        references = [("dc", .15, 0., False), ("dc", .5, 0., False),
                      ("dc", .85, 0., False), ("sine", .5, 100., False),
                      ("sine", .5, 400., False), ("dc", .3, 0., True),
                      ("dc", .7, 0., True), ("sine", .5, 250., True)]
        for index, (wave, level, frequency, validation) in enumerate(references, 1):
            ref = instrument.reference(wave, level * full_scale, .3 * full_scale, frequency)
            print(f"RO calibration {index}/{len(references)}: {wave} {frequency:g} Hz, "
                  f"offset {ref['offset_v']:.6g} V", file=stderr, flush=True)
            time.sleep(max(.15, args.ro_settle))
            instrument.stage, instrument.command = "calibration reference acquisition", "UART R reference"
            payload, meta, record = service.capture(args.ro_timeout)
            context = {"reference": ref, "validation": validation,
                       "capture": record.path.relative_to(store.path).as_posix(), "metadata": asdict(meta)}
            record.info["context"]["ro_calibration"] = context
            record.save()
            observations.append({"payload": payload, "meta": meta, "reference": ref,
                                 "validation": validation})
            info["captures"].append(context)
            atomic_json(path, info)
        calibration, verification = fit_calibration(observations)
        info.update(complete=True, calibration=calibration.to_dict(), verification=verification,
                    algorithm_parameters_file=str(service.parameters_path))
        atomic_json(path, info)
        service.publish_calibration(path)
        print(f"RO calibration OK: sample rate {calibration.sample_rate_millihz / 1000:.6g} Hz, "
              f"ADC scale {calibration.adc_scale:.8g}, offset {calibration.adc_offset_counts:.6g} counts",
              file=stderr, flush=True)
        print(f"RO calibration saved for R: {service.parameters_path}", file=stderr, flush=True)
        return info
    except BaseException as error:
        info["complete"] = False
        info["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        atomic_json(path, info)


def calibrate_playback(service, store, args, stderr, instrument, report, vref, gain):
    """Validate ARB separately from the ADC calibration used in competition R."""
    from dataclasses import asdict
    from tools.acquisition_calibration import counts
    from tools.generator_calibration import reference, fit_playback, verify_playback
    full_scale = vref / gain
    path = report / "generator_calibration.json"
    info = {"complete": False, "captures": [], "calibration": None, "error": "",
            "references_independent_of_matrix": True,
            "note": "ARB command transfer only; never applied to competition R parameters."}
    atomic_json(path, info)
    observations = []

    def capture_ref(wave, level, offset, validation, profile=None):
        data, config, expected = reference(full_scale, level, wave=wave, offset_v=offset)
        requested = dict(config)
        if profile is not None:
            config = profile.apply(config)
        directory = report / "generator-references" / f"{len(info['captures']) + 1:02d}"
        directory.mkdir(parents=True)
        print(f"RO ARB calibration {len(info['captures']) + 1}/8: "
              f"{'check' if validation else 'fit'} {wave}, expected DC {expected['dc_v']:.6g} V, "
              f"AMP {config['amplitude_v']:.6g} V, OFST {config['offset_v']:.6g} V",
              file=stderr, flush=True)
        (directory / "generator.i16.bin").write_bytes(data)
        config["readback"] = instrument.configure(data, config, readback_dir=directory)
        atomic_json(directory / "configuration.json", {"requested": requested, "sent": config,
                                                       "reference": expected, "validation": validation})
        time.sleep(max(.15, args.ro_settle))
        instrument.stage, instrument.command = "ARB calibration acquisition", "UART R reference"
        payload, meta, record = service.capture(args.ro_timeout)
        context = {"reference": expected, "configuration": config, "validation": validation,
                   "capture": record.path.relative_to(store.path).as_posix()}
        acquisition = service.calibration
        adjusted, effective = acquisition.apply(payload, meta)
        samples = counts(payload)
        # Fit the small OFST control slope from the unrounded calibrated mean.
        # Re-quantizing every sample first can bias a short voltage difference.
        measured_dc = ((float(samples.mean()) - acquisition.adc_offset_counts) / acquisition.adc_scale *
                       full_scale / effective.adc_full_scale)
        context["measured_dc_v"] = measured_dc
        context["adc_std_counts"] = float(samples.std()) / acquisition.adc_scale
        context["metadata"] = asdict(meta)
        record.info["context"]["generator_calibration"] = context
        record.save()
        info["captures"].append(context)
        atomic_json(path, info)
        return {"payload": adjusted, "meta": effective, "config": requested, "reference": expected,
                "measured_dc_v": measured_dc}

    try:
        print("RO: checking independent ARB voltage references", file=stderr, flush=True)
        # The SDG's OFST response changes over large excursions. Calibrate the
        # local control response around the 50%-scale offset used by the matrix,
        # rather than combining distant 30%/70% ranges into one global affine fit.
        references = [(.15, .5), (.5, .5), (.85, .5), (.5, .48), (.5, .52)]
        for level, offset in references:
            observations.append(capture_ref("dc", level, offset * full_scale, False))
        profile, verification = fit_playback(observations, full_scale)
        checks = []
        for wave, level in (("dc", .3), ("dc", .7), ("sine", .5)):
            check = capture_ref(wave, level, .5 * full_scale, True, profile)
            checks.append(verify_playback(check["payload"], check["meta"], check["reference"]))
        instrument.playback_calibration = profile
        info.update(complete=True, calibration=asdict(profile),
                    verification={**verification, "independent_checks": checks})
        print(f"RO ARB calibration OK: amplitude gain {profile.amplitude_gain:.8g}, "
              f"offset gain {profile.offset_gain:.8g}, offset bias {profile.offset_bias_v:.6g} V",
              file=stderr, flush=True)
    except BaseException as error:
        info["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        atomic_json(path, info)


def run(service, store, args, stderr):
    """Replay the matrix on the generator; measure through R when a board is attached.

    ``service`` may be None.  The board link is only needed for the R leg, so
    without it RO still generates, encodes, uploads and readback-verifies every
    selected case and archives the full encoding, then reports mode=encode-only.
    """
    if not math.isfinite(args.ro_settle) or not 0 <= args.ro_settle <= 10:
        raise ValueError("RO settling time must be finite and between 0 and 10 seconds")
    measured = service is not None
    generator = Generator(args.ro_host, args.ro_port, args.ro_channel, args.ro_timeout)
    generator.stderr = stderr
    selected = [c for c in matrix(args.seed, args.random_count)
                if (args.suite == "all" or c.suite == args.suite) and
                (not args.case_id or c.id in args.case_id)]
    if args.case_id and set(args.case_id) - {c.id for c in selected}:
        raise ValueError("unknown case ID or case outside selected suite")
    if args.max_cases is not None:
        selected = selected[:args.max_cases]
    if not selected:
        raise ValueError("empty RO selection")
    vref, gain = board_levels()
    prepared = [encode(case, vref, gain, args.ro_level_mode) for case in selected]
    report = store.path / ("ro-" + str(time.time_ns()))
    report.mkdir()
    atomic_json(report / "matrix.json", {"schema": 1, "seed": args.seed,
        "cases": [asdict(c) for c in selected]})
    results, counts = [], Counter()
    started, completed = time.perf_counter(), False
    error_text, failed_case = "", None
    calibration_snapshot = playback_snapshot = None
    previous_calibration_mode = service.use_saved_calibration if measured else None
    if not measured:
        print("RO: no board attached; encoding and uploading the matrix, R is not sent",
              file=stderr, flush=True)
    try:
        with (report / "scpi.jsonl").open("w", encoding="utf-8", buffering=1) as trace:
            generator.trace = trace
            print(f"RO: VXI-11 {args.ro_host}:{args.ro_port} / inst0", file=stderr, flush=True)
            with generator as instrument, (report / "results.jsonl").open("w", encoding="utf-8") as journal:
                if measured:
                    if getattr(args, "ro_calibrate", "auto") == "auto":
                        service.use_saved_calibration = True
                        calibration_snapshot = calibrate_link(service, store, args, stderr, instrument, report, vref, gain)
                        playback_snapshot = calibrate_playback(service, store, args, stderr, instrument, report, vref, gain)
                    else:
                        service.use_saved_calibration = False
                _run_cases(service, store, args, stderr, instrument, journal, report,
                           selected, prepared, results, counts, vref, gain, measured)
            completed = True
    except BaseException as error:
        completed = False
        error_text = f"{type(error).__name__}: {error}"
        failed_case = selected[len(results)].id if len(results) < len(selected) else None
        raise
    finally:
        if measured:
            service.use_saved_calibration = previous_calibration_mode
        generator.trace = None
        # Close the report with the verified snapshots, just like the result
        # bundle, rather than leaving a streamed intermediate capture state.
        if isinstance(calibration_snapshot, dict):
            atomic_json(report / "calibration.json", calibration_snapshot)
        if isinstance(playback_snapshot, dict):
            atomic_json(report / "generator_calibration.json", playback_snapshot)
        columns = ["case_id", "wave", "verdict", "output", "frequency_error_pct",
                   "vpp_error_pct", "duty_error_pp", "dc_error_v", "capture"]
        table = io.StringIO(newline="")
        writer = csv.DictWriter(table, fieldnames=columns)
        writer.writeheader()
        for entry in results:
            writer.writerow({"case_id": entry["case_id"], "wave": entry["case"]["wave"],
                "capture": entry["capture"], **{k: entry["result"][k] for k in columns
                                                 if k in entry["result"]}})
        atomic_text(report / "results.csv", table.getvalue())
        atomic_json(report / "results.json", {"complete": completed, "results": results})
        summary = {"selected": len(selected), "complete": completed, "counts": dict(counts),
            "board": measured, "mode": "full" if measured else "encode-only",
            "all_passed": completed and not counts["failed"], "error": error_text,
            "shutdown_error": generator.shutdown_error,
            "failure": generator.failure, "failed_case": failed_case,
            "waveform_readback": generator.readback_info,
            "calibration": "calibration.json" if (report / "calibration.json").exists() else None,
            "generator_calibration": ("generator_calibration.json"
                if (report / "generator_calibration.json").exists() else None),
            "transport": "VXI-11", "diagnostics": "scpi.jsonl",
            "not_run": [c.id for c in selected[len(results):]],
            "wall_time_s": time.perf_counter() - started,
            "note": ("Real instrument/UART test. Original RT matrix is retained; adc mode scales "
                     "source volts to this board. Evaluation uses board metadata and, when enabled, "
                     "independent acquisition calibration; replay truth remains unchanged."
                     if measured else
                     "No board attached: every selected case was generated, encoded, uploaded and "
                     "readback-verified on the generator; R was not sent and nothing was measured.")}
        atomic_json(report / "summary.json", summary)
        print(f"RO: {dict(counts)}; summary: {report / 'summary.json'}", file=stderr, flush=True)
    return 0 if summary["all_passed"] else 1


def _run_cases(service, store, args, stderr, instrument, journal, report,
               selected, prepared, results, counts, vref, gain, measured):
    """Keep the RT/evaluation and optional UART behavior separate from transport."""
    atomic_json(report / "instrument.json", {"identity": instrument.identity,
        "host": args.ro_host, "port": args.ro_port, "channel": args.ro_channel,
        "transport": "VXI-11", "resource": f"TCPIP::{args.ro_host}::inst0::INSTR",
        "vref_v": vref, "gain": gain, "level_mode": args.ro_level_mode,
        "board": measured, "mode": "full" if measured else "encode-only",
        "acquisition_calibration": (service.calibration.to_dict()
                                    if measured and service.calibration is not None else None),
        "algorithm_parameters_file": str(service.parameters_path) if measured else None,
        "acquisition_calibration_source": (service.calibration_profile.source
            if measured and service.calibration is not None else None),
        "generator_calibration": (asdict(instrument.playback_calibration)
            if instrument.playback_calibration is not None else None)})
    for index, (case, (adc, dac, config)) in enumerate(zip(selected, prepared), 1):
        if instrument.playback_calibration is not None:
            config = instrument.playback_calibration.apply(config)
        source = report / case.id
        source.mkdir()
        (source / "rt.adc.bin").write_bytes(adc)
        (source / "generator.i16.bin").write_bytes(dac)
        atomic_json(source / "encoding.json", config)
        instrument.stage, instrument.command = "prepare waveform", case.id
        instrument.record("case_begin", case_id=case.id)
        print(f"RO {index}/{len(selected)} {case.id}: uploading and verifying waveform",
              file=stderr, flush=True)
        config["readback"] = instrument.configure(dac, config, readback_dir=source)
        atomic_json(source / "encoding.json", config)
        if not measured:
            entry = {"case_id": case.id, "case": asdict(case), "generator": config,
                "metadata": None, "algorithm": None, "capture": None,
                "result": {"verdict": "ENCODED", "passed": True,
                           "note": "generator encoding verified by readback; no board, R not sent"}}
            journal.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
            results.append(entry)
            counts["encoded"] += 1
            print(f"RO {index}/{len(selected)} {case.id}: ENCODED", file=stderr, flush=True)
            continue
        if index == 1:
            print("开始测试", file=stderr, flush=True)
        instrument.stage, instrument.command = "R acquisition", "UART R"
        instrument.record("start", case_id=case.id)
        time.sleep(args.ro_settle)
        line, details, elapsed, meta, record = service.measure(
            timeout=args.ro_timeout, return_result=True)
        instrument.record("ok", case_id=case.id)
        instrument.stage, instrument.command = "evaluate R capture", case.id
        # Real R metadata governs measurement units; source truth stays fixed.
        measured_case = replace(case, fs_hz=meta.sample_rate_millihz / 1000,
            vref_v=meta.reference_microvolts / 1e6, gain=meta.gain_ppm / 1e6,
            vpp_v=config["truth"]["vpp_v"], dc_v=config["truth"]["dc_v"])
        check = evaluate(measured_case, line, details, elapsed, config)
        record.info["context"].update(ro_case=asdict(case), generator=config,
            ro_report=report.relative_to(store.path).as_posix())
        record.info["self_test"] = check
        record.save()
        entry = {"case_id": case.id, "case": asdict(case), "generator": config,
            "metadata": asdict(meta), "wire_metadata": record.info["metadata"],
            "result": check, "algorithm": details,
            "capture": record.path.relative_to(store.path).as_posix()}
        journal.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
        journal.flush()
        os.fsync(journal.fileno())
        results.append(entry)
        counts["completed"] += 1
        counts["passed" if check["passed"] else "failed"] += 1
        failure = "; ".join(check["failure_reasons"])
        if details["high_clip_samples"] or details["low_clip_samples"]:
            failure += f"; ADC rails high={details['high_clip_samples']}, low={details['low_clip_samples']}"
        print(f"RO {index}/{len(selected)} {case.id}: {check['verdict']}" +
              (f" ({failure})" if not check["passed"] else ""), file=stderr, flush=True)
