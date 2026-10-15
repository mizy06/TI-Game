"""Observable command, archival and contest-coverage contracts."""
import io
import json
import math
import subprocess
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import host_waveform as host
from tools.capture_store import CaptureStore, RecordingSerial
from tools.self_test import Case, generate, matrix
from tools.waveform_server import Engine, SerialService
from test_host_integration import PartialSerial, response, sine_payload


class LiveSerial:
    """UART with asynchronous incoming data and fragmented blocking reads."""
    def __init__(self, reply):
        self.reply = reply
        self.buffer = bytearray()
        self.condition = threading.Condition()
        self.written = bytearray()
        self.is_open = True

    def inject(self, data):
        with self.condition:
            self.buffer.extend(data)
            self.condition.notify_all()

    def read(self, count):
        with self.condition:
            if not self.buffer and self.is_open:
                self.condition.wait(.05)
            data = bytes(self.buffer[:min(count, 73)])
            del self.buffer[:len(data)]
            return data

    def read_until(self, separator=b"\n", size=256):
        data = bytearray()
        while len(data) < size:
            item = self.read(1)
            if not item:
                break
            data.extend(item)
            if item == separator:
                break
        return bytes(data)

    def write(self, data):
        self.written.extend(data)
        self.inject(self.reply)
        return len(data)

    def flush(self):
        pass

    def close(self):
        with self.condition:
            self.is_open = False
            self.condition.notify_all()


