"""Spike admission must not smooth periodic peaks or true waveform edges."""
import unittest

import numpy as np

from tools.unified_solver import shape
from tools.vpp_preprocessing import harmonic_design, periodic_outlier_weights


class VppPreprocessingTests(unittest.TestCase):
    def test_sine_impulses_are_rejected_without_rewriting_adc_samples(self):
        rng = np.random.default_rng(79123)
        fs, f, n = 20000., 237.193, 8192
        phase = np.arange(n)*f/fs+.137
        y = 1.6+.65*shape(0, phase)+rng.normal(0, .001, n)
        indices = np.arange(85, n-30, 127)
        y[indices] += .2*np.sign(np.sin(2*np.pi*phase[indices]))
        saved = y.copy()
        weights, info = periodic_outlier_weights(y, fs, f)
        self.assertTrue(info['applied'])
        self.assertGreater(np.count_nonzero(weights[indices] == 0), .9*len(indices))
        self.assertTrue(np.array_equal(y, saved))
        design = harmonic_design(n, fs, f)
        before = np.linalg.lstsq(design, y, rcond=None)[0]
        after = np.linalg.lstsq(design[weights > 0], y[weights > 0], rcond=None)[0]
        vpp = lambda beta: 2*np.hypot(beta[1], beta[2])
        self.assertGreater(abs(vpp(before)/1.3-1), .001)
        self.assertLess(abs(vpp(after)/1.3-1), .0002)

    def test_clean_waveform_knots_and_peaks_are_retained(self):
        fs, f, n = 20000., 237.193, 8192
        phase = np.arange(n)*f/fs+.137
        for kind in range(4):
            y = 1.6+.65*shape(kind, phase, .63173, .001, .002)
            weights, info = periodic_outlier_weights(y, fs, f, kind, duty=.63173)
            with self.subTest(kind=kind):
                self.assertEqual(info['removed_count'], 0)
                self.assertTrue(np.all(weights == 1))

    def test_gaussian_noise_does_not_trigger_sample_removal(self):
        rng = np.random.default_rng(61719)
        fs, f, n = 20000., 237.193, 8192
        for kind in range(4):
            y = 1.6+.65*shape(kind, np.arange(n)*f/fs+.137, .63173)+rng.normal(0, .078, n)
            _, info = periodic_outlier_weights(y, fs, f, kind, duty=.63173)
            with self.subTest(kind=kind):
                self.assertEqual(info['removed_count'], 0)

    def test_invalid_samples_are_reported_instead_of_hidden(self):
        y = np.ones(8192)
        y[123] = np.nan
        with self.assertRaises(ValueError):
            periodic_outlier_weights(y, 20000., 237.193)


if __name__ == '__main__':
    unittest.main()
