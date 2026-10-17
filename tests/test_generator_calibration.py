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
                                           arb_offset_gain=1.011, arb_bias_v=.004,
                                           arb_offset_curvature=-.066)
                        service = SerialService(board, store, Engine(self.api, output), stderr=io.StringIO())
                        try:
                            if enable:
                                code = run(service, store, self.options(instrument), io.StringIO())
                            else:
                                with patch("tools.ro_test.calibrate_playback"):
                                    # A clipped R block must abort the shared
                                    # request; no partial RESULT is published.
                                    with self.assertRaisesRegex(ValueError, r"R block .* rejected: status=[25]"):
                                        run(service, store, self.options(instrument), io.StringIO())
                                    code = 1
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
                            summary = json.loads((report / 'summary.json').read_text())
                            self.assertFalse(summary['complete'])
                            self.assertIn('rejected: status=', summary['error'])
                            self.assertEqual(summary['shutdown_error'], '')
                        parameters = json.loads((Path(directory) / "algorithm_parameters.json").read_text())
                        self.assertNotIn("generator_calibration", parameters)
                        self.assertAlmostEqual(parameters["acquisition_calibration"]["adc_scale"], 1.016, delta=.0002)
                        lines = output.getvalue().splitlines()
                        self.assertEqual(len(lines), sum(e["algorithm"]["ok"] for e in entries))
                        self.assertTrue(all(len(line.split(",")) == 5 and line.startswith("RESULT,") for line in lines))
                        for entry in entries:
                            if not entry["algorithm"]["ok"]:
                                archive = json.loads((store.path / entry["capture"] / "capture.json").read_text())
                                self.assertEqual(archive["display_output"], "")
                                self.assertEqual(archive["output"], "unknown,0,0,0,0")
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
        for level, offset in ((.15, .5), (.5, .5), (.85, .5), (.5, .48), (.5, .52)):
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

    def test_range_dependent_offset_is_fitted_locally_and_compensation_is_independently_verified(self):
        # Measured hardware pattern: the distant OFST midpoint is about 28.8 mV
        # below the center reference, while the three fixed-OFST levels are linear.
        amplitude_gain, offset_gain, center, curvature = 1.033, .97138, 1.67163, -.066
        bias = center - offset_gain * 1.65

        def capture(data, config, bend=curvature):
            words = np.resize(np.frombuffer(data, dtype="<i2").astype(float), 4096)
            volts = (amplitude_gain * config["amplitude_v"] / 2 * words / 32767 +
                     offset_gain * config["offset_v"] + bias + bend * (config["offset_v"] - 1.65) ** 2)
            return np.rint(volts / 3.3 * 4095).astype("<u2").tobytes()

        def observations(offsets, bend=curvature):
            result = []
            for level, offset in ((.15, .5), (.5, .5), (.85, .5), (.5, offsets[0]), (.5, offsets[1])):
                data, config, expected = reference(3.3, level, offset_v=offset * 3.3)
                result.append({"payload": capture(data, config, bend), "config": config, "reference": expected})
            return result

        distant = observations((.3, .7))
        design = np.array([[e["reference"]["dc_v"] - e["config"]["offset_v"],
                            e["config"]["offset_v"], 1.] for e in distant])
        measured = np.array([np.frombuffer(e["payload"], dtype="<u2").mean() * 3.3 / 4095 for e in distant])
        global_fit = np.linalg.lstsq(design, measured, rcond=None)[0]
        self.assertGreater(np.max(np.abs(design @ global_fit - measured)), .015)
        with self.assertRaisesRegex(ValueError, "working point locally"):
            fit_playback(distant, 3.3)
        local, info = fit_playback(observations((.48, .52)), 3.3)
        self.assertLess(info["dc_fit_max_error_v"], .001)
        self.assertAlmostEqual(local.amplitude_gain, amplitude_gain, delta=.001)
        self.assertAlmostEqual(local.offset_gain, offset_gain, delta=.01)
        self.assertAlmostEqual(local.offset_origin_v, 1.65)
        self.assertAlmostEqual(local.offset_span_v, .066)
        for level in (.3, .7):
            data, config, expected = reference(3.3, level)
            corrected = local.apply(config)
            result = verify_playback(capture(data, corrected), META, expected)
            self.assertLess(result["dc_error_v"], .001)
        # Actual nonlinearity within the local window is still rejected at the
        # original 6.6 mV limit, rather than being hidden by changing that limit.
        with self.assertRaisesRegex(ValueError, "maximum fit error.*6.600 mV"):
            fit_playback(observations((.48, .52), bend=-20.), 3.3)

    def test_playback_cannot_extrapolate_beyond_its_calibrated_offset_range(self):
        profile = GeneratorCalibration(1., 1., .08, offset_origin_v=1.65, offset_span_v=.066)
        config = {"amplitude_v": 3.3, "offset_v": 1.65}
        with self.assertRaisesRegex(ValueError, "exceeds the calibrated working point"):
            profile.apply(config)
        self.assertEqual(config, {"amplitude_v": 3.3, "offset_v": 1.65})

    def test_display_all_wave_classes_uses_contest_protocol_and_keeps_diagnostics(self):
        for wave in ("sine", "square", "triangle", "sawtooth"):
            with self.subTest(wave=wave), tempfile.TemporaryDirectory() as directory:
                payload, meta, _ = generate(Case(wave=wave, duty=.3))
                store, output = CaptureStore(directory), io.StringIO()
                archive = store.begin("test")
                line, details, _ = Engine(self.api, output).process(payload, meta, archive)
                fields = output.getvalue().strip().split(",")
                self.assertEqual(fields[:2], ["RESULT", wave])
                self.assertEqual(len(fields), 5)
                self.assertEqual(fields[2:4], line.split(",")[1:3])
                for number in fields[2:]:
                    self.assertRegex(number, r"^\d+(?:\.\d+)?$")
                if wave == "square":
                    self.assertAlmostEqual(float(fields[4]), 30., delta=2.5)
                else:
                    self.assertEqual(fields[4], "0")
                self.assertIn("dc_offset_counts", details)
                self.assertEqual(archive.info["display_output"], output.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