class StalledSerial(LiveSerial):
    """Board keeps capturing, but the host handle loses RX until it reopens."""
    def __init__(self, reply):
        super().__init__(reply)
        self.stalled = False
        self.cancelled = False
        self.fail_open = False
        self.late_on_cancel = False
        self.open_calls = 0
        self.close_calls = 0
        self.captures = 0
        self.last_reply = b""

    def write(self, data):
        self.written.extend(data)
        self.captures += 1
        self.last_reply = self.reply.replace(b"seq=37,", f"seq={self.captures},".encode())
        if not self.stalled:
            self.inject(self.last_reply)
        return len(data)

    def read(self, count):
        with self.condition:
            if not self.buffer and self.is_open and not self.cancelled:
                self.condition.wait(.05)
            if self.cancelled:
                self.cancelled = False
                return b""
            data = bytes(self.buffer[:min(count, 73)])
            del self.buffer[:len(data)]
            return data

    @property
    def in_waiting(self):
        return len(self.buffer)

    def cancel_read(self):
        with self.condition:
            if self.late_on_cancel:
                self.buffer.extend(self.last_reply[self.last_reply.index(b"DUMP,"):])
            else:
                self.cancelled = True
            self.condition.notify_all()

    def close(self):
        self.close_calls += 1
        super().close()

    def open(self):
        self.open_calls += 1
        if self.fail_open:
            raise OSError("device still unavailable")
        self.is_open, self.stalled, self.cancelled = True, False, False

    def reset_input_buffer(self):
        raise AssertionError("Recovery must not discard the UART RX buffer")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def test_r_and_unsolicited_button_frames_are_both_saved(self):
        payload = sine_payload()
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory, sample_csv=True)
            output = io.StringIO()
            port = LiveSerial(response(payload))
            service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO())
            try:
                port.inject(response(payload))  # PB13 unsolicited frame, no command.
                deadline = time.monotonic() + 3
                while len(output.getvalue().splitlines()) < 1 and time.monotonic() < deadline:
                    threading.Event().wait(.01)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                service.measure(timeout=3)
                self.assertEqual(bytes(port.written), b"R")
            finally:
                service.close()
            records = sorted(store.path.glob("uart/*/capture.json"))
            self.assertEqual(len(records), 2)
            for record in records:
                info = json.loads(record.read_text())
                self.assertEqual(info["validation"], "valid")
                self.assertEqual(info["algorithm"]["sequence"], 37)
                self.assertEqual((record.parent / "samples.adc.bin").read_bytes(), payload)
                self.assertEqual(len((record.parent / "samples.csv").read_text().splitlines()), 4097)
            self.assertEqual((store.path / "uart_rx.bin").read_bytes(), response(payload) * 2)
            for line in output.getvalue().splitlines():
                self.assertEqual(len(line.split(",")), 5)
                self.assertEqual(line.split(",")[:2], ["RESULT", "sine"])
                self.assertEqual(line.split(",")[4], "0")

    def test_bad_checksum_and_partial_adc_bytes_survive_validation(self):
        payload = sine_payload()
        for wire, error in ((response(payload, checksum=123), ValueError),
                            (response(payload).split(payload)[0] + payload[:101], TimeoutError)):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                store = CaptureStore(directory)
                port = RecordingSerial(PartialSerial(wire), store.path)
                try:
                    with self.assertRaises(error):
                        host.receive_frame(port, store=store)
                finally:
                    port.close()
                record = next(store.path.glob("uart/*/capture.json"))
                info = json.loads(record.read_text())
                self.assertEqual(info["validation"], "invalid")
                saved = (record.parent / "samples.adc.bin").read_bytes()
                self.assertEqual(saved, payload if error is ValueError else payload[:101])
                self.assertEqual((store.path / "uart_rx.bin").read_bytes(), wire)
                self.assertNotIn("algorithm", info)

    def test_checksum_error_has_no_stdout_and_next_r_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            store = CaptureStore(directory)
            port = LiveSerial(response(sine_payload(), checksum=0))
            service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO())
            try:
                with self.assertRaisesRegex(ValueError, "integrity"):
                    service.measure(timeout=3)
                self.assertEqual(output.getvalue(), "")
                port.reply = response(sine_payload())
                service.measure(timeout=3)
            finally:
                service.close()
            self.assertEqual(len(output.getvalue().splitlines()), 1)
            self.assertEqual(len(list(store.path.glob("uart/*/samples.adc.bin"))), 2)

    def test_no_signal_frame_reports_rejection_without_a_contest_result(self):
        for dc in (0, 100, 2048, 4095):
            with self.subTest(dc=dc), tempfile.TemporaryDirectory() as directory:
                payload = struct.pack("<4096H", *([dc] * 4096))
                output = io.StringIO()
                errors = io.StringIO()
                store = CaptureStore(directory)
                port = LiveSerial(response(payload))
                service = SerialService(port, store, Engine(self.api, output, stderr=errors), stderr=errors)
                try:
                    service.measure(timeout=2)
                finally:
                    service.close()
                self.assertEqual(output.getvalue(), "")
                self.assertIn("measurement rejected: status=", errors.getvalue())
                info = json.loads(next(store.path.glob("uart/*/capture.json")).read_text())
                self.assertEqual(info["validation"], "valid")
                self.assertFalse(info["algorithm"]["ok"])
                self.assertNotEqual(info["algorithm"]["status"], 0)
                self.assertEqual(info["display_output"], "")
                self.assertEqual(info["output"], "unknown,0,0,0,0")
                self.assertEqual((next(store.path.glob("uart/*/samples.adc.bin"))).read_bytes(), payload)

    def test_no_response_times_out_and_next_r_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            port = LiveSerial(b"")
            service = SerialService(port, CaptureStore(directory), Engine(self.api, output), stderr=io.StringIO())
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, "waiting for board response"):
                    service.measure(timeout=.04)
                self.assertLess(time.monotonic() - started, .5)
                self.assertIsNone(service.pending)
                self.assertEqual(output.getvalue(), "")
                port.reply = response(sine_payload())
                service.measure(timeout=3)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
            finally:
                service.close()

    def test_idle_receive_stall_recovers_with_one_retry_and_keeps_the_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store, output, errors = CaptureStore(directory), io.StringIO(), io.StringIO()
            port = StalledSerial(response(sine_payload()))
            service = SerialService(port, store, Engine(self.api, output), stderr=errors)
            service.RESPONSE_GRACE_S = .05
            try:
                service.measure(timeout=3)
                original_wire = (store.path / "uart_rx.bin").read_bytes()
                port.stalled = True
                service.measure(timeout=3)
                service.measure(timeout=3)
                self.assertEqual(port.open_calls, 1)
                self.assertEqual(bytes(port.written), b"RRRR")
                self.assertEqual(len(output.getvalue().splitlines()), 3)
                self.assertTrue((store.path / "uart_rx.bin").read_bytes().startswith(original_wire))
                records = sorted(store.path.glob("uart/*/capture.json"))
                self.assertEqual([json.loads(p.read_text())["metadata"]["sequence"] for p in records], [1, 3, 4])
                events = [json.loads(line) for line in (store.path / "connection.jsonl").read_text().splitlines()]
                self.assertEqual(sum(e["event"] == "reconnect_complete" for e in events), 1)
                self.assertEqual(sum(e["event"] == "command_sent" and e["attempt"] == 2 for e in events), 1)
                self.assertIn("reopened", errors.getvalue())
                self.assertTrue(service.thread.is_alive())
            finally:
                service.close()

    def test_late_frame_during_receive_recovery_is_not_requested_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store, output = CaptureStore(directory), io.StringIO()
            port = StalledSerial(response(sine_payload()))
            port.stalled, port.late_on_cancel = True, True
            service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO())
            service.RESPONSE_GRACE_S = .05
            try:
                service.measure(timeout=3)
                self.assertEqual(bytes(port.written), b"R")
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertEqual(len(list(store.path.glob("uart/*/capture.json"))), 1)
                self.assertEqual(port.open_calls, 1)
            finally:
                service.close()

    def test_recovery_open_failure_is_reported_without_repeated_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            output, errors = io.StringIO(), io.StringIO()
            store = CaptureStore(directory)
            port = StalledSerial(response(sine_payload()))
            port.stalled, port.fail_open = True, True
            service = SerialService(port, store, Engine(self.api, output), stderr=errors)
            service.RESPONSE_GRACE_S = .05
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(OSError, "device still unavailable"):
                    service.measure(timeout=3)
                self.assertLess(time.monotonic() - started, 2.)
                self.assertEqual(bytes(port.written), b"R")
                self.assertEqual(output.getvalue(), "")
                self.assertFalse(service.thread.is_alive())
                events = [json.loads(line) for line in (store.path / "connection.jsonl").read_text().splitlines()]
                self.assertTrue(any(e["event"] == "reconnect_failed" for e in events))
                port.fail_open = False
                service.measure(timeout=3)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
            finally:
                service.close()

    def test_board_already_capturing_is_not_reopened_or_commanded_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            port = StalledSerial(b"CAPTURING\r\n")
            service = SerialService(port, CaptureStore(directory), Engine(self.api, io.StringIO()), stderr=io.StringIO())
            service.RESPONSE_GRACE_S = .05
            try:
                with self.assertRaisesRegex(TimeoutError, "board ADC/DMA capture"):
                    service.measure(timeout=.2)
                self.assertEqual(bytes(port.written), b"R")
                self.assertEqual(port.open_calls, 0)
            finally:
                service.close()

    def test_board_capture_timeout_is_reported_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            port = LiveSerial(b"CAPTURING\r\nERR_ADC_DMA\r\nREADY\r\n")
            service = SerialService(port, CaptureStore(directory), Engine(self.api, output), stderr=io.StringIO())
            try:
                with self.assertRaisesRegex(RuntimeError, "ERR_ADC_DMA"):
                    service.measure(timeout=2)
                self.assertEqual(output.getvalue(), "")
                port.reply = response(sine_payload())
                service.measure(timeout=3)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
            finally:
                service.close()

    def test_capture_stage_timeout_and_command_do_not_wait_for_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            port = LiveSerial(b"CAPTURING\r\n")
            def forbidden_drain():
                raise AssertionError("R must not enter an unbounded serial flush")
            port.flush = forbidden_drain
            service = SerialService(port, CaptureStore(directory), Engine(self.api, io.StringIO()), stderr=io.StringIO())
            try:
                with self.assertRaisesRegex(TimeoutError, "board ADC/DMA capture"):
                    service.measure(timeout=.15)
            finally:
                service.close()

    def test_rt_cli_is_offline_and_stdout_uses_contest_result_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "server.py"), "RT",
                "--max-cases", "8", "--data-dir", directory], capture_output=True, text=True, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 8)
            for line in result.stdout.splitlines():
                fields = line.split(",")
                self.assertEqual(len(fields), 5)
                self.assertEqual(fields[0], "RESULT")
                self.assertIn(fields[1], host.NAMES[:-1])
                self.assertTrue(all(math.isfinite(float(x)) for x in fields[2:]))
                self.assertNotRegex(line, r"[eE][+-]?\d")
                if fields[1] != "square":
                    self.assertEqual(fields[4], "0")
            self.assertEqual(result.stderr, "")
            log = next(Path(directory).glob("*/server.log")).read_text()
            self.assertIn("RT done:", log)
            self.assertEqual(len(list(Path(directory).glob("*/rt/*/samples.adc.bin"))), 8)
            summary = json.loads(next(Path(directory).glob("*/rt-*/summary.json")).read_text())
            self.assertEqual(summary["counts"], {"passed": 8, "completed": 8})

    def test_non_50_percent_square_dc_is_not_sample_mean(self):
        for duty in (.2, .8):
            case = Case(wave="square", duty=duty, dc_v=1.4, vpp_v=1.5,
                        rise=.1, fall=.025, noise_pp_ratio=.5, bandwidth_ratio=1.5, seed=117)
            payload, meta, _ = generate(case)
            result = host.classify(self.api, payload, meta).split(",")
            self.assertEqual(result[0], "square")
            self.assertAlmostEqual(float(result[4]), 1.4, delta=.04)

    def test_interactive_errors_are_visible_and_stdout_remains_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "server.py"), "--data-dir", directory,
                "--max-cases", "2"], input="bad\nR\nRT\nQ\n", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("server: enter R, RT or Q", result.stderr)
            self.assertIn("R requires --port", result.stderr)
            self.assertNotIn("RT done:", result.stderr)
            self.assertIn("R requires --port", next(Path(directory).glob("*/server.log")).read_text())
            self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_strong_triangle_spectral_and_amplitude_backstops(self):
        from dataclasses import replace
        from tools.self_test import evaluate
        selected = [c for c in matrix() if c.id in ("competition-000263", "competition-000279", "competition-000295")]
        self.assertEqual(len(selected), 3)
        for case in selected:
            # Restored AM/interference cases have their actual clean-envelope
            # truth and normal challenge gates, rather than the old 5% bound
            # on collapsed, undistorted inputs.
            payload, meta, facts = generate(case)
            line, details = host.analyze(self.api, payload, meta)
            self.assertTrue(evaluate(case, line, details, .01, facts)["passed"])
            # Keep the original narrowband-noise amplitude backstop check.
            case = replace(case, am_depth=0, am_rate_hz=0, interferer_ratio=0,
                           interferer_frequency_ratio=0, inl_lsb=0,
                           noise_mode="gaussian_highpass", bandwidth_ratio=2.)
            payload, meta, _ = generate(case)
            line, details = host.analyze(self.api, payload, meta)
            wave,f,vpp,duty,dc = line.split(",")
            self.assertEqual(wave, "triangle")
            self.assertAlmostEqual(float(f), case.frequency_hz, delta=.5)
            self.assertAlmostEqual(float(vpp), case.vpp_v, delta=case.vpp_v*.05)
            self.assertTrue(details["ok"])

    def test_verbose_diagnostics_are_still_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "server.py"), "RT", "--max-cases", "1",
                "--verbose", "--data-dir", directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertIn("RT done:", result.stderr)
            self.assertIn("RT done:", next(Path(directory).glob("*/server.log")).read_text())


