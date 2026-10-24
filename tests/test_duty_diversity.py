"""Clock and phase coverage regressions for fractional square duty."""
from dataclasses import replace
import io
import tempfile
import unittest

import numpy as np

from tools.acquisition_calibration import AcquisitionCalibration, signature
from tools.host_waveform import Metadata, fnv1a
from tools.precision_batch import _combine_diverse_duty


class DutyDiversityTests(unittest.TestCase):
    def test_same_clock_scaling_preserves_samples_and_voltage_calibration(self):
        payload = np.full(8192, 2048, dtype='<u2').tobytes()
        meta = Metadata(1, 20000000, 3300000, 1000000, 4095, 9, fnv1a(payload),
                        payload_bytes=len(payload), protocol_version=3)
        calibration = AcquisitionCalibration(19999368, 1.003, -7., signature(meta),
                                              sample_rate_hz_exact=19999.3678)
        for ticks in (1500, 1597, 1613, 1679, 1700):
            wire = replace(meta, sample_rate_millihz=round(32000000000/ticks))
            original, measured = calibration.apply_exact(payload, wire)
            self.assertIs(original, payload)
            self.assertAlmostEqual(measured.sample_rate_hz_exact,
                                   19999.3678*1600/ticks, places=9)
            self.assertEqual(measured.adc_scale, calibration.adc_scale)
            self.assertEqual(measured.adc_offset_counts, calibration.adc_offset_counts)
            with self.assertRaisesRegex(ValueError, 'metadata changed'):
                calibration.apply_exact(payload, replace(wire, flags=1))
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            calibration.apply_exact(payload, replace(meta, sample_rate_millihz=20001000))
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            calibration.apply_exact(payload, replace(meta, gain_ppm=1000001))

    def test_unobserved_fractional_edge_does_not_dominate_diverse_frames(self):
        from tools.unified_solver import estimate
        n, duty, phase = 8192, .301, .273
        rates = [20000., 32000000/1597, 32000000/1613]
        prepared, results, values = [], [], []
        for rate in rates:
            p = (np.arange(n)*200/rate+phase) % 1.
            payload = np.where(p < duty, 2800, 1000).astype('<u2').tobytes()
            meta = Metadata(1, round(rate*1000), 3300000, 1000000, 4095, 9,
                            fnv1a(payload), sample_rate_hz_exact=rate, payload_bytes=len(payload))
            prepared.append((payload, meta, None))
            result = estimate(np.frombuffer(payload, dtype='<u2'), rate, quantization_step=1.)
            self.assertEqual(result['name'], 'square')
            results.append(result)
            values.append([result['frequency_hz'], result['vpp'], result['duty_fraction'], result['dc']])
        locked = np.arange(n)*200/20000.+phase
        np.testing.assert_array_equal((locked % 1.) < .3, (locked % 1.) < .301)
        selected, info, reliable = _combine_diverse_duty(np.asarray(values), prepared, results)
        self.assertLess(abs(selected/duty-1), .001)
        self.assertLess(info['weights'][0], .01)
        self.assertEqual(len(reliable), 2)

    def test_equal_rates_preserve_the_original_aggregation(self):
        payload = bytes(16384)
        meta = Metadata(1, 20000000, 3300000, 1000000, 4095, 1, 0)
        values = np.array([[200., 1.4, .3, 1.5], [200., 1.4, .302, 1.5]])
        duty, info, _ = _combine_diverse_duty(values, [(payload, meta, None)]*2,
                                             [dict(frequency_hz=200.)]*2)
        self.assertEqual(duty, .301)
        self.assertIsNone(info)

    def test_biased_frequency_cannot_hide_a_blind_lattice(self):
        # USB regression: the third fit overestimated frequency by 0.008 Hz,
        # making its own phases look dense while the two other fits expose
        # the uncovered gap. It must not dominate the good first frame.
        rates = [19999.34686296197, 20036.91608061312, 19838.16179835037]
        frequencies = [350.49953181266903, 350.49993624382637, 350.5080910342619]
        duties = [.20113073064939258, .20116569209375001, .19963831860973555]
        values = np.column_stack((frequencies, np.full(3, 1.4), duties, np.full(3, 1.5)))
        prepared = [(bytes(16384), Metadata(1, round(rate*1000), 3300000,
            1000000, 4095, 9, 0, sample_rate_hz_exact=rate), None) for rate in rates]
        duty, info, _ = _combine_diverse_duty(values, prepared,
            [dict(frequency_hz=f) for f in frequencies])
        self.assertLess(abs(duty/.201-1), .001)
        self.assertLess(info['weights'][2], .01)

    def test_edge_intervals_bound_fractional_duty_without_frequency_truth(self):
        from tools.square_duty import edge_interval_duty
        prepared = []
        duty, frequency = .201, 200.36916
        for ticks, phase in zip((1600, 1597, 1575, 1679), (.17, .43, .71, .93)):
            rate = 32000000/ticks
            p = (np.arange(8192)*frequency/rate+phase) % 1.
            # Low-band interference never crosses the definite plateau guard.
            noise = 70*np.sin(np.arange(8192)*.021+phase)
            samples = np.rint(np.where(p < duty, 2800, 1000)+noise).astype('<u2')
            meta = Metadata(1, round(rate*1000), 3300000, 1000000, 4095, 9, 0,
                            sample_rate_hz_exact=rate)
            prepared.append((samples.tobytes(), meta, None))
        selected, info = edge_interval_duty(prepared, [])
        self.assertIsNotNone(selected)
        self.assertLessEqual(info['duty_lower'], duty)
        self.assertGreaterEqual(info['duty_upper'], duty)
        self.assertLess(abs(selected/duty-1), .001)
        self.assertLessEqual(info['frequency_bounds_hz'][0], frequency)
        self.assertGreaterEqual(info['frequency_bounds_hz'][1], frequency)

    def test_smooth_wave_does_not_receive_a_square_edge_bound(self):
        from tools.square_duty import edge_interval_duty
        y = np.rint(1800+800*np.sin(np.arange(8192)*2*np.pi*.01)).astype('<u2')
        meta = Metadata(1, 20000000, 3300000, 1000000, 4095, 9, 0)
        selected, info = edge_interval_duty([(y.tobytes(), meta, None)]*3, [])
        self.assertIsNone(selected)

    def test_fractional_sample_edge_jitter_does_not_force_a_biased_fallback(self):
        from tools.square_duty import edge_interval_duty
        prepared = []
        frequency, duty = 328.68709354280753, .6428152846289388
        for ticks, phase in zip((1600, 1597, 1570), (.17, .43, .71)):
            rate = 32000000/ticks
            # Independent DAC updates quantize the continuous edge time, as
            # in USB ARB playback. They do not alter the intended duty.
            time_s = np.arange(8192)/rate+phase/frequency
            source_rate = frequency*8192/135*32
            source_time = np.floor(time_s*source_rate)/source_rate
            p = (source_time*frequency) % 1.
            samples = np.where(p < duty, 2800, 1000).astype('<u2')
            meta = Metadata(1, round(rate*1000), 3300000, 1000000, 4095, 9, 0,
                            sample_rate_hz_exact=rate)
            prepared.append((samples.tobytes(), meta, None))
        selected, info = edge_interval_duty(prepared, [])
        self.assertIsNotNone(selected)
        self.assertLess(abs(selected/duty-1), .001)
        self.assertLessEqual(info['timing_slack_samples'], .02)

    def test_large_phase_discontinuity_rejects_the_periodic_edge_model(self):
        from tools.square_duty import edge_interval_duty
        rate = 20000.
        phase = np.arange(8192)*200.36916/rate+.17
        phase[4096:] += .02
        samples = np.where(phase % 1. < .301, 2800, 1000).astype('<u2')
        meta = Metadata(1, 20000000, 3300000, 1000000, 4095, 9, 0,
                        sample_rate_hz_exact=rate)
        selected, info = edge_interval_duty([(samples.tobytes(), meta, None)], [])
        self.assertIsNone(selected)
        self.assertEqual(info['reason'], 'excessive_edge_timing_variation')

    def test_wide_common_edge_interval_survives_a_biased_spectral_frame(self):
        from pathlib import Path
        import threading
        from types import SimpleNamespace
        from tools.capture_store import CaptureStore
        from tools.precision_batch import measure_batch

        frequency, duty = 272.6553, .20189439525892275
        phases = [.3374538613219561, .6972562877984159,
                  .46590642682895644, .8373504166460422]
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            captures = []
            for ticks, phase in zip((1600, 1597, 1565, 1544), phases):
                rate = 32000000/ticks
                t = np.arange(8192)/rate+phase/frequency
                source_t = np.floor(t*(19943.4*32))/(19943.4*32)
                samples = np.where(source_t*frequency % 1. < duty, 2800, 1000).astype('<u2')
                raw = samples.tobytes()
                meta = Metadata(1, round(rate*1000), 3300000, 1000000, 4095, 9, 0,
                                sample_rate_hz_exact=rate)
                captures.append((raw, meta, store.begin('fixture')))
            pending = iter(captures)

            def fitted(payload, meta, record, **kwargs):
                # A bias of 0.1 percentage points in the dense fourth lattice
                # must not displace the estimate supported by all raw edges.
                estimated = duty+.001 if meta.sample_rate_hz_exact > 20700 else duty
                return '', dict(ok=True, type=1, frequency_hz=frequency, vpp_v=1.4,
                    duty_fraction=estimated, dc_offset_v=1.5,
                    measurement_sample_count=8192, classification_sample_count=8192), .001

            engine = SimpleNamespace(parallel_pool=None, output=io.StringIO(), process=fitted)
            service = SimpleNamespace(engine=engine, store=store,
                request_lock=threading.RLock(), use_saved_calibration=False,
                parameters_path=Path(directory)/'absent.json', capture=lambda _:next(pending))
            result = measure_batch(service, 4, 15.)
            interval = result['duty_phase_coverage']['edge_intervals']
            self.assertFalse(interval['accepted'])
            self.assertTrue(interval['selected_as_point_estimate'])
            self.assertLess(abs(result['measured']['duty_fraction']/duty-1), .001)

    def test_advertised_rates_are_requested_and_archived_with_their_own_timebase(self):
        from pathlib import Path
        from tools.capture_store import CaptureStore
        from tools.precision_batch import measure_batch
        from tools.waveform_server import Engine, SerialService
        from test_host_integration import response
        from test_server import LiveSerial

        class DiverseSerial(LiveSerial):
            def write(self, command):
                ticks = (int(command[1:5]) if command.startswith(b'U') else
                         {b'R': 1600, b'S': 1597, b'T': 1613}[command])
                rate = 32000000 / ticks
                phase = (np.arange(8192)*200/rate+.273) % 1.
                payload = np.where(phase < .301, 2800, 1000).astype('<u2').tobytes()
                self.reply = response(payload).replace(
                    b'proto=2,bytes=8192,', b'proto=3,bytes=16384,').replace(
                    b'fs_millihz=20000000,', f'fs_millihz={round(rate*1000)},'.encode()).replace(
                    b'flags=1\r\n', b'flags=9\r\n')
                return super().write(command)

        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            engine = Engine(None, io.StringIO())
            port = DiverseSerial(b'')
            service = SerialService(port, store, engine, stderr=io.StringIO(),
                                    parameters_path=Path(directory)/'absent.json')
            try:
                batch = measure_batch(service, 3, 15.)
                self.assertTrue(bytes(port.written).startswith(b'RSU'))
                self.assertLess(abs(batch['measured']['duty_fraction']/.301-1), .001)
                import json
                rates = []
                for capture in batch['captures']:
                    path = store.path/capture
                    saved = json.loads((path/'capture.json').read_text())
                    rates.append(saved['metadata']['sample_rate_millihz'])
                    self.assertEqual(len((path/'samples.adc.bin').read_bytes()), 16384)
                ticks = batch['duty_sampling_plan']['timer_ticks']
                self.assertEqual(rates, [round(32000000000/t) for t in (1600, 1597, ticks)])
                self.assertEqual(len(engine.output.getvalue().splitlines()), 1)
            finally:
                service.close()
                engine.close()

    def test_sine_with_rate_capability_keeps_original_three_r_commands(self):
        from pathlib import Path
        from tools.capture_store import CaptureStore
        from tools.precision_batch import measure_batch
        from tools.waveform_server import Engine, SerialService
        from test_host_integration import response
        from test_server import LiveSerial

        samples = np.rint(1800+800*np.sin(np.arange(8192)*2*np.pi*.01)).astype('<u2')
        reply = response(samples.tobytes()).replace(
            b'proto=2,bytes=8192,', b'proto=3,bytes=16384,').replace(
            b'flags=1\r\n', b'flags=9\r\n')
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(None, io.StringIO())
            port = LiveSerial(reply)
            service = SerialService(port, CaptureStore(directory), engine, stderr=io.StringIO(),
                                    parameters_path=Path(directory)/'absent.json')
            try:
                batch = measure_batch(service, 3, 15.)
                self.assertEqual(bytes(port.written), b'RRR')
                self.assertIsNone(batch['duty_sampling_plan'])
                self.assertTrue(batch['output'].startswith('sine,'))
            finally:
                service.close()
                engine.close()


if __name__ == '__main__':
    unittest.main()
