import unittest
from dataclasses import replace
import numpy as np
from tools.unified_solver import estimate,SolverConfig,_basis,_source,_joint,_nuisance
from tools.finals_matrix import waveform

class JointSolverTests(unittest.TestCase):
    def test_nuisance_reduced_derivative_moves_with_frequency(self):
        rng=np.random.default_rng(92831);t=(np.arange(768)-383.5)/20000
        for kind,q in ((0,np.array([237.193,.137])),(2,np.array([237.193,.137])),(1,np.array([237.193,.137,.63173,.037,.081]))):
            y=.71*waveform(('sine','square','triangle')[kind],q[0]*t+q[1],.63173,.037,.081)+rng.normal(0,.1,len(t))
            for bounded in (False,True):
                def fit(x):
                    s,ds=_basis(kind,x,t);B,dB=_nuisance(x[0],t,3,kind)
                    return _joint(y,s,ds,B,dB,.05 if not bounded else 2.,5.)
                analytic=fit(q)[4]
                for j in range(len(q)):
                    h=1e-6 if j==0 else 1e-8;left=q.copy();right=q.copy();left[j]-=h;right[j]+=h
                    diff=(fit(right)[0]-fit(left)[0])/(2*h)
                    np.testing.assert_allclose(analytic[:,j],diff,rtol=1e-4,atol=2e-6)

    def test_coherent_noise_separated_by_saw_innovations(self):
        fs=21317.173;f=237.193;t=np.arange(8192)/fs
        noise=.11*np.sin(2*np.pi*f*t+.27)+.07*np.cos(4*np.pi*f*t+.9)
        y=1.65+.75*waveform('sawtooth',f*t+.137,.5,0,0)+noise
        result=estimate(y,fs,SolverConfig(starts=1))
        self.assertEqual(result['name'],'sawtooth')
        self.assertLess(abs(result['frequency_hz']/f-1),1e-6)
        self.assertLess(abs(result['vpp']/1.5-1),1e-6)
        self.assertFalse(result['diagnostics']['accuracy_1ppm_certified'])

    def test_quantization_rechecks_actual_nonlinear_model(self):
        fs=21317.173;f=237.193;t=np.arange(8192)/fs
        y=np.rint(1800+900*waveform('sawtooth',f*t+.619,.5,0,0))
        result=estimate(y,fs,SolverConfig(starts=1),quantization_step=1.)
        q=result['diagnostics']['quantization']
        self.assertTrue(q['attempted']);self.assertTrue(q['accepted']);self.assertTrue(q['all_samples_in_bins'])
        self.assertLessEqual(q['maximum_bin_violation_lsb'],1e-7)
        self.assertFalse(result['diagnostics']['accuracy_1ppm_certified'])

    def test_coherent_sine_reports_source_ambiguity(self):
        fs=20000;f=237.193;t=np.arange(4096)/fs
        clean=1.65+.75*np.sin(2*np.pi*f*t+.137)
        other=clean+.1*np.sin(2*np.pi*f*t+.137)
        result=estimate(other,fs,SolverConfig(starts=1))
        self.assertEqual(result['name'],'sine')
        self.assertFalse(result['diagnostics']['source_amplitude_identifiable_under_nuisance_prior'])
        self.assertTrue(result['diagnostics']['sine_carrier_includes_unknown_coherent_fundamental'])
        self.assertAlmostEqual(result['vpp'],1.7,places=5)

    def test_residual_expands_coherent_bandwidth_without_truth(self):
        fs=20000.;f=237.193;t=np.arange(4096)/fs
        y=1.65+.75*np.sin(2*np.pi*f*t+.137)+.08*np.sin(2*np.pi*15*f*t+.9)+.04*np.cos(2*np.pi*18*f*t+.3)
        r=estimate(y,fs,SolverConfig(starts=1))
        self.assertEqual(r['name'],'sine')
        self.assertGreaterEqual(r['diagnostics']['nuisance_order'],18)
        self.assertLess(abs(r['frequency_hz']/f-1),1e-6)
        self.assertLess(abs(r['vpp']/1.5-1),1e-6)

    def test_zero_order_hold_edge_timing_is_explicitly_uncertain(self):
        fs=20240.;dac_fs=19760.;f=100.;size=4150
        p=(.620026323+np.arange(size)*f/dac_fs)%1
        table=np.where(p<.2,2800.,1000.)
        indices=np.floor(np.arange(4096)*dac_fs/fs+217).astype(int)%size
        y=np.rint(table[indices]+np.random.default_rng(4261).normal(0,.7,4096))
        r=estimate(y,fs,SolverConfig(starts=1),quantization_step=1.)
        self.assertEqual(r['name'],'square')
        self.assertEqual(r['diagnostics']['edge_timing_uncertainty_samples'],1.5)
        self.assertFalse(r['diagnostics']['edge_identifiable'])
        self.assertFalse(r['diagnostics']['accuracy_1ppm_certified'])
        self.assertLessEqual(.5*(r['diagnostics']['square_rise_fraction']+r['diagnostics']['square_fall_fraction']),min(r['duty_fraction'],1-r['duty_fraction'])+1e-10)

    def test_nonfinite_new_configuration_rejected(self):
        y=np.sin(np.arange(1024)*.1)
        for cfg in (SolverConfig(nuisance_orders=()),SolverConfig(nuisance_orders=(33,)),SolverConfig(max_noise_ratio=float('nan'))):
            with self.assertRaises(ValueError):estimate(y,20000,cfg)
        with self.assertRaises(ValueError):estimate(y,20000,quantization_step=float('nan'))

if __name__=='__main__':unittest.main()
