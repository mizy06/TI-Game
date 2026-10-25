"""Persistent competition R parameters: newest successful RO, recovery and provenance."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import host_waveform as host
from tools.acquisition_calibration import AcquisitionCalibration, load_latest_calibration, save_latest_calibration
from tools.capture_store import CaptureStore, atomic_json
from tools.ro_test import run
from tools.waveform_server import Engine, SerialService, parser
from test_acquisition_calibration import BiasedBoard
from test_host_integration import response, sine_payload
from test_ro import ScpiServer
from test_server import LiveSerial


class ParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    @staticmethod
    def calibration(rate=20240000):
        return AcquisitionCalibration(rate, 1.016, -33., {
            "sample_rate_millihz": 20000000, "reference_microvolts": 3300000,
            "gain_ppm": 1000000, "adc_full_scale": 4095})

    def report(self, root, name, rate=20240000):
        path = root / name / "calibration.json"
        path.parent.mkdir()
        atomic_json(path, {"complete": True, "error": "", "references_independent_of_matrix": True,
            "calibration": self.calibration(rate).to_dict(),
            "verification": {"independent_checks": [{"reference": {"wave": "dc"}, "dc_error_v": 0.},
                {"reference": {"wave": "dc"}, "dc_error_v": 0.},
                {"reference": {"wave": "sine"}, "sample_rate_error_fraction": 0.}]}})
        return path

    def options(self, instrument, mode="auto"):
        return parser().parse_args(["RO", "--ro-host", "127.0.0.1", "--ro-port", str(instrument.port),
                                   "--ro-transport", "vxi11", "--no-ro-vpp-calibrate",
                                   "--ro-oversample", "1", "--r-frames", "1",
                                   "--ro-settle", "0", "--ro-calibrate", mode, "--max-cases", "1"])

    def test_live_r_loads_latest_success_and_preserves_old_file_on_rejection_or_write_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters = root / "algorithm_parameters.json"
            first = self.report(root, "ro-first")
            initial = save_latest_calibration(parameters, first)
            store, output = CaptureStore(root / "captures"), io.StringIO()
            service = SerialService(LiveSerial(response(sine_payload())), store, Engine(self.api, output),
                                    stderr=io.StringIO(), parameters_path=parameters)
            try:
                _, _, _, first_meta, _ = service.measure(3, return_result=True)
                self.assertEqual(first_meta.sample_rate_millihz, 20240000)
                latest = self.report(root, "ro-latest", 20260000)
                updated = save_latest_calibration(parameters, latest)
                _, _, _, latest_meta, archive = service.measure(3, return_result=True)
                self.assertEqual(latest_meta.sample_rate_millihz, 20260000)
                self.assertEqual(archive.info["acquisition_calibration_source"], updated.source)
                self.assertNotEqual(initial.source, updated.source)
                saved_bytes = parameters.read_bytes()
                for changes in ({"complete": False}, {"error": "independent check failed"},
                                {"references_independent_of_matrix": False}, {"verification": {}}):
                    bad = json.loads(latest.read_text())
                    bad.update(changes)
                    atomic_json(latest, bad)
                    with self.assertRaisesRegex(ValueError, "successful independent RO"):
                        save_latest_calibration(parameters, latest)
                    self.assertEqual(parameters.read_bytes(), saved_bytes)
                candidate = self.report(root, "ro-write-error", 20280000)
                with patch("tools.capture_store.os.replace", side_effect=OSError("disk unavailable")):
                    with self.assertRaisesRegex(OSError, "disk unavailable"):
                        save_latest_calibration(parameters, candidate)
                self.assertEqual(parameters.read_bytes(), saved_bytes)
                self.assertEqual(list(parameters.parent.glob("*.tmp")), [])
                self.assertEqual(load_latest_calibration(parameters), updated)
                self.assertEqual(len(output.getvalue().splitlines()), 2)
            finally:
                service.close()

    def test_corrupt_parameters_and_metadata_mismatch_stop_r_but_raw_capture_can_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parameters = root / "algorithm_parameters.json"
            self.assertIsNone(load_latest_calibration(parameters))
            atomic_json(parameters, {"schema": 1, "acquisition_calibration": None, "calibration_source": None})
            self.assertIsNone(load_latest_calibration(parameters))
            report = self.report(root, "ro-valid")
            valid = save_latest_calibration(parameters, report)
            original = parameters.read_bytes()
            damaged = json.loads(original)
            damaged["acquisition_calibration"]["adc_scale"] = 1.03
            atomic_json(parameters, damaged)
            store, output = CaptureStore(root / "captures"), io.StringIO()
            port = LiveSerial(response(sine_payload()))
            service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO(),
                                    parameters_path=parameters)
            try:
                with self.assertRaisesRegex(ValueError, "checksum differs"):
                    service.measure(3)
                self.assertEqual(output.getvalue(), "")
                raw, _, archive = service.capture(3)
                self.assertEqual(raw, sine_payload())
                self.assertNotIn("analysis_metadata", archive.info)
                service.publish_calibration(report)
                port.reply = response(sine_payload(), vref=3000000)
                with self.assertRaisesRegex(ValueError, "metadata changed"):
                    service.measure(3)
                self.assertEqual(output.getvalue(), "")
                archive = json.loads(sorted(store.path.glob("uart/*/capture.json"))[-1].read_text())
                self.assertIn("metadata changed", archive["calibration_error"])
                self.assertEqual(archive["metadata"]["reference_microvolts"], 3000000)
                self.assertNotIn("algorithm", archive)
                self.assertEqual(service.calibration, valid.calibration)
                self.assertEqual(len(list(store.path.glob("uart/*/samples.adc.bin"))), 3)
            finally:
                service.close()

    def test_failed_ro_and_nominal_diagnostic_run_keep_saved_competition_calibration(self):
        instrument = ScpiServer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                parameters = root / "algorithm_parameters.json"
                prior = save_latest_calibration(parameters, self.report(root, "ro-prior"))
                saved_bytes = parameters.read_bytes()
                store, output = CaptureStore(root), io.StringIO()
                port = BiasedBoard(instrument, bad_validation=True)
                service = SerialService(port, store, Engine(self.api, output), stderr=io.StringIO())
                try:
                    with self.assertRaisesRegex(ValueError, "independent DC"):
                        run(service, store, self.options(instrument), io.StringIO())
                    self.assertEqual(parameters.read_bytes(), saved_bytes)
                    self.assertEqual(service.calibration_profile, prior)
                    self.assertTrue(service.use_saved_calibration)
                    self.assertEqual(output.getvalue(), "")
                    port.bad_validation = False
                    self.assertEqual(run(service, store, self.options(instrument, "off"), io.StringIO()), 1)
                    self.assertEqual(parameters.read_bytes(), saved_bytes)
                    self.assertTrue(service.use_saved_calibration)
                    self.assertEqual(service.calibration_profile, prior)
                    instrument.wave, instrument.output = "SINE", "ON"
                    instrument.frequency, instrument.amp, instrument.offset = 100., .99, 1.65
                    _, _, _, meta, archive = service.measure(3, return_result=True)
                    self.assertEqual(meta.sample_rate_millihz, prior.calibration.sample_rate_millihz)
                    self.assertEqual(archive.info["acquisition_calibration_source"], prior.source)
                    self.assertEqual(len(output.getvalue().splitlines()), 2)
                finally:
                    service.close()
                    instrument.output = "OFF"
        finally:
            instrument.close()

    def test_verified_ro_updates_parameters_even_if_the_subsequent_matrix_is_interrupted(self):
        instrument = ScpiServer()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                parameters = root / "algorithm_parameters.json"
                prior = save_latest_calibration(parameters, self.report(root, "ro-prior", 20260000))
                store, output = CaptureStore(root), io.StringIO()
                service = SerialService(BiasedBoard(instrument), store, Engine(self.api, output), stderr=io.StringIO())
                try:
                    with patch("tools.ro_test._run_cases", side_effect=RuntimeError("matrix interrupted")):
                        with self.assertRaisesRegex(RuntimeError, "matrix interrupted"):
                            run(service, store, self.options(instrument), io.StringIO())
                    saved = load_latest_calibration(parameters)
                    self.assertNotEqual(saved, prior)
                    self.assertAlmostEqual(saved.calibration.sample_rate_millihz / 1000, 20240., delta=1.)
                    self.assertEqual(service.calibration_profile, saved)
                    report = next(store.path.glob("ro-*"))
                    self.assertEqual(Path(saved.source["calibration_report"]), report / "calibration.json")
                    self.assertTrue(json.loads((report / "calibration.json").read_text())["complete"])
                    self.assertFalse(json.loads((report / "summary.json").read_text())["complete"])
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(instrument.output, "OFF")
                finally:
                    service.close()
        finally:
            instrument.close()


if __name__ == "__main__":
    unittest.main()
