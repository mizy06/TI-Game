"""RO encoding, VXI-11 SCPI, original R path, ordering and failure recovery."""
import hashlib
import io
import json
import re
import socket
import struct
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import host_waveform as host
from tools.capture_store import CaptureStore
from tools.ro_test import Generator, board_levels, encode, generate_ro, ro_matrix, run, select_ro_cases
from tools.self_test import Case, matrix
from tools.waveform_server import Engine, SerialService, main, measurement_output, parser
from test_host_integration import response, sine_payload
from test_server import LiveSerial


class ScpiServer:
    """RPC instrument with fragmented replies and binary uploads framed by END."""
    def __init__(self, channel=1, mismatch=False, *, byte_readback=True,
                 corrupt_bytes=False, missing_user_file=False, firmware="6.01.01.37R6"):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.channel, self.mismatch = f"C{channel}", mismatch
        self.byte_readback, self.corrupt_bytes = byte_readback, corrupt_bytes
        self.missing_user_file, self.firmware = missing_user_file, firmware
        self.commands, self.uploads, self.events = [], [], []
        self.rate, self.amp, self.offset = 20000., 3.3, 1.65
        self.wave, self.frequency, self.rate_mode = "ARB", 5., "TARB"
        self.name, self.output = f"RO_C{channel}", "OFF"
        self.combine = "ON"
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @staticmethod
    def exact(connection, size):
        result = bytearray()
        while len(result) < size:
            part = connection.recv(size - len(result))
            if not part:
                raise EOFError
            result.extend(part)
        return bytes(result)

    @staticmethod
    def words(*values):
        return struct.pack(f">{len(values)}I", *values)

    @classmethod
    def opaque(cls, data):
        return cls.words(len(data)) + data + b"\0" * (-len(data) % 4)

    def serve(self):
        try:
            while not self.stop.is_set():
                try:
                    connection = self.listener.accept()[0]
                except socket.timeout:
                    continue
                with connection:
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    try:
                        self.serve_rpc(connection)
                    except EOFError:
                        pass
        except BaseException as error:
            self.error = error
        finally:
            self.listener.close()

    def serve_rpc(self, connection):
        write_buffer, read_buffer = bytearray(), bytearray()
        while True:
            record = bytearray()
            while True:
                marker = struct.unpack(">I", self.exact(connection, 4))[0]
                record.extend(self.exact(connection, marker & 0x7fffffff))
                if marker & 0x80000000:
                    break
            xid, kind, rpc_version, program, version, procedure, *auth = struct.unpack_from(">10I", record)
            assert (kind, rpc_version, auth) == (0, 2, [0, 0, 0, 0])
            args = record[40:]
            if program == 100000:
                assert (version, procedure) == (2, 3)
                body = self.words(self.port)
            elif procedure == 10:
                body = self.words(0, 1, 0, 1024)
            elif procedure == 11:
                link, io_ms, lock_ms, flags, size = struct.unpack_from(">5I", args)
                part = args[20:20 + size]
                write_buffer.extend(part)
                if flags & 8:
                    wire = bytes(write_buffer)
                    write_buffer.clear()
                    if wire.startswith((b"C1:WVDT ", b"C2:WVDT ")):
                        end = wire.index(b"WAVEDATA,") + len(b"WAVEDATA,")
                        self.commands.append(wire[:end].decode("ascii"))
                        self.uploads.append(wire[end:])
                    else:
                        command = wire.decode("ascii").strip()
                        self.commands.append(command)
                        reply = self.dispatch(command)
                        if reply is not None:
                            read_buffer.extend(reply if isinstance(reply, bytes) else
                                               (reply + "\r\n").encode("ascii"))
                body = self.words(0, len(part))
            elif procedure == 12:
                link, size, io_ms, lock_ms, flags, term_char = struct.unpack_from(">6I", args)
                size = min(size, 1021)
                reason = 1
                if flags & 128 and term_char in read_buffer[:size]:
                    size = read_buffer.index(term_char) + 1
                    reason |= 2
                part = bytes(read_buffer[:size])
                del read_buffer[:size]
                if not read_buffer:
                    reason |= 4
                body = self.words(0, reason) + self.opaque(part)
            elif procedure == 23:
                body = self.words(0)
            else:
                raise AssertionError(f"unexpected RPC procedure {procedure}")
            reply = self.words(xid, 1, 0, 0, 0, 0) + body
            # Exercise RPC record fragments as well as fragmented device_read.
            connection.sendall(self.words(3) + reply[:3])
            connection.sendall(self.words(0x80000000 | (len(reply) - 3)) + reply[3:])

    def dispatch(self, command):
        c = self.channel
        if command == "*IDN?":
            return f"Siglent Technologies,SDG6022X-E,SDG6XFCC900300,{self.firmware}"
        if command == "*OPC?":
            return "1"
        if command.startswith(f"{c}:SRATE MODE,"):
            self.rate_mode = command.split("MODE,", 1)[1].split(",")[0]
            value = re.search(r"VALUE,([0-9.e+-]+)", command)
            if value:
                self.rate = float(value[1])
        if command.startswith(f"{c}:BSWV "):
            kind = re.search(r"WVTP,(\w+)", command)
            if kind:
                self.wave = kind[1]
            for key, attribute in (("AMP", "amp"), ("OFST", "offset"), ("FRQ", "frequency")):
                value = re.search(key + r",([0-9.e+-]+)", command)
                if value:
                    setattr(self, attribute, float(value[1]))
        if command == f"{c}:OUTP ON" or command == f"{c}:OUTP OFF":
            self.output = command.rsplit(" ", 1)[1]
            self.events.append(self.output)
        if command in (f"{c}:CMBN ON", f"{c}:CMBN OFF"):
            self.combine = command.rsplit(" ", 1)[1]
        if command == f"{c}:CMBN?":
            return f"{c}:CMBN {self.combine}"
        if command == f"{c}:BSWV?":
            amp = self.amp * 2 if self.mismatch else self.amp
            return f"{c}:BSWV WVTP,{self.wave},FRQ,{self.frequency}HZ,AMP,{amp}V,OFST,{self.offset}V"
        if command == f"{c}:SRATE?":
            return f"{c}:SRATE MODE,{self.rate_mode},VALUE,{self.rate},INTER,HOLD"
        if command == f"{c}:ARWV?":
            return f"{c}:ARWV INDEX,0,NAME,{self.name}"
        if command == f"{c}:OUTP?":
            return f"{c}:OUTP {self.output},LOAD,HZ,PLRT,NOR"
        if command == "STL? USER":
            return "STL WVNM," + (self.name if self.uploads and not self.missing_user_file else "")
        if command.startswith("WVDT?"):
            if not self.byte_readback:
                return (f"WVDT POS, {command.split(' ', 1)[1].upper()}, WVNM, , "
                        "LENGTH, 73728B, TYPE, 6, WAVEDATA,").encode() + b"\x06\0" * 36864 + b"\n"
            data = self.uploads[-1]
            if self.corrupt_bytes:
                data = bytes([data[0] ^ 1]) + data[1:]
            return (f"WVDT POS, USER, WVNM, {self.name}.bin, LENGTH, {len(data)}B, "
                    "TYPE, 6, WAVEDATA,").encode() + data + b"\n"
        return None

    def close(self):
        self.stop.set()
        self.thread.join(3)
        if self.thread.is_alive():
            self.listener.close()
            raise AssertionError("SCPI fixture did not stop")
        if self.error:
            raise self.error


