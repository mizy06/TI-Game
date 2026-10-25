import json
import unittest

import numpy as np

from tools.vpp_spectral import harmonic_amplitude, joint_frame_amplitude, saw_reset_edges


class SawResetAmplitudeTests(unittest.TestCase):
    def signal(self, phase=.2, polarity=1):
        t=np.arange(8192)/20000
        return 1.6+polarity*.4*(1-2*((100*t+phase)%1))

    def test_off_lattice_impulse_at_partial_first_cycle_is_not_a_reset(self):
        y=self.signal()
        y[10:20]+=.3
        edges,info=saw_reset_edges(y,20000.,100.)
        self.assertNotIn(9,edges)
        self.assertGreaterEqual(info['discarded_off_lattice_edges'],1)
        self.assertTrue(np.all(np.abs(np.diff(edges)/200-1)<.01))
        self.assertEqual(info['inserted_resets'],0)
        vpp,_=harmonic_amplitude(y,20000.,3,100.)
        self.assertLess(abs(vpp/.8-1),.002)

    def test_missing_interior_reset_is_still_rejected(self):
        y=self.signal()
        edge=np.flatnonzero(np.diff(y)>.5)[10]
        y[edge+1:]-=.8
        with self.assertRaisesRegex(ValueError,'unreliable saw reset lattice'):
            saw_reset_edges(y,20000.,100.)

    def test_both_saw_directions_and_phases_preserve_gain(self):
        for polarity in (1,-1):
            for phase in (.001,.2,.619,.999):
                with self.subTest(polarity=polarity,phase=phase):
                    value,info=harmonic_amplitude(self.signal(phase,polarity),20000.,3,100.)
                    self.assertLess(abs(value/.8-1),1e-6)
                    self.assertEqual(info['reset_lattice']['reset_polarity'],polarity)
                    self.assertGreater(info['source_harmonic_energy'],0)

    def test_shared_gain_under_gaussian_noise_uses_all_observed_frames(self):
        rng=np.random.default_rng(10252026)
        codes=np.linspace(0,4095,65)
        transfer=dict(codes=codes.tolist(),volts=(codes*3.3/4095).tolist())
        frames=[]
        for phase in (.137,.619,.871):
            y=self.signal(phase)+rng.normal(0,.8*.4/6,8192)
            frames.append(dict(samples=np.rint(y*4095/3.3),transfer=transfer,
                fs=20000.,frequency=100.,duty=0.,rise=0.,fall=0.,dc=1.6,vpp=.8))
        result=joint_frame_amplitude(frames,3)
        self.assertEqual(result['method'],'shared_reset_interior_ramp_least_squares')
        self.assertEqual(len(result['frame_weights']),3)
        self.assertLess(abs(result['vpp_v']/.8-1),.003)
        self.assertAlmostEqual(sum(result['frame_weights']),1.)
        json.dumps(result,allow_nan=False)

    def test_sine_cannot_supply_a_reset_lattice(self):
        y=np.sin(2*np.pi*100*np.arange(8192)/20000)
        with self.assertRaisesRegex(ValueError,'unreliable saw reset lattice'):
            saw_reset_edges(y,20000.,100.)
