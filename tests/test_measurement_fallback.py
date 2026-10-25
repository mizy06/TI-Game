import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tools import host_waveform as host
from tools.measurement_fallback import estimate_fallback
from tools.precision_batch import measure_batch
from tools.self_test import Case, generate
from tools.unified_solver import SolverConfig
from tools.waveform_server import Engine
from replay_fixture import ReplayService


class MeasurementFallbackTests(unittest.TestCase):
    def test_model_rejection_and_timeout_recover_all_four_families(self):
        errors = [ValueError('model rejected'), TimeoutError('solver deadline'),
                  np.linalg.LinAlgError('singular'), FloatingPointError('numerical failure')]
        for wave, error in zip(host.NAMES[:4], errors):
            for noise in (.1, .4):
                with self.subTest(wave=wave, noise=noise):
                    payload, meta, _ = generate(Case(wave=wave, frequency_hz=237.193,
                        vpp_v=1.517, phase=.619, duty=.63173, rise=.037, fall=.081,
                        noise_pp_ratio=noise))
                    with patch('tools.unified_solver.estimate', side_effect=error):
                        line, result = host.analyze(None, payload, meta, config=SolverConfig(max_seconds=2.))
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(line.split(',')[0], wave)
                    self.assertLess(abs(result['frequency_hz']/237.193-1), .002)
                    self.assertLess(abs(result['vpp_v']/1.517-1), .01)
                    self.assertTrue(result['fallback']['used'])
                    self.assertFalse(result['vpp_quality']['accuracy_0p1_percent_certified'])
                    json.dumps(result, allow_nan=False)

    def test_invalid_codes_metadata_and_clipped_input_are_not_invented(self):
        payload, meta, _ = generate(Case(noise_pp_ratio=0.))
        raw = np.frombuffer(payload, dtype='<u2').copy()
        bad = raw.copy()
        bad[0] = 4096
        clipped = raw.copy()
        clipped[:len(raw)//3] = 4095
        flat = np.full_like(raw, 2048)
        for samples, metadata, status in [(bad, meta, 6), (raw, replace(meta, flags=0), 6),
                                           (clipped, meta, 2), (flat, meta, 1)]:
            with self.subTest(status=status), patch('tools.measurement_fallback.estimate_fallback') as fallback:
                _, result = host.analyze(None, samples.tobytes(), metadata)
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], status)
                fallback.assert_not_called()
                if status in (1, 2):
                    self.assertIn('observed_input_span_v', result['partial_measurement'])
                json.dumps(result, allow_nan=False)

    def test_no_signal_cannot_create_a_periodic_source(self):
        with self.assertRaisesRegex(ValueError, 'flat'):
            estimate_fallback(np.ones(4096), 20000.)
        rng = np.random.default_rng(35179)
        with self.assertRaisesRegex(ValueError, 'no observable'):
            estimate_fallback(rng.normal(size=4096), 20000., max_seconds=1.)

    def batch(self, fits):
        payload, meta, _ = generate(Case(noise_pp_ratio=0.))
        engine = Engine(None, output=io.StringIO())
        service = ReplayService(engine, Path('.'), [(payload, meta, Path(str(i))) for i in range(len(fits))])
        with patch('tools.precision_batch.prepare_analysis', side_effect=lambda p, m, *_: (p, m)), \
                patch('tools.host_waveform.analyze', side_effect=fits):
            batch = measure_batch(service, frames=len(fits))
        self.assertEqual(len(engine.output.getvalue().splitlines()), 1)
        json.dumps(batch, allow_nan=False)
        return batch, service

    @staticmethod
    def fitted(kind=0, vpp=1.):
        details = dict(ok=True, status=0, type=kind, vpp_v=vpp, frequency_hz=200.,
                       duty_fraction=.5 if kind == 1 else 0., dc_offset_v=1.65,
                       measurement_sample_count=4096, classification_sample_count=4096)
        return f'{host.NAMES[kind]},200,{vpp},0,1.65', details

    def test_rejected_frame_does_not_discard_valid_siblings(self):
        rejected = ('unknown,0,0,0,0', dict(ok=False, type=4, status=6, rejection_reason='bad ADC code'))
        batch, service = self.batch([self.fitted(vpp=1.001), rejected, self.fitted(vpp=1.003)])
        self.assertEqual(batch['frames'], 2)
        self.assertAlmostEqual(batch['measured']['vpp_v'], 1.002)
        self.assertEqual(batch['frame_selection']['used_indices'], [1, 3])
        self.assertTrue(batch['fallback_used'])
        self.assertEqual(len(batch['captures']), 3)
        self.assertEqual(service.records[1].info['algorithm']['status'], 6)

    def test_classification_conflict_uses_majority_and_retains_exclusion(self):
        batch, _ = self.batch([self.fitted(), self.fitted(2), self.fitted(2)])
        self.assertEqual(batch['output'].split(',')[0], 'triangle')
        self.assertEqual(batch['frame_selection']['used_indices'], [2, 3])

    def test_nonstationary_values_select_an_actual_consensus_frame(self):
        batch, _ = self.batch([self.fitted(vpp=1.), self.fitted(vpp=1.8), self.fitted(vpp=1.01)])
        self.assertEqual(batch['frames'], 1)
        self.assertEqual(batch['measured']['vpp_v'], 1.01)
        self.assertEqual(batch['frame_selection']['method'], 'nonstationary_consensus_frame')
        self.assertEqual(batch['frame_selection']['observed_vpp_range_v'], [1., 1.8])

    def test_parallel_batch_keeps_good_frames_after_a_hard_frame_rejection(self):
        payload, meta, _ = generate(Case(frequency_hz=237.193, noise_pp_ratio=0.))
        bad = np.frombuffer(payload, dtype='<u2').copy()
        bad[19] = 4096
        engine = Engine(None, output=io.StringIO(), workers=2,
                        config=SolverConfig(max_seconds=5.))
        captures = [(payload, meta, Path('0')), (bad.tobytes(), meta, Path('1')),
                    (payload, meta, Path('2'))]
        service = ReplayService(engine, Path('.'), captures)
        try:
            engine.warm()
            batch = measure_batch(service, frames=3, timeout=14.)
            self.assertEqual(batch['frames'], 2)
            self.assertEqual(batch['frame_selection']['used_indices'], [1, 3])
            self.assertEqual(service.records[1].info['algorithm']['status'], 6)
            self.assertEqual(len(engine.output.getvalue().splitlines()), 1)
            self.assertTrue(all(p.is_alive() for p in engine.parallel_pool._processes))
        finally:
            engine.close()
