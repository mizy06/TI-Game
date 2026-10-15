"""Focused functional regressions. All generated inputs are synthetic."""
import importlib.util, json, math, sys, tempfile, unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import numpy as np
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
from tools import unified_solver as solver


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m);return m


class ObservationTests(unittest.TestCase):
    def test_double_clock_noise_and_spikes(self):
        fs=20248.076429812114; n=4096; t=np.arange(n)/fs; dac=19763.
        for kind in range(4):
            for f in (200.,800.):
                for a in (50.,800.):
                    for hold in (False,True):
                        with self.subTest(kind=kind,f=f,a=a,hold=hold):
                            tx=np.floor(t*dac+217)/dac if hold else t
                            rng=np.random.default_rng(31900+kind*100+int(f)+int(a))
                            y=np.rint(2050+a*solver.shape(kind,f*tx+.173,.2,.01,.02)+rng.normal(0,2,n))
                            y[[123,987,3051]] += [18,-19,21]
                            r=solver.estimate(y,fs,quantization_step=1.)
                            self.assertEqual(r['name'],solver.NAMES[kind])
                            self.assertLess(abs(r['frequency_hz']/f-1),.01)
                            self.assertLess(abs(r['vpp']/(2*a)-1),.1)
                            self.assertLess(abs(r['dc']-2050),10.)
                            if kind==1:self.assertLess(abs(r['duty_fraction']-.2),.025)
                            self.assertFalse(r['diagnostics']['accuracy_1ppm_certified'])
                            self.assertFalse(r['diagnostics']['band_exclusion']['accepted'])

    def test_degenerate_and_invalid_reset_lattices(self):
        n=4096;fs=20000.;f=200.
        # Equal exact bounds at rational fs/f used to delete the saw family.
        edges=np.arange(99,n-1,100)
        cycle,lo,hi,ap=solver._saw_topology(edges,n,fs,f,199.,201.,1.5)
        self.assertIsNotNone(cycle);self.assertLessEqual(lo,f);self.assertGreaterEqual(hi,f)
        jitter=edges+np.resize([0,1,-1,0],len(edges))
        cycle,lo,hi,ap=solver._saw_topology(jitter,n,fs,f,199.,201.,1.5)
        self.assertIsNotNone(cycle);self.assertGreater(ap,0)
        for bad in (np.delete(jitter,10),np.sort(np.r_[jitter,jitter[10]+30])):
            self.assertIsNone(solver._saw_topology(bad,n,fs,f,199.,201.,1.5)[0])

    def test_plateau_observability_is_required(self):
        fs=20248.;t=(np.arange(4096)-2047.5)/fs
        valid=solver._observation_mask(1,np.array([800.,.173,.2,.01,.02]),t,fs,None,1.5)
        self.assertIsNotNone(valid);self.assertGreater(np.mean(~valid),.2)
        # Aperture padding must preserve a short, repeatedly observed plateau.
        narrow=solver._observation_mask(1,np.array([1600.,.173,.05,.01,.02]),t,fs,None,1.5)
        self.assertIsNotNone(narrow)
        # Overlapping edges and a frame with too few cycles remain unobservable.
        self.assertIsNone(solver._observation_mask(1,np.array([1600.,.173,.05,.06,.06]),t,fs,None,1.5))
        self.assertIsNone(solver._observation_mask(1,np.array([5.,.173,.05,.01,.02]),t,fs,None,1.5))

    def test_background_scale_and_bad_config(self):
        rng=np.random.default_rng(78421);y=rng.normal(0,2,30000)
        signal=y+800*np.sin(2*np.pi*200*np.arange(len(y))/20000)
        self.assertLess(abs(solver._background_scale(signal,1.)/2-1),.04)
        self.assertLess(solver._background_scale(y,1.),.3)
        coeff=np.diff(np.eye(6),n=5,axis=0).ravel()
        self.assertEqual(int(coeff@coeff),252)
        for cfg in (solver.SolverConfig(background_sigma=-1),solver.SolverConfig(observation_aperture_samples=math.nan),solver.SolverConfig(observation_aperture_samples=4)):
            with self.assertRaises(ValueError):solver.estimate(np.sin(np.arange(1024)*.1),20000,cfg)
        for y,fs in [(np.ones(1024),20000),(np.arange(255),20000),(np.r_[np.nan,np.ones(1023)],20000),(np.arange(1024),0)]:
            with self.assertRaises(ValueError):solver.estimate(y,fs)

    def test_band_rejects_weak_and_inconsistent_information(self):
        fs=20000.;t=np.arange(4096)/fs;s=np.sin(2*np.pi*200*t)
        self.assertEqual(solver._band_validation(s,s,1.,fs,8312.92,1e-4),(False,'cutoff_too_high'))
        self.assertFalse(solver._band_validation(s,s,1.,fs,3000.,.01)[0])
        s=np.sign(s);y=s.copy();y[len(y)//2:]*=1.2
        self.assertFalse(solver._band_validation(y,s,1.,fs,3000.,.001)[0])

    def test_deadline_counts_waiting_time(self):
        real=solver._frequency_starts
        tick=[0.]
        def seeds(*args):
            out=real(*args);tick[0]=2.;return out
        with patch.object(solver.time,'perf_counter',side_effect=lambda:tick[0]),patch.object(solver,'_frequency_starts',side_effect=seeds):
            with self.assertRaises(TimeoutError):
                solver.estimate(np.sin(np.arange(4096)*.07),20000,solver.SolverConfig(max_seconds=1.))

    def test_coherent_interference_does_not_flatten_family_scores(self):
        fs=21209.626219394588;f=747.4640171087541;t=np.arange(8192)/fs
        y=np.rint(2050+800*solver.shape(2,f*t+.696823)
                  +130*np.sin(2*np.pi*5*f*t+.27)+85*np.cos(2*np.pi*6*f*t+.91))
        r=solver.estimate(y,fs,quantization_step=1.)
        self.assertEqual(r['name'],'triangle')
        self.assertLess(abs(r['vpp']/1600-1),.01)
        self.assertFalse(r['diagnostics']['accuracy_1ppm_certified'])

    def test_integer_comb_does_not_override_independent_sine_frequency(self):
        fs=20000.;t=(np.arange(4096)-2047.5)/fs;q=np.array([237.193,.173])
        s,_=solver._source(0,q,t,None);y=s+.1*np.sin(2*np.pi*47.3*t)
        fit=dict(kind=0,order=0,q=q.copy(),a=1.,residual=y-s)
        def clock():pass
        with patch.object(solver,'_detect_subharmonic_comb',return_value=(13.,20)):
            result,accepted=solver._subharmonic_refine(y,t,fit,.001,fs,100.,400.,.1,2.,clock,.55)
        self.assertFalse(accepted);np.testing.assert_array_equal(result['q'],q)

    def test_background_only_has_no_admissible_waveform(self):
        for seed in (19,93,771):
            y=2050+np.random.default_rng(seed).normal(0,2,4096)
            with self.subTest(seed=seed),self.assertRaises(ValueError):
                solver.estimate(y,20000,quantization_step=1.)


if __name__=='__main__':unittest.main(verbosity=2)
