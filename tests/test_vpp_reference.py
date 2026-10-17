"""Independent reference, calibration separation, integrity and Vpp-only tests."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.signal import lfilter
from tools import host_waveform as host
from tools.vpp_reference import (ReferenceCalibration, _calibrate_fir, calibrate,
    estimate_reference)
from tools.vpp_reference_io import read_npz, MEASUREMENT
from vpp_reference_fixture import (FS, NOISE_PATH, SIGNAL_PATH, adc,
    make_calibration, measurement, signal)


class ReferenceVppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calibration = make_calibration()

    def test_independent_calibration_recovers_unknown_paths_and_roundtrips(self):
        c = self.calibration
        np.testing.assert_allclose(c.noise_taps, NOISE_PATH, atol=5e-5)
        np.testing.assert_allclose(c.signal_taps, SIGNAL_PATH, atol=5e-5)
        restored = ReferenceCalibration.from_dict(json.loads(json.dumps(c.to_dict())))
        self.assertEqual(restored.sha256, c.sha256)
        for path in ('noise', 'signal'):
            self.assertNotEqual(c.evidence[path]['training_sha256'], c.evidence[path]['validation_sha256'])
        self.assertTrue(c.evidence['no_source_on_adaptation'])

    def test_coherent_interference_all_wave_families_and_distorted_envelopes(self):
        for kind in range(4):
            for distortion in (0., .07):
                with self.subTest(kind=kind, distortion=distortion):
                    y, o, truth = measurement(kind, distortion=distortion)
                    fit = estimate_reference(y, FS, o, self.calibration)
                    self.assertEqual(fit['type'], kind)
                    self.assertLess(100*abs(fit['vpp_v']/truth-1), .1)
                    self.assertFalse(fit['source_on_adaptation'])
                    self.assertFalse(fit['accuracy_0p1_percent_certified'])

    def test_identical_main_bytes_are_disambiguated_by_distinct_references(self):
        n = 8192
        s = np.sin(2*np.pi*(237.193*np.arange(n)/FS+.173))
        extra = .1*s+.03*np.sin(2*np.pi*419.317*np.arange(n)/FS)
        source_a, source_b = .75*s, .753*s
        # Keep the PRIMARY ADC exactly identical. Only the physically measured
        # reference changes between the two previously indistinguishable worlds.
        noise_b = extra-lfilter([1.], NOISE_PATH,
            lfilter(SIGNAL_PATH, [1.], source_b-source_a))
        analog = 1.65+lfilter(SIGNAL_PATH, [1.], source_a)+lfilter(NOISE_PATH, [1.], extra)
        other = 1.65+lfilter(SIGNAL_PATH, [1.], source_b)+lfilter(NOISE_PATH, [1.], noise_b)
        primary = adc(analog, 'main')
        np.testing.assert_array_equal(primary, adc(other, 'main'))
        _, o, _ = measurement(0)
        estimates = []
        for noise, truth in ((extra, 1.5), (noise_b, 1.506)):
            obs = replace(o, reference_codes=adc(1.6+noise, 'reference'))
            r = estimate_reference(primary, FS, obs, self.calibration)
            self.assertLess(100*abs(r['vpp_v']/truth-1), .1)
            estimates.append(r['vpp_v'])
        self.assertGreater(estimates[1]-estimates[0], .005)

    def test_unseen_hardware_paths_are_learned_instead_of_hardcoded(self):
        rng = np.random.default_rng(914)
        noise_taps = np.array([-.21, .62, .1])
        signal_taps = np.array([.76, .18, .06])
        x, z = rng.uniform(-.6, .6, (2, 16000))
        c = calibrate(sample_rate_hz=FS, main_adc=self.calibration.main_adc,
            reference_adc=self.calibration.reference_adc,
            noise_main_codes=adc(1.6+lfilter(noise_taps, [1.], x), 'main'),
            noise_reference_codes=adc(1.6+x, 'reference'),
            standard_main_codes=adc(1.6+lfilter(signal_taps, [1.], z), 'main'),
            standard_input_v=z, main_channel='main', reference_channel='reference',
            frontend_id='unseen-hardware-B', clock_id='shared-simulator-clock',
            source_off_evidence='independent source-off experiment B',
            standard_evidence='independent precision digitizer B')
        t = np.arange(8192)/FS
        s = np.sin(2*np.pi*318.771*t+.93)
        ref = .27*s+.03*np.cos(2*np.pi*179.771*t)
        primary = adc(1.6+lfilter(signal_taps, [1.], .419*s)+lfilter(noise_taps, [1.], ref), 'main')
        _, o, _ = measurement(0)
        o = replace(o, frontend_id='unseen-hardware-B', reference_codes=adc(1.6+ref, 'reference'))
        result = estimate_reference(primary, FS, o, c)
        self.assertLess(100*abs(result['vpp_v']/.838-1), .1)

    def test_inverted_sawtooth_fits_signed_observations(self):
        t = np.arange(8192)/FS
        phase = 237.193*t+.619
        source = -.7585*signal(3, phase)
        noise = .2*np.sin(2*np.pi*phase+.37)+.01*np.cos(2*np.pi*319.31*t)
        y = adc(1.65+lfilter(SIGNAL_PATH, [1.], source)+lfilter(NOISE_PATH, [1.], noise), 'main')
        _, o, _ = measurement(0)
        o = replace(o, reference_codes=adc(1.6+noise, 'reference'))
        result = estimate_reference(y, FS, o, self.calibration)
        self.assertEqual(result['type'], 3)
        self.assertLess(100*abs(result['vpp_v']/1.517-1), .1)

    def test_host_extension_changes_only_vpp_and_its_diagnostics(self):
        y, o, truth = measurement(0)
        payload = y.tobytes()
        meta = host.Metadata(9, round(FS*1000), 3300000, 1000000, 4095, 7,
            host.fnv1a(payload), sample_rate_hz_exact=FS,
            payload_bytes=len(payload), protocol_version=3)
        line0, before = host.analyze(None, payload, meta)
        line1, after = host.analyze(None, payload, meta,
            vpp_observation=o, vpp_calibration=self.calibration)
        for key in before:
            if key in ('vpp_v', 'vpp_microvolts', 'vpp_millivolts', 'vpp_counts_q16', 'unified_solver'):
                continue
            self.assertEqual(before[key], after[key], key)
        # Solver diagnostics contain elapsed wall time; all its other fields
        # must be byte-for-byte equivalent under the unchanged original path.
        for key in before['unified_solver']:
            if key != 'elapsed_s':
                self.assertEqual(before['unified_solver'][key], after['unified_solver'][key], key)
        self.assertEqual(line0.split(',')[:2], line1.split(',')[:2])
        self.assertEqual(line0.split(',')[3:], line1.split(',')[3:])
        self.assertLess(100*abs(after['vpp_v']/truth-1), .1)
        with self.assertRaises(ValueError):
            host.analyze(None, payload, meta, vpp_observation=o)

    def test_missing_unsynchronized_untrusted_or_wrong_profile_rejected(self):
        y, o, _ = measurement(0)
        observations = [replace(o, reference_start_tick=o.main_start_tick+1),
            replace(o, sample_rate_hz=FS*1.000001), replace(o, clock_id='other-clock'),
            replace(o, main_channel='other-channel'), replace(o, frontend_id='other-device'),
            replace(o, reference_is_source_free=False), replace(o, reference_is_source_free='false'),
            replace(o, reference_codes=o.reference_codes[:-1])]
        for obs in observations:
            with self.subTest(observation=repr(obs)[:70]):
                with self.assertRaises(ValueError):
                    estimate_reference(y, FS, obs, self.calibration)

    def test_saturation_invalid_codes_and_out_of_range_are_not_reconstructed(self):
        y, o, _ = measurement(0)
        for value in (0, 4095, 4096, -1, np.nan, .5):
            bad = y.astype(float)
            bad[123] = value
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    estimate_reference(bad, FS, o, self.calibration)
                with self.assertRaises(ValueError):
                    estimate_reference(y, FS, replace(o, reference_codes=bad), self.calibration)

    def test_calibration_drift_and_insufficient_excitation_rejected_on_holdout(self):
        rng = np.random.default_rng(731)
        x = rng.normal(0, .2, 4096)
        y = .7*x
        y[len(x)*3//5:] *= 1.1
        with self.assertRaisesRegex(ValueError, 'held-out'):
            _calibrate_fir(x, y, (1, 3, 5), .002)
        with self.assertRaisesRegex(ValueError, 'excitation'):
            _calibrate_fir(np.ones(4096), y, (1, 3, 5), .002)

    def test_runtime_npz_rejects_truth_case_settings_or_missing_observation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'input.npz'
            np.savez(path, main_codes=np.ones(8192), truth_vpp_v=1.5, case_id='test')
            with self.assertRaisesRegex(ValueError, 'field mismatch'):
                read_npz(path, MEASUREMENT)

    def test_carrier_formula_cannot_override_clean_source_vpp_failure(self):
        from tools.ro_test import evaluate_ro
        from tools.self_test import Case
        truth = dict(frequency_hz=237.193, vpp_v=1.5, duty_pct=0., dc_v=1.65,
            wire_carrier_vpp_v=1.8)
        details = dict(ok=True, status=0, frequency_hz=237.193,
            frequency_millihz=237193, vpp_v=1.8, vpp_microvolts=1800000,
            duty_fraction=0., duty_ppm=0, dc_offset_v=1.65, dc_offset_counts=2048)
        result = evaluate_ro(Case(wave='sine', frequency_hz=237.193, group='final_strong'),
            'sine,237.193,1.8,0,1.65', details, 1., {'truth': truth})
        self.assertFalse(result['passed'])
        self.assertIn('vpp_error_pct', result['failure_reasons'])
        self.assertEqual(result['wire_carrier_vpp_error_pct'], 0.)


if __name__ == '__main__':
    unittest.main()
