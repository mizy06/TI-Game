"""Independent reference generation, numerical derivatives and rejection cases."""
import unittest
from dataclasses import replace
import numpy as np
from tools.unified_solver import (estimate, SolverConfig, _basis, _project,
                                  shape, _square_duty_spectral_refine)
from tools.finals_matrix import waveform, counterexamples
from tools import host_waveform as host


class UnifiedTests(unittest.TestCase):
    def test_reduced_jacobian_matches_central_difference(self):
        t=(np.arange(1024)-511.5)/20000
        rng=np.random.default_rng(19)
        for kind,q in ((0,np.array([237.193,.137])),(2,np.array([237.193,.137])),
                       (1,np.array([237.193,.137,.63173,.037,.081]))):
            s,ds=_basis(kind,q,t);y=2+.7*s+rng.normal(0,.01,len(t))
            noise_q=np.linalg.qr(rng.normal(size=(len(t),5)),mode='reduced')[0]
            for weight in (1.,.01):
                def whiten(v): return v-(1-np.sqrt(weight))*noise_q@(noise_q.T@v)
                analytic=_project(whiten(y),whiten(s),whiten(ds))[3]
                for j in range(len(q)):
                    step=1e-6 if j==0 else 1e-8
                    left=q.copy();right=q.copy();left[j]-=step;right[j]+=step
                    numerical=(_project(whiten(y),whiten(_basis(kind,right,t)[0]))[0]-_project(whiten(y),whiten(_basis(kind,left,t)[0]))[0])/(2*step)
                    np.testing.assert_allclose(analytic[:,j],numerical,rtol=2e-5,atol=1e-6)

    def test_floating_reference_all_types_phases_and_frequencies(self):
        for kind in host.NAMES[:4]:
            for f,phase in ((100.,.173),(237.193,.619),(900.,.999999)):
                fs=21317.173;p=np.arange(8192)*f/fs+phase
                y=1.673+.7585*waveform(kind,p,.63173,.037,.081)
                r=estimate(y,fs)
                with self.subTest(kind=kind,f=f,phase=phase):
                    self.assertEqual(r['name'],kind)
                    self.assertLess(abs(r['frequency_hz']/f-1),1e-6)
                    self.assertLess(abs(r['vpp']/1.517-1),1e-6)
                    if kind=='square': self.assertLess(abs(r['duty_fraction']/.63173-1),1e-6)
                    self.assertFalse(r['diagnostics']['accuracy_1ppm_certified'])

    def test_host_does_not_access_legacy_classifier(self):
        class ForbiddenAPI:
            def __getattr__(self,name): raise AssertionError('legacy classifier called')
        y=1.65+.75*np.sin(2*np.pi*237.193*np.arange(8192)/20000+.7)
        payload=np.rint(y*4095/3.3).astype('<u2').tobytes()
        meta=host.Metadata(19,20000000,3300000,1000000,4095,1,host.fnv1a(payload),payload_bytes=len(payload),protocol_version=3)
        _,d=host.analyze(ForbiddenAPI(),payload,meta)
        self.assertTrue(d['ok']);self.assertEqual(d['classification_sample_count'],8192)
        bad=np.frombuffer(payload,dtype='<u2').copy();bad[7000]=4096
        self.assertEqual(host.analyze(None,bad.tobytes(),meta)[1]['status'],6)
        for rail in (0,4095):
            bad[:1000]=rail;bad[7000]=2048
            self.assertFalse(host.analyze(None,bad.tobytes(),meta)[1]['ok'])
        self.assertFalse(host.analyze(None,payload,replace(meta,flags=0))[1]['ok'])

    def test_invalid_inputs_limits_and_deadline(self):
        good=np.sin(np.arange(1024)*.1)
        for y,fs in (([],20000),(np.ones(1024),20000),(good,0),(np.r_[good[:-1],np.nan],20000)):
            with self.assertRaises(ValueError): estimate(y,fs)
        for cfg in (SolverConfig(starts=0),SolverConfig(max_seconds=float('nan')),
                    SolverConfig(frequency_min_hz=2000,frequency_max_hz=1000)):
            with self.assertRaises(ValueError): estimate(good,20000,cfg)
        with self.assertRaises(TimeoutError): estimate(good,20000,SolverConfig(max_seconds=1e-9))

    def test_unidentifiable_examples_are_exact(self):
        c=counterexamples()
        self.assertTrue(c['coherent_noise']['identical_adc'])
        self.assertTrue(c['square_locked_sampling']['identical_samples'])
        self.assertTrue(c['clock_scale']['identical_sample_phases'])

    def test_inverted_square_keeps_high_level_duty(self):
        p=np.arange(8192)*237.193/21317.173+.619
        y=1.65-.75*waveform('square',p,.2,.037,.081)
        r=estimate(y,21317.173)
        self.assertEqual(r['name'],'square')
        self.assertAlmostEqual(r['duty_fraction'],.8,delta=1e-6)

    def test_duty_search_bounds_are_observable(self):
        p=np.arange(8192)*237.193/21317.173+.619
        for duty in (.05,.95):
            y=1.65+.75*waveform('square',p,duty,.01,.02)
            r=estimate(y,21317.173)
            self.assertEqual(r['name'],'square')
            self.assertAlmostEqual(r['duty_fraction'],duty,delta=1e-6)

    def test_sharp_square_duty_spectral_refinement_recovers_subsample_edges(self):
        n, fs, f = 8192, 20009.77238653639, 224.71912348160987
        duty, phase = .784592547248832, .9323467888311839
        t = (np.arange(n) - (n - 1) / 2) / fs
        frequencies = np.fft.rfftfreq(n, 1 / fs)
        rng = np.random.default_rng(412588955)
        spectrum = np.fft.rfft(rng.normal(size=n))
        distance = np.abs(frequencies - np.rint(frequencies / f) * f)
        spectrum[(frequencies > 3 * f) | (distance < .18 * f)] = 0
        noise = np.fft.irfft(spectrum, n=n)
        noise *= .099 / np.std(noise)
        y = .2 + .62 * shape(1, f * t + phase, duty, .0001, .0001) + noise

        q = np.array([f, phase + .001, duty - .002, .0001, .0001])
        fit = dict(kind=1, order=0, q=q, a=.62, cb=np.array([.2]),
                   residual=.62 * shape(1, f * t + q[1], q[2], q[3], q[4]) + .2 - y,
                   low=np.array([f - 1, q[1] - .24, .05, 1e-5, 1e-5]),
                   high=np.array([f + 1, q[1] + .24, .95, .2, .2]))
        refined, info = _square_duty_spectral_refine(y, t, fit, 0., fs, lambda: None)

        self.assertTrue(info['accepted'])
        self.assertLess(abs(refined['q'][2] - duty), abs(q[2] - duty))
        self.assertLess(abs(refined['q'][2] - duty), .001)
        np.testing.assert_array_equal(refined['q'][[0, 3, 4]], q[[0, 3, 4]])
        self.assertEqual(refined['a'], fit['a'])
        np.testing.assert_array_equal(refined['cb'], fit['cb'])


if __name__=='__main__': unittest.main()
