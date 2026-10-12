"""Source-offset semantics and blind total-envelope measurement regressions."""
import ctypes
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import host_waveform as host
from tools.self_test import Case, generate, matrix


class EnvelopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT/host.LIBRARY_NAME))

    def measure(self, case):
        payload, metadata, facts = generate(case)
        # Historical envelope/distortion semantics remain covered explicitly.
        # New R measures the basic pre-noise carrier; see test_unified_solver.
        line, result = host.analyze_legacy(self.api, payload, metadata)
        self.assertTrue(result["ok"], (case, line))
        self.assertEqual(result["type"], host.NAMES.index(case.wave))
        self.assertEqual(facts["truth"]["dc_v"], case.dc_v)
        return result, facts, payload, metadata

    def test_offset_truth_is_setting_for_asymmetric_distortion_and_non_50_square(self):
        for case in (Case(clip_top=.2), Case(clip_bottom=.15),
                     Case(harmonic_2=.07, harmonic_3=.06),
                     Case(wave="square", duty=.2, am_depth=.1, am_rate_hz=11.7),
                     Case(wave="square", duty=.8, interferer_ratio=.08,
                          interferer_frequency_ratio=5.),
                     Case(wave="square", duty=.35, inverted=True,
                          interferer_ratio=.08, interferer_frequency_ratio=1.73)):
            with self.subTest(case=case):
                result, facts, _, _ = self.measure(case)
                volts = result["dc_offset_counts"]*case.vref_v/(4095*case.gain)
                self.assertLess(abs(volts-case.dc_v), .02)
                self.assertEqual(result["envelope_measurement"]["dc_definition"],
                                 facts["truth"]["dc_definition"])

    def test_unseen_distortion_parameters_use_total_clean_vpp(self):
        # Frequencies, phase, gain, reference, modulation and interferer ratios
        # differ from the default RT; the estimator receives none of these labels.
        base = Case(frequency_hz=227.41, phase=.619, vpp_v=1.2, dc_v=1.7,
                    fs_hz=23817., vref_v=3.17, gain=.83, noise_pp_ratio=.04,
                    seed=98217)
        cases = [replace(base, clip_top=.13), replace(base, clip_bottom=.17),
                 replace(base, clip_top=.1, clip_bottom=.1),
                 replace(base, crossover=.09),
                 replace(base, harmonic_2=.065, harmonic_3=.045)]
        for wave in ("sine", "square", "triangle", "sawtooth"):
            cases.extend((replace(base, wave=wave, duty=.63, rise=.03, fall=.07,
                                   am_depth=.11, am_rate_hz=17.3),
                          replace(base, wave=wave, duty=.63, rise=.03, fall=.07,
                                   interferer_ratio=.075, interferer_frequency_ratio=1.37),
                          replace(base, wave=wave, duty=.63, rise=.03, fall=.07,
                                   interferer_ratio=.075, interferer_frequency_ratio=5.)))
        for case in cases:
            with self.subTest(wave=case.wave, am=case.am_depth,
                              interferer=case.interferer_frequency_ratio,
                              clip=(case.clip_top, case.clip_bottom)):
                result, _, _, _ = self.measure(case)
                self.assertLess(abs(result["vpp_microvolts"]/(case.vpp_v*1e6)-1), .04)
                self.assertIn("carrier_result", result)
                self.assertTrue(result["quality_flags"] & (1 << 7))

    def test_periodic_noise_is_not_promoted_to_am_or_a_coherent_tone(self):
        for wave in ("sine", "square", "triangle", "sawtooth"):
            for seed in (17, 131):
                case = Case(wave=wave, frequency_hz=237.19, noise_pp_ratio=.30,
                            bandwidth_ratio=5.25, seed=seed)
                result, _, _, _ = self.measure(case)
                self.assertEqual(result["envelope_measurement"]["model"], "periodic")
                self.assertLess(abs(result["vpp_microvolts"]/1.5e6-1), .04)

    def test_weak_am_survives_strong_noise_when_both_halves_are_coherent(self):
        case = next(c for c in matrix() if c.id == "competition-000272")
        result, _, _, _ = self.measure(case)
        self.assertEqual(result["envelope_measurement"]["model"], "am")
        self.assertLess(abs(result["vpp_microvolts"]/(case.vpp_v*1e6)-1), .03)

    def test_cascade_keeps_l1_timing_when_l2_misclassifies_the_shape(self):
        case = next(c for c in matrix() if c.id == "competition-000295")
        result, _, payload, metadata = self.measure(case)
        samples = (ctypes.c_uint16*host.SAMPLES).from_buffer_copy(payload)
        frame = host.Frame(samples, host.SAMPLES, metadata.adc_full_scale,
            metadata.sample_rate_millihz, metadata.reference_microvolts,
            metadata.gain_ppm, metadata.sequence, metadata.flags)
        stage = host.Result()
        self.api.ti_waveform_analyze_frame_with_algorithm.argtypes = [
            ctypes.POINTER(host.Frame), ctypes.POINTER(host.Result), ctypes.c_int]
        self.api.ti_waveform_analyze_frame_with_algorithm.restype = ctypes.c_bool
        self.assertTrue(self.api.ti_waveform_analyze_frame_with_algorithm(
            ctypes.byref(frame), ctypes.byref(stage), 3))
        self.assertEqual(result["c_timing_result"]["frequency_millihz"], stage.frequency_millihz)
        self.assertLess(abs(result["c_timing_result"]["frequency_millihz"]/1000/case.frequency_hz-1), .0002)
        self.assertTrue(result["quality_flags"] & (1 << 6))


if __name__ == "__main__":
    unittest.main()
