"""Independent physical-model and multi-block safety regressions."""
import io
import tempfile
import unittest
from dataclasses import replace
import numpy as np
from tools import host_waveform as host
from tools.acquisition_calibration import AcquisitionCalibration,signature
from tools.precision_batch import measure_batch
from tools.capture_store import CaptureStore
from tools.self_test import Case,generate
from tools.waveform_server import Engine,SerialService
from test_host_integration import response,PartialSerial
from test_server import LiveSerial


class PrecisionPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api=host.load_api(str(host.ROOT/host.LIBRARY_NAME))

    def test_unknown_fractional_frequency_and_unequal_square_edges(self):
        for wave in ['sine','triangle','sawtooth','square']:
            case=Case(wave=wave,frequency_hz=237.193,phase=.619,
                vpp_v=1.517,dc_v=1.673,fs_hz=21317.173,
                noise_pp_ratio=0.,duty=.63173,rise=.037,fall=.081)
            payload,meta,_=generate(case)
            meta=replace(meta,sample_rate_hz_exact=case.fs_hz)
            line,details=host.analyze(self.api,payload,meta)
            with self.subTest(wave=wave):
                self.assertTrue(details['ok'])
                self.assertEqual(details['type'],host.NAMES.index(wave))
                self.assertLess(abs(details['frequency_hz']/case.frequency_hz-1),2e-6)
                self.assertLess(abs(details['vpp_v']/case.vpp_v-1),5e-4)
                self.assertAlmostEqual(float(line.split(',')[1]),details['frequency_hz'],delta=5.1e-10)
                if wave=='square':
                    # 12-bit deterministic quantization is still physical input
                    # uncertainty; unequal edges must remain within 10 fraction ppm.
                    self.assertLess(abs(details['duty_fraction']-case.duty),10e-6)

    def test_v3_8192_points_are_read_and_all_points_enter_the_estimator(self):
        f,fs=217.139,20244.675232784888
        t=np.arange(8192)/fs
        raw=np.rint((1.673+.7585*np.sin(2*np.pi*f*t+.719))*4095/3.3).astype('<u2')
        payload=raw.tobytes()
        wire=response(payload).replace(b'proto=2,bytes=8192,',b'proto=3,bytes=16384,')
        received,meta,_=host.receive_frame(PartialSerial(wire))
        self.assertEqual(received,payload)
        self.assertEqual(meta.payload_bytes,16384)
        meta=replace(meta,sample_rate_millihz=round(fs*1000),sample_rate_hz_exact=fs)
        _,d=host.analyze(self.api,received,meta)
        self.assertTrue(d['ok'])
        self.assertEqual(d['measurement_sample_count'],8192)
        self.assertEqual(d['classification_sample_count'],4096)
        self.assertLess(abs(d['frequency_hz']/f-1),1e-6)
        # A fault in the second half must not hide behind the 4096-point C seed.
        raw[7000]=4096
        _,bad=host.analyze(self.api,raw.tobytes(),meta)
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['status'],6)
        with self.assertRaises(ValueError):
            host.parse_header(wire.split(b'\n')[2].decode().strip().replace('proto=3','proto=2'))

    def test_affine_calibration_keeps_fractional_samples_and_raw_rail_rejection(self):
        case=Case(frequency_hz=217.139,fs_hz=20244.675232784888,vpp_v=1.517,
                  dc_v=1.673,noise_pp_ratio=0.,phase=.719)
        payload,_,_=generate(case)
        wire=host.Metadata(17,20000000,3300000,1000000,4095,1,host.fnv1a(payload))
        cal=AcquisitionCalibration(round(case.fs_hz*1000),1.0095795279249518,
            -5.40265764508826,signature(wire),sample_rate_hz_exact=case.fs_hz)
        same,meta=cal.apply_exact(payload,wire)
        self.assertEqual(same,payload)
        self.assertEqual(meta.checksum,wire.checksum)
        _,raw=host.analyze(self.api,payload,replace(wire,sample_rate_hz_exact=case.fs_hz))
        _,corrected=host.analyze(self.api,same,meta)
        self.assertAlmostEqual(corrected['vpp_v'],raw['vpp_v']/cal.adc_scale,delta=1e-9)
        self.assertAlmostEqual(corrected['frequency_hz'],raw['frequency_hz'],delta=1e-8)
        bad=np.frombuffer(payload,dtype='<u2').copy();bad[:1000]=4095
        _,detail=host.analyze(self.api,bad.tobytes(),meta)
        self.assertFalse(detail['ok'])

    def test_batch_emits_one_result_and_preserves_all_blocks(self):
        case=Case(frequency_hz=217.139,noise_pp_ratio=.02,phase=.718)
        payload,meta,_=generate(case)
        with tempfile.TemporaryDirectory() as directory:
            store=CaptureStore(directory);output=io.StringIO()
            port=LiveSerial(response(payload))
            service=SerialService(port,store,Engine(self.api,output),stderr=io.StringIO())
            try:
                report=measure_batch(service,frames=4,timeout=10)
            finally:
                service.close()
            self.assertEqual(len(output.getvalue().splitlines()),1)
            self.assertEqual(len(report['captures']),4)
            self.assertEqual(bytes(port.written),b'RRRR')
            self.assertLess(abs(report['measured']['frequency_hz']/case.frequency_hz-1),.001)
            for path in store.path.glob('uart/*/samples.adc.bin'):
                self.assertEqual(path.read_bytes(),payload)

    def test_batch_timeout_does_not_emit_an_earlier_complete_block(self):
        case=Case(frequency_hz=217.139,noise_pp_ratio=0.)
        payload,_,_=generate(case)
        with tempfile.TemporaryDirectory() as directory:
            output=io.StringIO()
            service=SerialService(LiveSerial(response(payload)),CaptureStore(directory),
                Engine(self.api,output),stderr=io.StringIO())
            try:
                with self.assertRaises(TimeoutError):
                    measure_batch(service,frames=4,timeout=.001)
                self.assertEqual(output.getvalue(),'')
            finally:
                service.close()

if __name__=='__main__': unittest.main()
