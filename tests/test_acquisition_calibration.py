"""Independent reference calibration, raw archival and end-to-end RO behavior."""
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tools import host_waveform as host
from tools.acquisition_calibration import AcquisitionCalibration, load_latest_calibration, sine_period
from tools.capture_store import CaptureStore
from tools.ro_test import run
from tools.waveform_server import Engine, SerialService, main, parser
from test_host_integration import response, sine_payload
from test_ro import ScpiServer
from test_server import LiveSerial


META = host.Metadata(1, 20000000, 3300000, 1000000, 4095, 1, 0)


class BiasedBoard(LiveSerial):
    """Physical sampling clock and affine ADC response differ from wire metadata."""
    def __init__(self, instrument, *, disconnected=False, bad_validation=False,
                 arb_amplitude_gain=1., arb_offset_gain=1., arb_bias_v=0., arb_offset_curvature=0.):
        super().__init__(b"")
        self.instrument = instrument
        self.actual_rate, self.adc_scale, self.adc_offset = 20240., 1.016, -33.
        self.sequence = 0
        self.disconnected, self.bad_validation = disconnected, bad_validation
        self.arb_amplitude_gain, self.arb_offset_gain, self.arb_bias_v = (
            arb_amplitude_gain, arb_offset_gain, arb_bias_v)
        self.arb_offset_curvature = arb_offset_curvature

    def write(self, data):
        self.sequence += 1
        g = self.instrument
        g.events.append("R")
        t = np.arange(host.SAMPLES) / self.actual_rate
        if self.disconnected:
            volts = .54 + .04 * np.sin(2 * np.pi * 416.667 * t)
        elif g.output == "OFF":
            volts = np.zeros(host.SAMPLES)
        elif g.wave == "DC":
            volts = np.full(host.SAMPLES, g.offset)
            if self.bad_validation and abs(g.offset - .99) < .001:
                volts += .04
        elif g.wave == "SINE":
            volts = g.offset + .5 * g.amp * np.sin(2 * np.pi * g.frequency * t + .71)
        else:
            dac = np.frombuffer(g.uploads[-1], dtype="<i2").astype(float)
            indices = np.floor(np.arange(host.SAMPLES) * g.rate / self.actual_rate + 217).astype(int) % len(dac)
            volts = (.5 * g.amp * dac[indices] / 32767 * self.arb_amplitude_gain +
                     g.offset * self.arb_offset_gain + self.arb_bias_v +
                     self.arb_offset_curvature * (g.offset - 1.65) ** 2)
        rng = np.random.default_rng(973 + self.sequence)
        codes = np.rint(volts * (4095 / 3.3) * self.adc_scale + self.adc_offset +
                        rng.normal(0, .7, host.SAMPLES))
        payload = np.clip(codes, 0, 4095).astype("<u2").tobytes()
        self.reply = response(payload, fs=20000000).replace(
            b"seq=37,", f"seq={self.sequence},".encode(), 1)
        return super().write(data)


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def options(self, instrument, mode="auto"):
        return parser().parse_args(["RO", "--ro-host", "127.0.0.1", "--ro-port", str(instrument.port),
            "--ro-settle", "0", "--ro-calibrate", mode, "--case-id", "competition-000001",
            "--case-id", "competition-000002", "--case-id", "competition-000003",
            "--case-id", "competition-000051", "--case-id", "competition-000101"])

    def test_capture_is_raw_and_does_not_emit_or_analyze(self):
        with tempfile.TemporaryDirectory() as directory:
            store, output = CaptureStore(directory), io.StringIO()
            service = SerialService(LiveSerial(response(sine_payload())), store, Engine(self.api, output))
            try:
                payload, meta, record = service.capture(3)
            finally:
                service.close()
            self.assertEqual(payload, sine_payload())
            self.assertEqual(record.payload.read_bytes(), payload)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("algorithm", record.info)

    def test_calibration_corrects_metadata_and_counts_and_preserves_clipped_rails(self):
        calibration = AcquisitionCalibration(20240000, 1.016, -33.,
            {"sample_rate_millihz": 20000000, "reference_microvolts": 3300000,
             "gain_ppm": 1000000, "adc_full_scale": 4095})
        original = np.resize(np.array([0, 33, 100, 2000, 4095], dtype="<u2"), host.SAMPLES).tobytes()
        adjusted, meta = calibration.apply(original, META)
        values = np.frombuffer(adjusted, dtype="<u2")
        np.testing.assert_array_equal(values[:5], [0, 65, 131, 2001, 4095])
        self.assertEqual(meta.sample_rate_millihz, 20240000)
        self.assertEqual(meta.flags, 7)
        self.assertEqual(meta.checksum, host.fnv1a(adjusted))
        self.assertEqual(META.flags, 1)
        with self.assertRaisesRegex(ValueError, "metadata changed"):
            calibration.apply(original, replace(META, reference_microvolts=3000000))

    def test_timebase_estimator_rejects_flat_and_unstable_data(self):
        with self.assertRaises(ValueError):
            sine_period(np.full(host.SAMPLES, 670, dtype="<u2").tobytes())
        rng = np.random.default_rng(922)
        with self.assertRaises(ValueError):
            sine_period(rng.integers(1200, 2800, host.SAMPLES, dtype="<u2").tobytes())

    def test_biased_board_fails_nominally_and_passes_after_independent_calibration(self):
        for mode in ("off", "auto"):
            with self.subTest(mode=mode):
                instrument = ScpiServer()
                output, status = io.StringIO(), io.StringIO()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        store = CaptureStore(directory)
                        service = SerialService(BiasedBoard(instrument), store, Engine(self.api, output), stderr=status)
                        try:
                            code = run(service, store, self.options(instrument, mode), status)
                        finally:
                            service.close()
                        report = next(store.path.glob("ro-*"))
                        summary = json.loads((report / "summary.json").read_text())
                        self.assertEqual(len(output.getvalue().splitlines()), 5)
                        entries = json.loads((report / "results.json").read_text())["results"]
                        if mode == "off":
                            self.assertEqual(code, 1)
                            self.assertTrue(all("frequency_error_pct" in e["result"]["failure_reasons"] for e in entries))
                        else:
                            self.assertEqual(code, 0, json.dumps(entries, indent=2))
                            self.assertTrue(summary["all_passed"])
                            calibration = json.loads((report / "calibration.json").read_text())
                            self.assertTrue(calibration["complete"])
                            self.assertTrue(calibration["references_independent_of_matrix"])
                            self.assertAlmostEqual(calibration["calibration"]["sample_rate_millihz"] / 1000,
                                                   20240, delta=1.)
                            self.assertAlmostEqual(calibration["calibration"]["adc_scale"], 1.016, delta=.0002)
                            self.assertEqual(len(calibration["verification"]["independent_checks"]), 3)
                            parameters = Path(directory) / "algorithm_parameters.json"
                            profile = load_latest_calibration(parameters)
                            self.assertEqual(profile.calibration.to_dict(), calibration["calibration"])
                            self.assertEqual(Path(profile.source["calibration_report"]), report / "calibration.json")
                            for entry in entries:
                                capture = store.path / entry["capture"]
                                raw = (capture / "samples.adc.bin").read_bytes()
                                adjusted = (capture / "samples.calibrated.adc.bin").read_bytes()
                                archive = json.loads((capture / "capture.json").read_text())
                                self.assertEqual(archive["metadata"]["sample_rate_millihz"], 20000000)
                                self.assertEqual(archive["metadata"]["checksum"], host.fnv1a(raw))
                                self.assertEqual(entry["metadata"], archive["analysis_metadata"])
                                self.assertEqual(entry["metadata"]["checksum"], host.fnv1a(adjusted))
                                self.assertEqual(archive["acquisition_calibration_source"], profile.source)
                                self.assertNotEqual(raw, adjusted)
                            # A fresh competition server shares only the saved parameters.
                            # Its data folder differs, and it never connects to the generator.
                            instrument.wave, instrument.output = "SINE", "ON"
                            instrument.frequency, instrument.amp, instrument.offset = 137.5, 1.2, 1.65
                            competition_port = BiasedBoard(instrument)
                            competition_output, competition_status = io.StringIO(), io.StringIO()
                            competition_data = Path(directory) / "competition"
                            try:
                                with patch("serial.Serial", return_value=competition_port), \
                                     patch("sys.stdout", competition_output), patch("sys.stderr", competition_status), \
                                     patch("socket.create_connection", side_effect=AssertionError("R contacted generator")):
                                    self.assertEqual(main(["R", "--port", "COMPETITION", "--data-dir",
                                        str(competition_data), "--algorithm-parameters", str(parameters)]), 0,
                                        competition_status.getvalue())
                                fields = competition_output.getvalue().strip().split(",")
                                self.assertEqual(len(fields), 5)
                                self.assertEqual(fields[:2], ["RESULT", "sine"])
                                self.assertAlmostEqual(float(fields[2]), 137.5, delta=.2)
                                self.assertAlmostEqual(float(fields[3]), 1.2, delta=.02)
                                self.assertEqual(fields[4], "0")
                                competition_archive = json.loads(next(competition_data.glob(
                                    "*/uart/*/capture.json")).read_text())
                                self.assertEqual(competition_archive["analysis_metadata"]["sample_rate_millihz"],
                                                 profile.calibration.sample_rate_millihz)
                                self.assertEqual(competition_archive["acquisition_calibration_source"], profile.source)
                                self.assertAlmostEqual(competition_archive["algorithm"]["dc_offset_counts"] *
                                    3.3 / 4095, 1.65, delta=.01)
                                self.assertFalse(competition_port.is_open)
                            finally:
                                instrument.output = "OFF"
                        self.assertEqual(instrument.output, "OFF")
                finally:
                    instrument.close()

    def test_missing_connection_and_failed_independent_check_stop_before_matrix(self):
        for settings in ({"disconnected": True}, {"bad_validation": True}):
            with self.subTest(settings=settings):
                instrument = ScpiServer()
                output = io.StringIO()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        store = CaptureStore(directory)
                        service = SerialService(BiasedBoard(instrument, **settings), store,
                                                Engine(self.api, output), stderr=io.StringIO())
                        try:
                            with self.assertRaises(ValueError):
                                run(service, store, self.options(instrument), io.StringIO())
                            self.assertIsNone(service.calibration)
                        finally:
                            service.close()
                        report = next(store.path.glob("ro-*"))
                        summary = json.loads((report / "summary.json").read_text())
                        self.assertFalse(summary["complete"])
                        self.assertEqual(summary["counts"], {})
                        self.assertEqual(len(summary["not_run"]), 5)
                        self.assertEqual(output.getvalue(), "")
                        self.assertFalse(json.loads((report / "calibration.json").read_text())["complete"])
                        self.assertEqual(instrument.uploads, [])
                        self.assertEqual(instrument.output, "OFF")
                finally:
                    instrument.close()


if __name__ == "__main__":
    unittest.main()
