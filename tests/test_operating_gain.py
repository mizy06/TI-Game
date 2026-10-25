"""Operating amplitude references and the non-spectral calibration path."""
import unittest
import io
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np

from tools import host_waveform as host
from tools.dynamic_amplitude_calibration import (
    check_operating_amplitude, fit_operating_response,calibrate_adc_transfer,
    refine_adc_codes,tone_amplitude,GAIN_CHECKS,GAIN_REFERENCES,fit_sample_curve,
    REFERENCES,ORIGINAL_CHECK_OFFSETS)
from tools.amplitude_calibration import validate_transfer,voltage_values


class OperatingGainTests(unittest.TestCase):
    def setUp(self):
        codes=np.arange(34.,4060.)
        self.transfer=dict(codes=codes.tolist(),volts=(codes*3.3/4095).tolist())

    def block(self,offset,amplitude,gain=1.0027,phase=.137):
        period=83.7193
        t=np.arange(8192)/period
        raw=(offset+gain*amplitude/2*np.sin(2*np.pi*(t+phase)))*4095/3.3
        return dict(offset_v=offset,amplitude_v=amplitude,period_samples=period,
                    values=raw)

    def test_fifty_curve_references_cover_operating_range_and_exclude_validation(self):
        self.assertEqual(len(GAIN_REFERENCES),50)
        self.assertEqual(len(set(GAIN_REFERENCES)),50)
        offsets={o for o,a in GAIN_REFERENCES}
        amplitudes={a for o,a in GAIN_REFERENCES}
        self.assertEqual(len(offsets),5)
        self.assertEqual(len(amplitudes),10)
        self.assertEqual((min(offsets),max(offsets)),(1.4,1.8))
        self.assertEqual((min(amplitudes),max(amplitudes)),(.8,1.6))
        self.assertFalse(set(GAIN_CHECKS)&set(GAIN_REFERENCES))

    def test_independent_span_gain_corrects_unseen_amplitudes_and_offsets(self):
        refs=[self.block(offset,amp) for amp in (.8,1.,1.3,1.6) for offset in (1.4,1.8)]
        fixed=fit_operating_response(self.transfer,refs)
        for offset,amp in ((1.45,.83),(1.75,1.11),(1.55,1.37),(1.65,1.57)):
            block=self.block(offset,amp,phase=.461)
            check=check_operating_amplitude(fixed,[block],offset,amp,293.173)
            self.assertLess(abs(check['relative_bias']),1e-10)

    def test_single_small_span_cannot_certify_operating_gain(self):
        with self.assertRaisesRegex(ValueError,'covering'):
            fit_operating_response(self.transfer,[self.block(o,.72) for o in (.4,1.,2.,2.9)])

    def test_filtered_parameters_cannot_silently_apply_to_unfiltered_data(self):
        with self.assertRaisesRegex(ValueError,'rebuild from unfiltered references'):
            validate_transfer(dict(self.transfer,preprocessing={'method':'median3','size':3,'boundary':'nearest'}))

    def test_validation_records_cannot_fit_the_operating_response(self):
        with self.assertRaisesRegex(ValueError,'fitting references only'):
            fit_operating_response(self.transfer,[dict(self.block(1.4,.8),validation=True)])

    def test_span_and_offset_dependent_response_generalizes_to_unseen_points(self):
        def gain(offset,amp):
            return 1.001+.001*amp+.0005*(offset-1.6)
        fixed=fit_operating_response(self.transfer,
            [self.block(o,a,gain=gain(o,a)) for a in (.8,1.,1.3,1.6) for o in (1.4,1.8)])
        for offset,amp in ((1.45,.83),(1.75,1.11),(1.55,1.37),(1.65,1.57)):
            check=check_operating_amplitude(fixed,[self.block(offset,amp,gain=gain(offset,amp))],
                                             offset,amp,293.173)
            self.assertLess(abs(check['relative_bias']),3e-6)

    def test_joint_fit_uses_the_response_once(self):
        from tools.vpp_spectral import joint_frame_amplitude
        fixed=fit_operating_response(self.transfer,
            [self.block(o,a) for a in (.8,1.,1.3,1.6) for o in (1.4,1.8)])
        frames=[]
        for phase in (.13,.47,.79):
            fs,f,amp,dc=20000.,217.193,1.137,1.53
            raw=np.rint((dc+1.0027*amp/2*np.sin(2*np.pi*(np.arange(8192)*f/fs+phase)))*4095/3.3)
            frames.append(dict(samples=raw,transfer=fixed,fs=fs,frequency=f,
                               duty=.5,rise=.03,fall=.03,vpp=amp,dc=dc))
        result=joint_frame_amplitude(frames,0)
        self.assertLess(abs(result['vpp_v']/amp-1),2e-5)

    def test_nonfinite_saw_amplitude_is_rejected_before_returning_diagnostics(self):
        from tools.vpp_spectral import joint_frame_amplitude
        frame=dict(samples=np.full(4096,2000.),transfer=self.transfer,
                   fs=20000.,frequency=250.,duty=0.,rise=0.,fall=0.,vpp=1.52,dc=1.6)
        for amplitude in (0.,float('nan'),float('inf')):
            with self.subTest(amplitude=amplitude), \
                 patch('tools.vpp_spectral.harmonic_amplitude',return_value=(amplitude,{})), \
                 patch('tools.vpp_spectral.saw_ramp_amplitude',return_value=(amplitude,{})):
                with self.assertRaisesRegex(ValueError,'invalid saw reset amplitude'):
                    joint_frame_amplitude([frame,frame],3)

    def test_implausible_amplifier_ranges_are_rejected(self):
        refs=[self.block(o,a,gain=1.03 if a<1 else .998)
              for a in (.8,1.,1.3,1.6) for o in (1.4,1.8)]
        with self.assertRaisesRegex(ValueError,'invalid operating'):
            fit_operating_response(self.transfer,refs)

    def test_unseen_validation_bias_is_rejected(self):
        fixed=fit_operating_response(self.transfer,
            [self.block(o,a) for a in (.8,1.,1.3,1.6) for o in (1.4,1.8)])
        bad=self.block(1.45,.83,gain=1.006)
        with self.assertRaisesRegex(ValueError,'operating amplitude check failed'):
            check_operating_amplitude(fixed,[bad],1.45,.83,293.173)

    def test_failed_validation_collects_other_checks_without_publishing(self):
        service=SimpleNamespace(calibration=None,publish_calibration=Mock())
        instrument=SimpleNamespace(identity='independent-reference',reference=Mock())
        count=0
        def collect(service,store,args,instrument,audit,path,offset,amp,validation,configure):
            nonlocal count
            configure(offset,amp)
            count+=1
            block=self.block(offset,amp,gain=1.006 if validation and offset in ORIGINAL_CHECK_OFFSETS[:2] else 1.0027)
            block.update(capture=f'uart/{count:06d}',validation=validation)
            audit['observations'].append({k:v for k,v in block.items() if k!='values'})
            return [block]
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)
            with patch('tools.dynamic_amplitude_calibration._collect',side_effect=collect), \
                 patch('tools.dynamic_amplitude_calibration.fit_dynamic_spline',return_value={}), \
                 patch('tools.dynamic_amplitude_calibration.fit_sample_curve',side_effect=AssertionError('direct curve must not run')), \
                 patch('tools.dynamic_amplitude_calibration.fit_operating_response',side_effect=AssertionError('50-point response must not run')), \
                 patch('tools.dynamic_amplitude_calibration.refine_adc_codes',return_value=
                     dict(self.transfer,volts=(np.asarray(self.transfer['volts'])/1.0027).tolist())):
                with self.assertRaisesRegex(ValueError,f'2/{len(ORIGINAL_CHECK_OFFSETS)} independent'):
                    calibrate_adc_transfer(service,None,None,io.StringIO(),instrument,report,{},3.3)
            audit=json.loads((report/'amplitude_calibration.json').read_text())
            self.assertFalse(audit['complete'])
            self.assertEqual(audit['fit_point_count'],len(REFERENCES))
            self.assertEqual(audit['fit_capture_count'],len(REFERENCES))
            self.assertEqual(len(audit['checks']),len(ORIGINAL_CHECK_OFFSETS))
            self.assertEqual(len(audit['validation_failures']),2)
            self.assertEqual(audit['calibration_model'],'original_local_ADC_voltage_lookup')
            self.assertNotIn('sample_curve',audit)
            self.assertNotIn('operating_response',audit)
            service.publish_calibration.assert_not_called()
            self.assertFalse((report/'calibration.json').exists())

    def test_direct_curve_recovers_nonlinear_adc_at_unseen_spans(self):
        rng=np.random.default_rng(81529)
        observations=[]
        def block(offset,amp,phase,period):
            truth=offset+amp/2*np.sin(2*np.pi*(np.arange(8192)/period+phase))
            adc=truth*4095/3.3+3*np.sin(2*np.pi*truth/3.3)+1.5*(truth-1.65)**2
            adc=np.rint(adc+rng.normal(0,.2,len(adc)))
            return dict(offset_v=offset,amplitude_v=amp,period_samples=period,values=adc)
        for offset in (.4,.72,1.04,1.36,1.68,2.,2.32,2.64,2.9):
            observations.append(block(offset,.72,.173,83.7193))
        for offset in (1.4,1.8):
            for amp in (.8,1.,1.3,1.6):
                observations.append(block(offset,amp,.413,91.1937))
        fitted=fit_sample_curve(observations)
        self.assertNotIn('operating_response',fitted)
        self.assertFalse(fitted['fit']['validation_samples_used'])
        for offset,amp in ((1.49,.93),(1.71,1.17),(1.59,1.53)):
            unseen=block(offset,amp,.713,74.917)
            v=tone_amplitude(voltage_values(unseen['values'],fitted),unseen['period_samples'])
            self.assertLess(abs(v/amp-1),1e-4)

    def test_direct_curve_rejects_validation_records(self):
        record=self.block(1.6,1.)
        record['validation']=True
        with self.assertRaisesRegex(ValueError,'fitting references only'):
            fit_sample_curve([record])

    def test_noisy_code_refinement_preserves_plausible_gain_and_unseen_span(self):
        rng=np.random.default_rng(82145)
        blocks=[]
        for offset in (.4,.72,1.04,1.36,1.68,2.,2.32,2.64,2.9):
            b=self.block(offset,.72,gain=1.)
            b['values']=np.rint(b['values']+rng.normal(0,1.5,len(b['values'])))
            blocks.append(b)
        grid=np.linspace(0,4095,513)
        prior=dict(input=grid.tolist(),output=(grid*3.3/4095).tolist())
        result=refine_adc_codes(blocks,prior,sigma=.2)
        validate_transfer(result)
        self.assertLess(result['code_refinement_weight'],1.)
        unseen=self.block(1.57,1.137,gain=1.,phase=.413)
        volts=voltage_values(np.rint(unseen['values']),result)
        measured=tone_amplitude(volts,unseen['period_samples'])
        self.assertLess(abs(measured/1.137-1),3e-4)

    def test_noisy_spectral_rejection_still_uses_voltage_calibration(self):
        n,fs,f=8192,20000.,237.193
        rng=np.random.default_rng(781604)
        spectrum=np.fft.rfft(rng.normal(size=n))
        spectrum[np.fft.rfftfreq(n,1/fs)>3*f]=0
        noise=np.fft.irfft(spectrum,n)
        noise*=.06/np.std(noise)
        y=1.65+.4*np.sin(2*np.pi*f*np.arange(n)/fs+.71)+noise
        raw=np.rint(y*4095/3.3).astype('<u2').tobytes()
        meta=host.Metadata(1,20000000,3300000,1000000,4095,7,host.fnv1a(raw),
                           payload_bytes=len(raw),protocol_version=3)
        _,before=host.analyze(None,raw,meta)
        transfer=dict(self.transfer,volts=(1.65+.997*(np.asarray(self.transfer['volts'])-1.65)).tolist())
        _,after=host.analyze(None,raw,replace(meta,vpp_transfer=transfer))
        self.assertFalse(after['spectral_vpp']['accepted'])
        self.assertIn('vpp_extrema_calibration',after)
        self.assertAlmostEqual(after['vpp_v'],before['vpp_v']*.997,places=11)
        for field in ('type','frequency_hz','duty_fraction','dc_offset_v'):
            self.assertEqual(after[field],before[field],field)


if __name__=='__main__':
    unittest.main()
