import unittest
from dataclasses import replace
import numpy as np
from tools.unified_solver import estimate,SolverConfig
from reference import waveform

class BandTests(unittest.TestCase):
 def test_off_grid_band_interference_changes_parameters(self):
  fs=21431.13;f=227.381;t=np.arange(8192)/fs
  noise=.12*np.sin(2*np.pi*137.293*t+.61)+.08*np.cos(2*np.pi*331.781*t+.13)+.04*np.sin(2*np.pi*641.773*t+.39)
  y=np.rint((1.65+.75*waveform('square',f*t+.173,.631,.047,.029)+noise)*4095/3.3)
  cfg=SolverConfig(starts=1)
  before=estimate(y,fs,replace(cfg,bandlimited_refine=False),quantization_step=1.)
  after=estimate(y,fs,cfg,quantization_step=1.)
  self.assertEqual(after['name'],'square')
  self.assertTrue(after['diagnostics']['band_exclusion']['accepted'])
  self.assertLess(abs(after['vpp']/ (1.5*4095/3.3)-1),1e-5)
  self.assertLess(abs(after['frequency_hz']/f-1),1e-6)
  self.assertLess(abs(after['vpp']/(1.5*4095/3.3)-1),.2*abs(before['vpp']/(1.5*4095/3.3)-1))
  self.assertFalse(after['diagnostics']['accuracy_1ppm_certified'])
 def test_white_noise_does_not_earn_band_exclusion(self):
  rng=np.random.default_rng(348991);fs=21431.13;f=227.381;t=np.arange(4096)/fs
  y=1.65+.75*waveform('triangle',f*t+.173)+rng.normal(0,.03,len(t))
  r=estimate(y,fs,SolverConfig(starts=1))
  self.assertEqual(r['name'],'triangle')
  self.assertFalse(r['diagnostics']['band_exclusion']['accepted'])
 def test_same_frequency_sine_ambiguity_remains(self):
  fs=20000.;f=237.193;t=np.arange(4096)/fs
  y=1.65+.85*np.sin(2*np.pi*f*t+.137)+.03*np.sin(2*np.pi*319.13*t)
  r=estimate(y,fs,SolverConfig(starts=1))
  self.assertEqual(r['name'],'sine')
  self.assertFalse(r['diagnostics']['band_exclusion']['accepted'])
  self.assertFalse(r['diagnostics']['source_amplitude_identifiable_under_nuisance_prior'])
 def test_invalid_band_setting_rejected(self):
  with self.assertRaises(ValueError):estimate(np.sin(np.arange(1024)*.1),20000,SolverConfig(bandlimited_refine='yes'))

if __name__=='__main__':unittest.main(verbosity=2)
