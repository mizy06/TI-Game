import unittest
from dataclasses import replace
import numpy as np
from tools import host_waveform as host
from tools.amplitude_calibration import validate_transfer, voltage_values, predistort_dac
from tools.vpp_spectral import harmonic_amplitude
from tools.finals_matrix import waveform


class AmplitudePathTests(unittest.TestCase):
    def test_dynamic_calibration_recovers_local_ac_gain_without_dc_fit(self):
        from tools.dynamic_amplitude_calibration import fit_dynamic,tone_amplitude,REFERENCES
        rng=np.random.default_rng(817631)
        observations=[]
        for i,(offset,amp) in enumerate(REFERENCES):
            period=20000/173.371
            volts=offset+amp/2*np.sin(2*np.pi*np.arange(8192)/period+.37*i)
            distorted=volts+.003*volts**2-.0004*volts**3
            raw=distorted*4095/3.3-8+rng.normal(0,.4,len(volts))
            observations.append(dict(values=raw,amplitude_v=amp,offset_v=offset,period_samples=period))
        fit=fit_dynamic(observations,4095.)
        from numpy.polynomial.chebyshev import chebval
        for offset in (.57,1.77,2.67):
            volts=offset+.43/2*np.sin(2*np.pi*np.arange(8192)/period+.13)
            raw=(volts+.003*volts**2-.0004*volts**3)*4095/3.3-8
            restored=chebval(2*raw/4095-1,fit['coefficients'])
            self.assertLess(abs(tone_amplitude(restored,period)/.43-1),.0001)

    def test_monotone_transfer_and_inverse_dac_preserve_voltage(self):
        levels=np.linspace(.02,3.28,41)
        codes=4095/3.3*(levels+.001*levels**2)
        transfer=dict(codes=codes.tolist(),volts=levels.tolist())
        validate_transfer(transfer)
        probe=np.linspace(.04,3.25,800)
        measured=voltage_values(4095/3.3*(probe+.001*probe**2),transfer)
        self.assertLess(np.max(np.abs(measured-probe)),1e-6)
        with self.assertRaises(ValueError):
            validate_transfer(dict(codes=list(reversed(codes)),volts=levels.tolist()))
        norm=np.linspace(-1,1,81)
        output=1.65+1.6*norm+.003*norm**2
        desired=np.linspace(.1,3.2,501)
        data=np.rint((desired-1.65)/1.65*32767).astype('<i2').tobytes()
        fixed=np.frombuffer(predistort_dac(data,dict(amplitude_v=3.3,offset_v=1.65),
            dict(normalized=norm.tolist(),volts=output.tolist())),dtype='<i2')/32767
        actual=1.65+1.6*fixed+.003*fixed**2
        self.assertLess(np.max(np.abs(actual-desired)),.00006)

    def test_all_families_reject_separated_strong_noise_in_amplitude(self):
        n,fs=8192,20000.
        f=fs*97/n
        p=np.arange(n)*f/fs+.137
        rng=np.random.default_rng(904772)
        freq=np.fft.rfftfreq(n,1/fs)
        spec=np.fft.rfft(rng.normal(size=n))
        spec[(np.abs(freq-np.rint(freq/f)*f)<.16*f)|(freq>3*f)]=0
        noise=np.fft.irfft(spec,n)
        noise*=.4*.847/np.ptp(noise)
        for kind,name in enumerate(host.NAMES[:4]):
            y=1.41+.847/2*waveform(name,p,.63173,.037,.081)+noise
            vpp,diag=harmonic_amplitude(y,fs,kind,f,.63173,.037,.081)
            with self.subTest(kind=name):
                self.assertLess(abs(vpp/.847-1),.001)

    def test_amplitude_metadata_preserves_other_measurements(self):
        n,fs=8192,20000.
        y=1.65+.4*np.sin(2*np.pi*237.193*np.arange(n)/fs+.71)
        raw=np.rint(y*4095/3.3).astype('<u2').tobytes()
        meta=host.Metadata(1,20000000,3300000,1000000,4095,7,host.fnv1a(raw),
                           payload_bytes=len(raw),protocol_version=3)
        _,old=host.analyze(None,raw,meta)
        codes=np.linspace(10,4085,25)
        transfer=dict(codes=codes.tolist(),volts=(codes*3.3/4095).tolist())
        _,new=host.analyze(None,raw,replace(meta,vpp_transfer=transfer))
        for field in ('type','frequency_hz','duty_fraction','dc_offset_v'):
            self.assertEqual(old[field],new[field],field)
        self.assertTrue(new['spectral_vpp']['accepted'])
        self.assertLess(abs(new['vpp_v']/.8-1),.001)


if __name__=='__main__': unittest.main()
