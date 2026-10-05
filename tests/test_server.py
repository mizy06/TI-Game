"""Observable command, archival and contest-coverage contracts."""
import io
import json
import math
import subprocess
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
                self.assertEqual(line.split(",")[0], "sine")

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

    def test_rt_cli_is_offline_and_stdout_has_only_original_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "server.py"), "RT",
                "--max-cases", "8", "--data-dir", directory], capture_output=True, text=True, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 8)
            for line in result.stdout.splitlines():
                fields = line.split(",")
                self.assertEqual(len(fields), 5)
                self.assertIn(fields[0], host.NAMES)
                self.assertTrue(all(math.isfinite(float(x)) for x in fields[1:]))
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

    def test_interactive_commands_keep_diagnostics_out_of_ide_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(ROOT / "server.py"), "--data-dir", directory,
                "--max-cases", "2"], input="bad\nR\nRT\nQ\n", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertIn("R requires --port", next(Path(directory).glob("*/server.log")).read_text())
            self.assertEqual(len(result.stdout.splitlines()), 2)

    def test_strong_triangle_spectral_and_amplitude_backstops(self):
        selected = [c for c in matrix() if c.id in ("competition-000263", "competition-000279", "competition-000295")]
        self.assertEqual(len(selected), 3)
        for case in selected:
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
        self.assertEqual(sum(c.expectation == "measure" for c in cases), 200)
        self.assertEqual(sum(c.expectation == "observe" for c in cases), 96)

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
