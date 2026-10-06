"""Numerical bias, true challenge inputs, and durable RT verdict regressions."""
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import host_waveform as host
from tools.capture_store import CaptureStore
from tools.competition_stress_suite import challenge_cases, gate_cases
from tools.self_test import Case, evaluate, generate, matrix, run
from tools.waveform_server import Engine
from tools.verify_rt_archive import verify


class RepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def test_saw_ramp_amplitude_does_not_shrink_with_frequency_or_phase(self):
        for frequency in (100., 173.3, 200., 250., 333.333, 400.):
            for phase in (0., .173, .25, .4, .618, .83):
                case = Case(wave="sawtooth", frequency_hz=frequency,
                            vpp_v=1.5, dc_v=1.65, phase=phase)
                payload, meta, _ = generate(case)
                _, result = host.analyze(self.api, payload, meta)
                with self.subTest(frequency=frequency, phase=phase):
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["type"], 3)
                    self.assertLess(abs(result["vpp_microvolts"]/1.5e6-1), .003)
                    self.assertTrue(result["quality_flags"] & (1 << 5))

    def test_challenge_and_inl_parameters_survive_matrix_conversion(self):
        old = gate_cases(20261005, 50) + challenge_cases(20261005, 12)
        new = list(matrix())
        fields = ("inl_lsb", "harmonic_2", "harmonic_3", "clip_top", "clip_bottom",
                  "crossover", "am_depth", "am_rate_hz", "interferer_ratio",
                  "interferer_frequency_ratio", "source", "impairment")
        for before, after in zip(old, new):
            for name in fields:
                self.assertEqual(getattr(before, name), getattr(after, name))
            self.assertEqual(after.expectation, "measure")
        self.assertEqual(len(new), 296)
        self.assertEqual(len({c.impairment for c in new[200:]}), 8)

    def test_harmonic_vpp_truth_is_for_the_distorted_clean_signal(self):
        case = Case(frequency_hz=173.3, vpp_v=1.5, dc_v=1.65,
                    harmonic_ratio=.06)
        payload, meta, facts = generate(case)
        voltage = np.frombuffer(payload, dtype="<u2").astype(float)*meta.reference_microvolts/1e6/4095
        self.assertAlmostEqual(float(np.ptp(voltage)), 1.5, delta=.002)
        self.assertEqual(facts["truth"]["vpp_v"], 1.5)
        self.assertAlmostEqual(facts["truth"]["dc_v"], 1.65, delta=.0001)

    def test_evaluation_uses_unrounded_internal_amplitude(self):
        case = Case(frequency_hz=100, vpp_v=.08)
        details = {"frequency_millihz": 100000, "vpp_microvolts": 80030,
                   "duty_ppm": 0, "dc_offset_counts": 2048, "ok": True, "status": 0}
        check = evaluate(case, "sine,100.000,0.080,0.0,1.650", details, .01)
        self.assertAlmostEqual(check["vpp_bias_pct"], .0375)
        self.assertIs(check["passed"], True)
        failed = evaluate(replace(case, group="final_strong"),
                          "unknown,0,0,0,0", {**details, "ok": False, "status": 7}, .01)
        self.assertIs(failed["passed"], False)
        self.assertEqual(failed["verdict"], "FAIL")

    def test_exception_keeps_all_cases_and_complete_raw_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            engine = Engine(self.api, io.StringIO())
            calls = []

            def process(payload, meta, record):
                calls.append(meta.sequence)
                if len(calls) == 2:
                    raise RuntimeError("injected algorithm exception")
                return engine.process(payload, meta, record)

            code = run(self.api, store, process, max_cases=3, stderr=io.StringIO())
            self.assertEqual(code, 1)
            report = next(store.path.glob("rt-*"))
            summary = json.loads((report/"summary.json").read_text())
            self.assertTrue(summary["complete"])
            self.assertFalse(summary["all_passed"])
            self.assertEqual(summary["counts"], {"passed": 2, "failed": 1,
                                                 "errors": 1, "completed": 3})
            results = json.loads((report/"results.json").read_text())["results"]
            self.assertEqual([r["result"]["verdict"] for r in results], ["PASS", "ERROR", "PASS"])
            self.assertEqual(len((report/"results.jsonl").read_text().splitlines()), 3)
            with (report/"results.csv").open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 3)
            self.assertEqual(len(summary["failures"]), 1)
            self.assertEqual(verify(report), {"records": 3, "passed": 2, "failed": 1,
                                             "payloads_and_checksums_verified": True})
            for entry in results:
                capture = store.path/entry["capture"]
                self.assertEqual((capture/"samples.adc.bin").stat().st_size, 8192)
                self.assertIsInstance(json.loads((capture/"capture.json").read_text())
                                      ["self_test"]["passed"], bool)

    def test_interrupted_run_preserves_finished_results_and_not_run_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            engine = Engine(self.api, io.StringIO())
            stop = threading.Event()

            def process(payload, meta, record):
                result = engine.process(payload, meta, record)
                stop.set()
                return result

            self.assertEqual(run(self.api, store, process, max_cases=3,
                                 stop_event=stop, stderr=io.StringIO()), 1)
            report = next(store.path.glob("rt-*"))
            summary = json.loads((report/"summary.json").read_text())
            self.assertFalse(summary["complete"])
            self.assertEqual(summary["not_run"], ["competition-000002", "competition-000003"])
            self.assertEqual(len(json.loads((report/"results.json").read_text())["results"]), 1)


if __name__ == "__main__":
    unittest.main()