class MatrixTests(unittest.TestCase):
    def test_board_era_gate_and_challenge_matrix_is_explicit(self):
        cases = list(matrix())
        self.assertEqual(len(cases), 296)
        self.assertEqual(len({c.id for c in cases}), len(cases))
        self.assertEqual({c.suite for c in cases}, {"competition"})
        self.assertEqual({c.group for c in cases}, {"preliminary", "final_strong"})
        self.assertEqual({c.wave for c in cases}, set(host.NAMES[:-1]))
        self.assertEqual(sum(c.group == "preliminary" for c in cases), 200)
        self.assertEqual(sum(c.group == "final_strong" for c in cases), 96)
        self.assertEqual(sum(c.expectation == "measure" for c in cases), 296)
        self.assertEqual(sum(c.expectation == "observe" for c in cases), 0)

    def test_generated_contest_noise_is_zero_mean_bandlimited_and_not_clipped(self):
        import numpy as np
        for case in (Case(noise_pp_ratio=.5, bandwidth_ratio=1.5, seed=331),
                     Case(wave="sawtooth", vpp_v=2, dc_v=1.535, noise_pp_ratio=.5,
                          bandwidth_ratio=3., seed=121)):
            noisy, meta, facts = generate(case)
            from dataclasses import replace
            clean, _, _ = generate(replace(case, noise_pp_ratio=0))
            noise = np.frombuffer(noisy, dtype="<u2").astype(float) - np.frombuffer(clean, dtype="<u2")
            self.assertAlmostEqual(facts["noise_mean_v"], 0, delta=1e-12)
            self.assertLessEqual(facts["noise_vpp_v"], case.noise_pp_ratio*case.vpp_v+1e-12)
            self.assertEqual(facts["prequantization_clipped_samples"], 0)
            spectrum = np.abs(np.fft.rfft(noise))**2
            f = np.fft.rfftfreq(host.SAMPLES, 1/case.fs_hz)
            # ADC rounding leaves tiny broadband quantization residue.
            self.assertLess(spectrum[f>facts["noise_upper_hz"]].sum()/spectrum.sum(), .001)
            self.assertEqual(meta.checksum, host.fnv1a(noisy))


if __name__ == "__main__":
    unittest.main()
