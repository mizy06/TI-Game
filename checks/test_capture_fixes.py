"""Independent synthetic regressions for the real-capture failure fixes."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import unified_solver as solver


class CaptureFixTests(unittest.TestCase):
    def test_aperture_triangle_matches_integration_and_derivatives(self):
        fs=20713.;t=(np.arange(769)-384)/fs
        q=np.array([673.291,.173])
        cycle={'aperture_s':1/fs}
        s,ds=solver._source(2,q,t,cycle)
        offsets=(np.arange(2000)+.5)/2000-.5
        integrated=solver.shape(2,q[0]*(t[:,None]+offsets/fs)+q[1]).mean(axis=1)
        np.testing.assert_allclose(s,integrated,atol=2e-8)
        for j,step in enumerate((1e-5,1e-7)):
            left=q.copy();right=q.copy();left[j]-=step;right[j]+=step
            numerical=(solver._source(2,right,t,cycle)[0]-solver._source(2,left,t,cycle)[0])/(2*step)
            np.testing.assert_allclose(ds[:,j],numerical,rtol=2e-5,atol=2e-7)

    def test_worse_final_refinement_preserves_admitted_fit(self):
        fs=21137.;f=283.173;t=np.arange(4096)/fs
        y=2050+700*solver.shape(2,f*t+.173)
        real=solver.least_squares
        def deteriorate(fun,x0,**kwargs):
            # The final joint fit allows its nuisance design to move; the
            # initial projection has separate amplitude/Jacobian caches.
            if 'cache' in fun.__code__.co_freevars:
                x=np.asarray(kwargs['bounds'][1]).copy()-1e-7
                return SimpleNamespace(x=x,success=True,nfev=1)
            return real(fun,x0,**kwargs)
        cfg=solver.SolverConfig(starts=1,bandlimited_refine=False,quantization_refine=False)
        with patch.object(solver,'least_squares',side_effect=deteriorate):
            result=solver.estimate(y,fs,cfg)
        self.assertEqual(result['name'],'triangle')
        self.assertAlmostEqual(result['frequency_hz'],f,delta=1e-5)
        self.assertAlmostEqual(result['vpp'],1400.,delta=.01)

    def test_gain_regularization_is_bounded_by_frame_information(self):
        rng=np.random.default_rng(83291);fs=20317.;f=193.17
        t=(np.arange(4096)-2047.5)/fs
        s,_=solver._source(2,np.array([f,.173]),t)
        B,_=solver._nuisance(f,t,5,2)
        y=.8*s+B@np.r_[2.,rng.normal(0,.05,10)]+rng.normal(0,.04,len(t))
        r,a,cb,en=solver._joint(y,s,None,B)
        updated,cc,rr,se,target=solver._regularized_gain(y,s,B,a,r)
        self.assertLessEqual(abs(updated-a),se+1e-12)
        self.assertLessEqual(np.linalg.norm((B@cc)[1:]-(B@cc).mean()),
                             np.linalg.norm((B@cb)[1:]-(B@cb).mean())+1e-8)
        self.assertLessEqual(abs(updated-target),abs(a-target)+1e-12)
        np.testing.assert_allclose(rr,updated*s+B@cc-y)
        # The standard error scales with the measured residual, not an ID or
        # a test tolerance; a noiseless independent frame gets no shift.
        exact=solver._regularized_gain(.8*s+B@cb,s,B,.8,np.zeros(len(t)))
        self.assertEqual(exact[0],.8)
        self.assertEqual(exact[3],0.)

    def test_marginal_nuisance_compares_overlapping_source_families(self):
        rng=np.random.default_rng(39183);fs=21317.;f=371.391
        t=(np.arange(4096)-2047.5)/fs;q=np.array([f,.173])
        B,_=solver._nuisance(f,t,10,2)
        interference=B@np.r_[.7,rng.normal(0,.012,20)]
        for kind in (0,2):
            source,_=solver._source(kind,q,t)
            y=.8*source+interference+rng.normal(0,.003,len(t))
            scores={}
            for candidate in (0,2):
                qq=q.copy()
                if candidate==0:
                    design=np.column_stack((np.ones(len(t)),np.sin(solver.TAU*f*t),np.cos(solver.TAU*f*t)))
                    coefficients=np.linalg.lstsq(design,y,rcond=None)[0]
                    qq[1]=np.arctan2(coefficients[2],coefficients[1])/solver.TAU
                ss,_=solver._source(candidate,qq,t)
                BB,_=solver._nuisance(f,t,10,candidate)
                scores[candidate]=solver._nuisance_evidence(y,ss,BB,2)[0]
            self.assertLess(scores[kind],scores[2 if kind==0 else 0])

    def test_nuisance_prior_cannot_overturn_distinct_residual_evidence(self):
        rng=np.random.default_rng(8381);fs=20713.;f=183.291
        t=np.arange(4096)/fs
        y=2050+700*np.sin(solver.TAU*f*t+.31)+35*np.cos(solver.TAU*2*f*t)+rng.normal(0,2,len(t))
        # An intentionally wrong prior must have no access to distant BIC
        # candidates; direct fit evidence takes precedence over that prior.
        def wrong_prior(y,s,B,p):
            return (-1e9 if B.shape[1]%4==3 else 0.),1.
        cfg=solver.SolverConfig(starts=1,bandlimited_refine=False,quantization_refine=False)
        with patch.object(solver,'_nuisance_evidence',side_effect=wrong_prior):
            result=solver.estimate(y,fs,cfg,quantization_step=1.)
        self.assertEqual(result['name'],'sine')
        self.assertLess(abs(result['vpp']/1400-1),.01)


if __name__=='__main__':
    unittest.main(verbosity=2)
