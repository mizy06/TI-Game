import json
import unittest
from collections import Counter

import numpy as np

from tools.identifiable_finals import (DESIGN_SEED, HARMONIC_GUARD_RATIO, MATRIX_PATH,
                                       REVISION, design_rows, load_rows, noise_samples)
from tools.ro_test import _finals_waveform, _generate_finals_row, board_levels, encode, ro_matrix


class IdentifiableFinalsTests(unittest.TestCase):
    def test_published_design_and_balanced_finals_only(self):
        rows = load_rows()
        self.assertEqual(len(rows),296)
        self.assertEqual(rows, design_rows())
        policy=json.loads(MATRIX_PATH.read_text(encoding='utf-8'))
        self.assertEqual(policy['revision'],REVISION)
        self.assertEqual(policy['design_seed'],DESIGN_SEED)
        self.assertEqual(policy['harmonic_guard_ratio'],HARMONIC_GUARD_RATIO)
        self.assertEqual(policy['frequency_range_hz'],[100.,400.])
        self.assertEqual(policy['vpp_range_v'],[.8,1.6])
        self.assertEqual(policy['dc_range_v'],[1.4,1.8])
        self.assertEqual(policy['duty_range_fraction'],[.2,.8])
        self.assertEqual(policy['noise_type'],'gaussian')
        self.assertEqual(policy['noise_ratio_by_group'],{'weak':[.1,.2],'strong':[.35,.5]})
        self.assertEqual(policy['bandwidth_ratio_by_group'],{'weak':[5.,8.],'strong':[1.5,3.]})
        self.assertEqual(Counter(r['group'] for r in rows), {'weak':148,'strong':148})
        self.assertEqual(Counter(r['kind'] for r in rows), dict.fromkeys(('sine','square','triangle','sawtooth'),74))
        self.assertEqual(min(r['frequency'] for r in rows),100.)
        self.assertEqual(max(r['frequency'] for r in rows),400.)
        for r in rows:
            self.assertTrue(100 <= r['frequency'] <= 400)
            self.assertTrue(.8 <= r['vpp'] <= 1.6)
            self.assertTrue(1.4 <= r['dc_v'] <= 1.8)
            self.assertTrue(.2 <= r['duty'] <= .8)
            self.assertEqual(r['noise_model'],'gaussian')
            self.assertEqual(r['harmonic_guard_ratio'],HARMONIC_GUARD_RATIO)
            if r['group']=='weak':
                self.assertTrue(.1 <= r['noise'] <= .2)
                self.assertTrue(5 <= r['bandwidth'] <= 8)
            else:
                self.assertTrue(.35 <= r['noise'] <= .5)
                self.assertTrue(1.5 <= r['bandwidth'] <= 3)

    def test_every_noise_is_bandlimited_gaussian_with_published_sigma(self):
        for r in load_rows():
            noise, audit = noise_samples(r,r['seed'])
            self.assertLess(audit['maximum_out_of_band_coefficient'],1e-10)
            self.assertLess(audit['maximum_protected_coefficient'],1e-10)
            self.assertTrue(audit['same_frequency_noise_absent'])
            self.assertGreater(audit['protected_harmonic_bins'],1)
            self.assertAlmostEqual(np.std(noise),r['noise']*r['vpp']/6,places=13)
            self.assertAlmostEqual(audit['expected_noise_pp_v'],r['noise']*r['vpp'])
            self.assertGreater(np.ptp(noise),.5*r['noise']*r['vpp'])
            self.assertLess(np.ptp(noise),1.5*r['noise']*r['vpp'])
            self.assertAlmostEqual(float(noise.mean()),0,places=13)
            self.assertGreater(audit['active_fourier_bins'],8)
            self.assertAlmostEqual(audit['bandwidth_hz'],r['frequency']*r['bandwidth'])

    def test_entire_matrix_closes_both_source_and_noise_without_tail(self):
        for c in ro_matrix():
            adc,dac,config=encode(c,*board_levels())
            self.assertEqual(len(adc),16384)
            self.assertEqual(len(dac),16384)
            self.assertEqual(config['seam_bridge_samples'],0)
            self.assertEqual(config['truth']['vpp_v'],c.vpp_v)
            self.assertEqual(config['truth']['dc_v'],c.dc_v)
            self.assertEqual(config['rt_generated']['prequantization_clipped_samples'],0)
            self.assertTrue(0<config['input_min_v']<config['input_max_v']<3.3)

    def test_distinct_clean_amplitudes_can_produce_identical_old_input(self):
        t=np.arange(8192)/20000
        s=np.sin(2*np.pi*237.193*t+.17)
        world_a=1.65+.75*s+.015*s
        world_b=1.65+.765*s
        self.assertTrue(np.allclose(world_a,world_b,rtol=0,atol=1e-15))
        self.assertTrue(np.array_equal(np.rint(world_a*4095/3.3),np.rint(world_b*4095/3.3)))

    def test_noise_has_no_projection_on_any_clean_source_harmonic(self):
        for row in load_rows():
            noise,_=noise_samples(row,row['seed'])
            coefficients=np.fft.rfft(noise)
            spacing=row['fs']/row['samples']
            cycles=round(row['frequency']/spacing)
            harmonics=np.arange(0,len(coefficients),cycles)
            self.assertLess(float(np.max(np.abs(coefficients[harmonics]))),1e-10,row['kind'])

    def test_high_resolution_stimulus_preserves_truth_period_and_input_envelope(self):
        for c in ro_matrix():
            adc,dac,config=encode(c,*board_levels(),oversample=32)
            self.assertEqual(len(adc),16384)
            self.assertEqual(len(dac),16384*32)
            self.assertEqual(config['truth']['vpp_v'],c.vpp_v)
            self.assertAlmostEqual(config['sample_rate_hz']/config['waveform_samples'],c.fs_hz/8192)
            self.assertTrue(0<config['input_min_v']<config['input_max_v']<3.3)
            self.assertLess(config['dac_roundtrip_error_v'],.000026)
            row=json.loads(c.source)
            phase=np.arange(config['waveform_samples'])*c.frequency_hz/config['sample_rate_hz']+row['phase']
            clean=c.dc_v+.5*c.vpp_v*_finals_waveform(c.wave,phase,c.duty,c.rise,c.fall)
            restored=1.65*(np.frombuffer(dac,dtype='<i2')/32767+1)
            self.assertAlmostEqual(float(np.std(restored-clean)),c.noise_pp_ratio*c.vpp_v/6,
                                   delta=config['dac_roundtrip_error_v']+1e-12,
                                   msg=c.id)


if __name__=='__main__':
    unittest.main()
