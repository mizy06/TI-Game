"""ARB-specific correction, independent of persistent R calibration and matrix truth."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tools import host_waveform as host
from tools.capture_store import CaptureStore
from tools.generator_calibration import GeneratorCalibration, reference, fit_playback, verify_playback
from tools.ro_test import run
from tools.self_test import Case, generate
from tools.waveform_server import Engine, SerialService, parser
from test_acquisition_calibration import BiasedBoard, META
from test_ro import ScpiServer


class PlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def options(self, instrument):
        return parser().parse_args(["RO", "--ro-host", "127.0.0.1", "--ro-port", str(instrument.port),
            "--ro-settle", "0", "--case-id", "competition-000001", "--case-id", "competition-000002",
            "--case-id", "competition-000008", "--case-id", "competition-000051", "--case-id", "competition-000101",
            "--case-id", "competition-000151"])

    def test_builtin_adc_calibration_alone_leaves_arb_clipping_and_dc_error_but_playback_calibration_fixes_it(self):
        for enable in (False, True):
            with self.subTest(playback_calibration=enable):
                instrument = ScpiServer()
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        store, output = CaptureStore(directory), io.StringIO()
                        # Both command gains differ, so merely dividing AMP/OFST
                        # by one fitted voltage slope would give the wrong result.
                        board = BiasedBoard(instrument, arb_amplitude_gain=1.028,
                                           arb_offset_gain=1.011, arb_bias_v=.004)
                        service = SerialService(board, store, Engine(self.api, output), stderr=io.StringIO())
                        try:
                            if enable:
                                code = run(service, store, self.options(instrument), io.StringIO())
                            else:
                                with patch("tools.ro_test.calibrate_playback"):
                                    code = run(service, store, self.options(instrument), io.StringIO())
                        finally:
                            service.close()
                        report = next(store.path.glob("ro-*"))
                        entries = json.loads((report / "results.json").read_text())["results"]
                        reasons = {r for e in entries for r in e["result"]["failure_reasons"]}
                        if enable:
                            self.assertEqual(code, 0, json.dumps(entries, indent=2))
                            playback = json.loads((report / "generator_calibration.json").read_text())
                            self.assertTrue(playback["complete"])
                            self.assertAlmostEqual(playback["calibration"]["amplitude_gain"], 1.028, delta=.001)
                            self.assertAlmostEqual(playback["calibration"]["offset_gain"], 1.011, delta=.001)
                            self.assertEqual(len(playback["verification"]["independent_checks"]), 3)
                            for entry in entries:
                                self.assertTrue(entry["result"]["passed"])
                                self.assertEqual(entry["generator"]["truth"], entry["result"]["truth"])
                                self.assertEqual(entry["generator"]["requested_amplitude_v"], 3.3)
                                self.assertLess(entry["generator"]["amplitude_v"], 3.3)
                                raw = np.frombuffer((store.path / entry["capture"] / "samples.adc.bin").read_bytes(), dtype="<u2")
                                self.assertFalse(np.any((raw == 0) | (raw == 4095)))
                        else:
                            self.assertEqual(code, 1)
                            self.assertIn("dc_error_v", reasons)
                            self.assertIn("algorithm_status_2", reasons)
                        parameters = json.loads((Path(directory) / "algorithm_parameters.json").read_text())
                        self.assertNotIn("generator_calibration", parameters)
                        self.assertAlmostEqual(parameters["acquisition_calibration"]["adc_scale"], 1.016, delta=.0002)
                        lines = output.getvalue().splitlines()
                        self.assertEqual(len(lines), 6)
                        self.assertTrue(all(len(line.split(",")) == (4 if line.startswith("square,") else 3) for line in lines))
                        self.assertEqual(instrument.output, "OFF")
                finally:
                    instrument.close()

    def test_playback_validation_failure_stops_before_matrix_and_preserves_successful_adc_parameters(self):
        instrument = ScpiServer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store, output = CaptureStore(directory), io.StringIO()
                service = SerialService(BiasedBoard(instrument), store, Engine(self.api, output), stderr=io.StringIO())
                try:
                    with patch("tools.generator_calibration.verify_playback", side_effect=ValueError("ARB check failed")):
                        with self.assertRaisesRegex(ValueError, "ARB check failed"):
                            run(service, store, self.options(instrument), io.StringIO())
                    self.assertIsNotNone(service.calibration)
                finally:
                    service.close()
                report = next(store.path.glob("ro-*"))
                self.assertFalse(json.loads((report / "generator_calibration.json").read_text())["complete"])
                self.assertTrue(json.loads((report / "calibration.json").read_text())["complete"])
                self.assertTrue((Path(directory) / "algorithm_parameters.json").exists())
                self.assertEqual(json.loads((report / "summary.json").read_text())["counts"], {})
                self.assertEqual(output.getvalue(), "")
                self.assertFalse(list(report.glob("competition-*")))
                self.assertEqual(instrument.output, "OFF")
        finally:
            instrument.close()

    def test_reference_fit_rejects_clipped_or_flat_responses_and_validation_is_independent(self):
        observations = []
        for level, offset in ((.15, .5), (.5, .5), (.85, .5), (.5, .3), (.5, .7)):
            data, config, expected = reference(3.3, level, offset_v=offset * 3.3)
            codes = np.resize(np.rint((config["offset_v"] + config["amplitude_v"] / 2 *
                np.frombuffer(data, dtype="<i2") / 32767) * 4095 / 3.3).astype("<u2"), 4096).tobytes()
            observations.append({"payload": codes, "config": config, "reference": expected})
        fitted, _ = fit_playback(observations, 3.3)
        self.assertAlmostEqual(fitted.amplitude_gain, 1., delta=.001)
        for payload in (np.zeros(4096, dtype="<u2").tobytes(), np.full(4096, 2048, dtype="<u2").tobytes()):
            with self.assertRaises(ValueError):
                fit_playback([{**item, "payload": payload} for item in observations], 3.3)
        with self.assertRaisesRegex(ValueError, "DC check failed"):
            verify_playback(np.full(4096, 2048, dtype="<u2").tobytes(), META, {"wave": "dc", "dc_v": .99})

    def test_display_all_wave_classes_has_duty_only_for_square_and_keeps_diagnostics(self):
        for wave in ("sine", "square", "triangle", "sawtooth"):
            with self.subTest(wave=wave), tempfile.TemporaryDirectory() as directory:
                payload, meta, _ = generate(Case(wave=wave, duty=.3))
                store, output = CaptureStore(directory), io.StringIO()
                archive = store.begin("test")
                line, details, _ = Engine(self.api, output).process(payload, meta, archive)
                fields = output.getvalue().strip().split(",")
                self.assertEqual(fields[0], wave)
                self.assertEqual(len(fields), 4 if wave == "square" else 3)
                self.assertEqual(fields, line.split(",")[:len(fields)])
                if wave == "square":
                    self.assertAlmostEqual(float(fields[3]), 30., delta=2.5)
                self.assertIn("dc_offset_counts", details)
                self.assertEqual(archive.info["display_output"], output.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