class ReplaySerial(LiveSerial):
    def __init__(self, instrument, status, checksum=None, wire_vref=3300000, wire_gain=1000000,
                 sample_rate=None, phase_samples=0):
        super().__init__(b"")
        self.instrument, self.status = instrument, status
        self.checksum, self.wire_vref, self.wire_gain = checksum, wire_vref, wire_gain
        self.sample_rate, self.phase_samples = sample_rate, phase_samples

    def write(self, data):
        import numpy as np
        self.instrument.events.append("R")
        assert "开始测试" in self.status.getvalue()
        assert self.instrument.output == "ON"
        dac = np.frombuffer(self.instrument.uploads[-1], dtype="<i2").astype(float)
        sample_rate = self.sample_rate or self.instrument.rate
        n_capture = 8192 if (self.sample_rate is None and len(dac) > 6000) else host.SAMPLES
        # Form the rate ratio first: at equal clocks, multiply-then-divide
        # roundoff can otherwise turn sample 13 into 12.999999999999998.
        indices = np.floor(np.arange(n_capture) * (self.instrument.rate / sample_rate) +
                           self.phase_samples).astype(int) % len(dac)
        dac = dac[indices]
        adc = np.rint((dac / 32767 + 1) * 4095 / 2).astype("<u2").tobytes()
        self.reply = response(adc, fs=round(sample_rate * 1000),
            vref=self.wire_vref, gain=self.wire_gain, checksum=self.checksum)
        if len(adc) == host.MAX_PAYLOAD_BYTES:
            self.reply = self.reply.replace(b"proto=2,bytes=8192,", b"proto=3,bytes=16384,", 1)
        return super().write(data)


class ROTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def options(self, instrument, *extra):
        return parser().parse_args(["RO", "--port", "FAKE", "--ro-host", "127.0.0.1",
            "--ro-transport", "vxi11", "--ro-port", str(instrument.port),
            "--ro-settle", "0", "--ro-calibrate", "off", *extra])

    def test_entire_original_matrix_has_exact_adc_and_dac_roundtrip(self):
        import numpy as np
        from tools.self_test import generate
        vref, gain = board_levels()
        cases = list(matrix())
        self.assertEqual(len(cases), 296)
        for case in cases:
            original, _, _ = generate(case)
            adc, dac, config = encode(case, vref, gain)
            self.assertEqual(adc, original)
            codes = np.frombuffer(dac, dtype="<i2").astype(float)
            restored = np.rint((codes / 32767 + 1) * 4095 / 2).astype("<u2").tobytes()
            self.assertEqual(restored[:host.PAYLOAD_BYTES], original)
            self.assertGreaterEqual(config["input_min_v"], 0)
            self.assertLessEqual(config["input_max_v"], vref / gain)
            self.assertAlmostEqual(config["loop_frequency_hz"] * len(codes), case.fs_hz)

    def test_finals_below_600_matrix_has_exact_adc_and_dac_roundtrip(self):
        import numpy as np
        vref, gain = board_levels()
        cases = list(ro_matrix())
        self.assertEqual(len(cases), 296)
        self.assertTrue(all(100 <= c.frequency_hz <= 600 and c.noise_pp_ratio <= 0.5 for c in cases))
        self.assertEqual(sum(c.group == "final_weak" for c in cases), 148)
        self.assertEqual(sum(c.group == "final_strong" for c in cases), 148)
        from tools.ro_test import _cached_legacy_finals_bundle
        fingerprints = (
            json.loads((ROOT / "validation/fingerprints_new48.json").read_text()) +
            json.loads((ROOT / "validation/fingerprints_fresh96.json").read_text()) +
            json.loads((ROOT / "validation/fingerprints_221.json").read_text())
        )
        self.assertEqual(len(fingerprints), 365)
        by_id = {f"{group}-{i+1:04d}":sha
                 for group, start, size in (("new48",0,48),("fresh96",48,96),("paired221",144,221))
                 for i, sha in enumerate(fingerprints[start:start+size])}
        for case in _cached_legacy_finals_bundle()[0]:
            expected_sha = by_id[case.id]
            original, _, _ = generate_ro(case)
            self.assertEqual(hashlib.sha256(original).hexdigest(), expected_sha)
        for case in cases:
            original, _, _ = generate_ro(case)
            adc, dac, config = encode(case, vref, gain)
            self.assertEqual(adc, original)
            codes = np.frombuffer(dac, dtype="<i2").astype(float)
            restored = np.rint((codes / 32767 + 1) * 4095 / 2).astype("<u2").tobytes()
            self.assertEqual(restored[:len(original)], original)
            self.assertGreaterEqual(config["input_min_v"], 0)
            self.assertLessEqual(config["input_max_v"], vref / gain)
            self.assertAlmostEqual(config["loop_frequency_hz"] * len(codes), case.fs_hz)

    def test_frequency_limit_excludes_boundary_and_applies_before_selection(self):
        from types import SimpleNamespace
        fixtures=tuple(Case(id=f'boundary-{i}',frequency_hz=f) for i,f in
                       enumerate((599.999,600.,600.001)))
        with patch('tools.ro_test._cached_finals_bundle',return_value=(fixtures,{})):
            self.assertEqual([c.id for c in ro_matrix()],['boundary-0'])
            with self.assertRaisesRegex(ValueError,'unknown case ID'):
                select_ro_cases(SimpleNamespace(case_id=['boundary-1']))
        for group,expected in (('new48',12),('fresh96',37),('paired221',92)):
            selected=select_ro_cases(SimpleNamespace(case_id=[group]))
            self.assertEqual(len(selected),expected)
            self.assertTrue(all(200<=c.frequency_hz<600 and c.noise_pp_ratio<0.5 for c in selected))
        self.assertEqual(len(select_ro_cases(SimpleNamespace(max_cases=5))),5)

    def test_list_matrix_exports_the_same_filtered_ro_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('tools.ro_test.Generator',side_effect=AssertionError('offline listing opened generator')):
                code=main(['RO','--list-matrix','--data-dir',directory,'--case-id','new48'])
            self.assertEqual(code,0)
            report=next(Path(directory).glob('*/matrix.json'))
            cases=json.loads(report.read_text())['cases']
            self.assertEqual(len(cases),12)
            self.assertTrue(all(200<=c['frequency_hz']<600 and c['noise_pp_ratio']<0.5 and c['group']=='new48' for c in cases))

    def test_source_mode_rejects_unsafe_volts_and_keeps_safe_truth(self):
        case = list(matrix())[1]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            encode(case, 3.3, 1., "source")
        _, _, config = encode(Case(), 3.3, 1., "source")
        self.assertEqual(config["voltage_scale"], 1.)
        self.assertEqual(config["truth"]["vpp_v"], 1.5)

    def test_vxi11_upload_and_channel_two_are_exact(self):
        instrument = ScpiServer(channel=2)
        _, data, config = encode(Case(), 3.3, 1.)
        try:
            with Generator("127.0.0.1", instrument.port, channel=2) as generator:
                generator.configure(data, config)
        finally:
            instrument.close()
        self.assertEqual(instrument.uploads, [data])
        self.assertNotIn("C1:", "\n".join(instrument.commands))
        self.assertNotIn("*RST", instrument.commands)
        self.assertEqual(instrument.output, "OFF")

    def test_default_r_return_contract_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            output = io.StringIO()
            port = LiveSerial(response(sine_payload()))
            service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO())
            try:
                self.assertIsNone(service.measure(timeout=3))
                line, details, elapsed, meta, record = service.measure(timeout=3, return_result=True)
            finally:
                service.close()
            self.assertEqual(bytes(port.written), b"RR")
            self.assertEqual(output.getvalue().splitlines()[-1], measurement_output(line))
            self.assertEqual(details["sequence"], meta.sequence)
            self.assertEqual(record.info["source"], "uart")

    def test_original_r_pipeline_and_matrix_archive(self):
        instrument = ScpiServer()
        status, output = io.StringIO(), io.StringIO()
        port = ReplaySerial(instrument, status)
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                service = SerialService(port, store, Engine(self.api, output), stderr=status)
                try:
                    code = run(service, store, self.options(instrument, "--max-cases", "2"), status)
                finally:
                    service.close()
                self.assertEqual(code, 0, status.getvalue())
                self.assertEqual(bytes(port.written), b"RRRR")
                self.assertEqual(len(output.getvalue().splitlines()), 2)
                self.assertTrue(all(len(line.split(",")) == 5 and line.startswith("RESULT,")
                                    for line in output.getvalue().splitlines()))
                report = next(store.path.glob("ro-*"))
                summary = json.loads((report / "summary.json").read_text())
                self.assertTrue(summary["all_passed"])
                self.assertEqual(summary["not_run"], [])
                original = [asdict(c) for c in list(ro_matrix())[:2]]
                self.assertEqual(json.loads((report / "matrix.json").read_text())["cases"], original)
                entries = json.loads((report / "results.json").read_text())["results"]
                for entry in entries:
                    self.assertEqual(len(entry["captures"]), 2)
                    self.assertEqual(entry["algorithm"]["batch_frames"], 2)
                    captured = json.loads((store.path / entry["capture"] / "capture.json").read_text())
                    self.assertEqual(captured["source"], "uart")
                    self.assertEqual(captured["self_test"], entry["result"])
                    self.assertEqual(captured["metadata"]["reference_microvolts"], 3300000)
                    self.assertEqual(captured["metadata"]["gain_ppm"], 1000000)
                    # The ADC sees the quantized DAC replay, which can differ
                    # by one ADC code from the pre-encoding RT source.
                    import numpy as np
                    dac = np.frombuffer((report / entry["case_id"] / "generator.i16.bin").read_bytes(),
                                        dtype='<i2').astype(float)
                    for capture in entry["captures"]:
                        received = (store.path / capture / "samples.adc.bin").read_bytes()
                        indices = np.arange(len(received)//2) % len(dac)
                        expected = np.rint((dac[indices]/32767+1)*4095/2).astype('<u2').tobytes()
                        self.assertEqual(received, expected)
                self.assertEqual(status.getvalue().count("开始测试"), 1)
        finally:
            instrument.close()
        self.assertEqual(instrument.events, ["OFF", "OFF", "ON", "R", "R", "OFF", "ON", "R", "R", "OFF"])

    def test_failed_verdict_can_stop_early_and_turn_output_off(self):
        instrument = ScpiServer()
        status = io.StringIO()
        port = ReplaySerial(instrument, status)
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                service = SerialService(port, store, Engine(self.api, io.StringIO()), stderr=status)
                from tools.ro_test import evaluate_ro as real_evaluate
                def fail_first(*a, **kw):
                    result = real_evaluate(*a, **kw)
                    result.update(passed=False, verdict="FAIL", failure_reasons=["injected_failure"])
                    return result
                try:
                    with patch('tools.ro_test.evaluate_ro', side_effect=fail_first):
                        with self.assertRaisesRegex(RuntimeError, 'first failed verdict'):
                            run(service, store, self.options(instrument, '--max-cases', '2',
                                                            '--ro-stop-on-failure'), status)
                finally:
                    service.close()
                report = next(store.path.glob('ro-*'))
                summary = json.loads((report / 'summary.json').read_text())
                self.assertEqual(summary['counts']['completed'], 1)
                self.assertEqual(summary['counts']['failed'], 1)
                self.assertEqual(len(summary['not_run']), 1)
                self.assertFalse(summary['complete'])
        finally:
            instrument.close()
        self.assertEqual(instrument.output, 'OFF')

    def test_readback_mismatch_never_starts_r_and_saves_incomplete_summary(self):
        instrument = ScpiServer(mismatch=True)
        status = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            service = SerialService(LiveSerial(b""), store, Engine(self.api, io.StringIO()), stderr=status)
            try:
                with self.assertRaisesRegex(RuntimeError, "AMP differs"):
                    run(service, store, self.options(instrument, "--max-cases", "2"), status)
            finally:
                service.close()
                instrument.close()
            self.assertEqual(bytes(service.raw_port.written), b"")
            self.assertNotIn("开始测试", status.getvalue())
            summary = json.loads(next(store.path.glob("ro-*/summary.json")).read_text())
            self.assertFalse(summary["complete"])
            self.assertEqual(len(summary["not_run"]), 2)
            self.assertEqual(instrument.output, "OFF")

    def test_truncated_uart_frame_retries_current_case_and_completes_matrix(self):
        instrument = ScpiServer()
        status, output = io.StringIO(), io.StringIO()

        class InterruptedReplay(ReplaySerial):
            writes = 0
            open_calls = 0

            def inject(self, data):
                if self.writes == 1:
                    start = data.index(b"\n", data.index(b"DUMP,")) + 1
                    data = data[:start + 4000] + data[start + 6001:]
                super().inject(data)

            def write(self, data):
                self.writes += 1
                return super().write(data)

            def open(self):
                self.open_calls += 1
                self.is_open = True

            @property
            def in_waiting(self):
                return len(self.buffer)

        try:
            with tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                port = InterruptedReplay(instrument, status)
                service = SerialService(port, store, Engine(self.api, output), stderr=status)
                try:
                    code = run(service, store, self.options(instrument, "--max-cases", "2"), status)
                finally:
                    service.close()
                self.assertEqual(code, 0, status.getvalue())
                self.assertEqual(bytes(port.written), b"RRRRR")
                self.assertEqual(port.open_calls, 1)
                self.assertEqual(len(output.getvalue().splitlines()), 2)
                report = next(store.path.glob("ro-*"))
                summary = json.loads((report / "summary.json").read_text())
                self.assertTrue(summary["complete"])
                self.assertEqual(summary["counts"], {"completed": 2, "passed": 2})
                entries = json.loads((report / "results.json").read_text())["results"]
                self.assertEqual([row["case_id"] for row in entries], [c.id for c in list(ro_matrix())[:2]])
                records = sorted(store.path.glob("uart/*/capture.json"))
                self.assertEqual(len(records), 5)
                self.assertEqual(json.loads(records[0].read_text())["validation"], "invalid")
                self.assertEqual(entries[0]["capture"], records[2].parent.relative_to(store.path).as_posix())
                self.assertEqual(entries[1]["capture"], records[4].parent.relative_to(store.path).as_posix())
        finally:
            instrument.close()
        self.assertEqual(instrument.events,
            ["OFF", "OFF", "ON", "R", "R", "R", "OFF", "ON", "R", "R", "OFF"])

    def test_bad_uart_checksum_stops_matrix_and_preserves_received_frame(self):
        instrument = ScpiServer()
        status, output = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            port = ReplaySerial(instrument, status, checksum=0)
            service = SerialService(port, store, Engine(self.api, output), stderr=status)
            try:
                with self.assertRaisesRegex(ValueError, "integrity"):
                    run(service, store, self.options(instrument, "--max-cases", "2"), status)
            finally:
                service.close()
                instrument.close()
            self.assertEqual(bytes(port.written), b"R")
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(instrument.output, "OFF")
            capture = json.loads(next(store.path.glob("uart/*/capture.json")).read_text())
            self.assertEqual(capture["validation"], "invalid")
            self.assertFalse(json.loads(next(store.path.glob("ro-*/summary.json")).read_text())["complete"])

    def test_r_timeout_and_keyboard_interrupt_turn_off_generator(self):
        for interruption in (False, True):
            with self.subTest(interruption=interruption):
                instrument = ScpiServer()
                status = io.StringIO()
                with tempfile.TemporaryDirectory() as directory:
                    store = CaptureStore(directory)
                    service = SerialService(LiveSerial(b""), store, Engine(self.api, io.StringIO()), stderr=status)
                    args = self.options(instrument, "--max-cases", "2", "--ro-timeout", ".2")
                    try:
                        if interruption:
                            with patch.object(service, "measure", side_effect=KeyboardInterrupt):
                                with self.assertRaises(KeyboardInterrupt):
                                    run(service, store, args, status)
                        else:
                            with self.assertRaises(TimeoutError):
                                run(service, store, args, status)
                    finally:
                        service.close()
                        instrument.close()
                    self.assertEqual(instrument.output, "OFF")
                    summary = json.loads(next(store.path.glob("ro-*/summary.json")).read_text())
                    self.assertEqual(len(summary["not_run"]), 2)

    def test_evaluation_uses_real_r_reference_and_gain(self):
        instrument = ScpiServer()
        status, output = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            port = ReplaySerial(instrument, status, wire_vref=3000000, wire_gain=2000000)
            service = SerialService(port, store, Engine(self.api, output), stderr=status)
            try:
                run(service, store, self.options(instrument, "--max-cases", "1"), status)
            finally:
                service.close()
                instrument.close()
            entry = json.loads(next(store.path.glob("ro-*/results.json")).read_text())["results"][0]
            expected = entry["algorithm"]["dc_offset_v"]
            self.assertAlmostEqual(entry["result"]["measured"]["dc_v"], expected)
            self.assertEqual(entry["result"]["truth"], entry["generator"]["truth"])

    def test_real_board_sampling_and_unsynchronised_replay_phases(self):
        for phase in (0, 1024, 2048, 3072):
            with self.subTest(phase=phase):
                instrument = ScpiServer()
                status, output = io.StringIO(), io.StringIO()
                with tempfile.TemporaryDirectory() as directory:
                    store = CaptureStore(directory)
                    port = ReplaySerial(instrument, status, sample_rate=20000, phase_samples=phase)
                    service = SerialService(port, store, Engine(self.api, output), stderr=status)
                    try:
                        run(service, store, self.options(instrument, "--case-id", "competition-000001"), status)
                    finally:
                        service.close()
                        instrument.close()
                    row = json.loads(next(store.path.glob("ro-*/results.json")).read_text())["results"][0]
                    self.assertEqual(row["metadata"]["sample_rate_millihz"], 20000000)
                    self.assertEqual(row["case"]["fs_hz"], 19760.)
                    self.assertEqual(row["generator"]["sample_rate_hz"], 19760.)
                    self.assertEqual(output.getvalue().split(",")[:2], ["RESULT", "sine"])
                    self.assertAlmostEqual(row["result"]["measured"]["frequency_hz"], 100., delta=1.)

    def test_invalid_settings_or_case_id_fail_before_network(self):
        args = parser().parse_args(["RO", "--port", "FAKE", "--max-cases", "1"])
        for changed in (replace(Case(), fault="invalid_code"),):
            with self.assertRaisesRegex(ValueError, "invalid ADC"):
                encode(changed, 3.3, 1.)
        for key, value in (("ro_timeout", float("nan")), ("ro_settle", -1.),
                           ("ro_port", 0), ("case_id", ["missing"])):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                bad = parser().parse_args(["RO", "--port", "FAKE", "--max-cases", "1"])
                setattr(bad, key, value)
                with patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
                    with self.assertRaises(ValueError):
                        run(None, CaptureStore(directory), bad, io.StringIO())

    def test_encode_only_runs_without_a_board(self):
        """No --port: the board link is optional, so RO still encodes and uploads."""
        instrument = ScpiServer()
        with tempfile.TemporaryDirectory() as directory, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            try:
                code = main(["RO", "--data-dir", directory, "--ro-host", "127.0.0.1",
                             "--ro-transport", "vxi11",
                             "--ro-port", str(instrument.port), "--ro-settle", "0",
                             "--max-cases", "2"])
            finally:
                instrument.close()
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("no board attached", stderr.getvalue())
            self.assertNotIn("开始测试", stderr.getvalue())
            self.assertNotIn("R", instrument.events)
            self.assertEqual(len(instrument.uploads), 2)
            self.assertEqual(instrument.output, "OFF")
            report = next(Path(directory).glob("*/ro-*"))
            summary = json.loads((report / "summary.json").read_text())
            self.assertEqual(summary["mode"], "encode-only")
            self.assertFalse(summary["board"])
            self.assertEqual(summary["counts"], {"encoded": 2})
            self.assertTrue(summary["all_passed"])
            self.assertEqual(summary["not_run"], [])
            self.assertEqual(json.loads((report / "instrument.json").read_text())["mode"],
                             "encode-only")
            entries = json.loads((report / "results.json").read_text())["results"]
            self.assertEqual([e["result"]["verdict"] for e in entries], ["ENCODED", "ENCODED"])
            for entry in entries:
                self.assertIsNone(entry["capture"])
                source = report / entry["case_id"]
                self.assertTrue((source / "rt.adc.bin").read_bytes())
                self.assertTrue((source / "generator.i16.bin").read_bytes())
                encoding = json.loads((source / "encoding.json").read_text())
                self.assertEqual(encoding["readback"]["output"][0], "OFF")

    def test_unopenable_port_degrades_to_encode_only(self):
        instrument = ScpiServer()
        with tempfile.TemporaryDirectory() as directory, \
             patch("serial.Serial", side_effect=OSError(22, "信号灯超时时间已到")), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            try:
                code = main(["RO", "--port", "COM6", "--data-dir", directory,
                             "--ro-transport", "vxi11",
                             "--ro-host", "127.0.0.1", "--ro-port", str(instrument.port),
                             "--ro-settle", "0", "--max-cases", "1"])
            finally:
                instrument.close()
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertIn("continuing without the board", stderr.getvalue())
            summary = json.loads(next(Path(directory).glob("*/ro-*/summary.json")).read_text())
            self.assertEqual(summary["mode"], "encode-only")
            self.assertTrue(summary["all_passed"])

    def test_firmware_compatibility_checks_each_file_and_records_no_byte_verification(self):
        instrument = ScpiServer(byte_readback=False)
        status = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                code = run(None, store, self.options(instrument, "--max-cases", "2"), status)
                self.assertEqual(code, 0, status.getvalue())
                report = next(store.path.glob("ro-*"))
                entries = json.loads((report / "results.json").read_text())["results"]
                for entry in entries:
                    info = entry["generator"]["readback"]["waveform"]
                    self.assertFalse(info["verified"])
                    self.assertFalse(info["byte_readback_supported"])
                    self.assertTrue(info["configuration_verified"])
                    self.assertEqual(info["uploaded_bytes"],
                                     (report / entry["case_id"] / "generator.i16.bin").stat().st_size)
                    self.assertEqual(len(info["query_attempts"]), 4)
                self.assertEqual(sum(c.startswith("WVDT?") for c in instrument.commands), 4)
                self.assertEqual(instrument.commands.count("STL? USER"), 2)
                self.assertEqual(instrument.output, "OFF")
        finally:
            instrument.close()

    def test_named_byte_mismatch_never_uses_firmware_compatibility(self):
        instrument = ScpiServer(corrupt_bytes=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                with self.assertRaisesRegex(RuntimeError, "waveform readback differs"):
                    run(None, store, self.options(instrument, "--max-cases", "2"), io.StringIO())
                summary = json.loads(next(store.path.glob("ro-*/summary.json")).read_text())
                self.assertFalse(summary["complete"])
                self.assertFalse(summary["waveform_readback"]["verified"])
                self.assertNotIn("STL? USER", instrument.commands)
                self.assertNotIn("ON", instrument.events)
                self.assertEqual(instrument.output, "OFF")
        finally:
            instrument.close()

    def test_firmware_compatibility_rejects_missing_user_file(self):
        instrument = ScpiServer(byte_readback=False, missing_user_file=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "missing from user files"):
                    run(None, CaptureStore(directory), self.options(instrument, "--max-cases", "1"),
                        io.StringIO())
                self.assertNotIn("ON", instrument.events)
                self.assertEqual(instrument.output, "OFF")
        finally:
            instrument.close()

    def test_unknown_firmware_cannot_bypass_byte_verification(self):
        instrument = ScpiServer(byte_readback=False, firmware="other-version")
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "did not resolve uploaded waveform"):
                    run(None, CaptureStore(directory), self.options(instrument, "--max-cases", "1"),
                        io.StringIO())
                self.assertNotIn("STL? USER", instrument.commands)
                self.assertNotIn("ON", instrument.events)
        finally:
            instrument.close()


if __name__ == "__main__":
    unittest.main()
