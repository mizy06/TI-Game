"""Parallel fits preserve measurements, one-result semantics and hard cancellation."""
import io
import tempfile
import threading
import time
import unittest

import numpy as np
from tools import host_waveform as host
from tools.capture_store import CaptureStore
from tools.precision_batch import measure_batch
from tools.self_test import Case, generate
from tools.unified_solver import SolverConfig
from tools.waveform_server import Engine, SerialService, parser
from test_host_integration import response
from test_server import LiveSerial


FIELDS = ('ok', 'status', 'type', 'frequency_hz', 'vpp_v', 'duty_fraction', 'dc_offset_v')


class ParallelAnalysisTests(unittest.TestCase):
    def test_parallel_real_fits_match_serial_for_all_waveforms_in_acquisition_order(self):
        from replay_fixture import ReplayService
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            root = Path(directory)
            captures = []
            expected = []
            config = SolverConfig(max_seconds=10.)
            for i, wave in enumerate(host.NAMES[:4]):
                payload, meta, _ = generate(Case(wave=wave, frequency_hz=237.193,
                    phase=.619, noise_pp_ratio=0., duty=.63173, rise=.037, fall=.081))
                captures.append((payload, meta, root/str(i)))
                expected.append(host.analyze(None, payload, meta, config=config))
            engine = Engine(None, io.StringIO(), config=config, workers=3)
            try:
                engine.warm()
                service = ReplayService(engine, root, captures)
                prepared = [service.capture(14.) for _ in captures]
                fitted, elapsed = engine.process_frames(prepared, deadline=time.perf_counter()+14.)
                self.assertEqual(engine.output.getvalue(), '')
                for (line, actual, _), (old_line, old) in zip(fitted, expected):
                    self.assertEqual(line, old_line)
                    for field in FIELDS:
                        self.assertEqual(actual[field], old[field], field)
                self.assertGreater(elapsed, 0)
            finally:
                engine.close()

    def test_parallel_batch_emits_once_and_keeps_all_raw_frames(self):
        payload, _, _ = generate(Case(frequency_hz=217.139, noise_pp_ratio=0.))
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            engine = Engine(None, output, workers=3)
            service = None
            try:
                engine.warm()
                store = CaptureStore(directory)
                port = LiveSerial(response(payload))
                service = SerialService(port, store, engine, stderr=io.StringIO())
                batch = measure_batch(service, frames=3, timeout=14.)
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertEqual(bytes(port.written), b'RRR')
                self.assertEqual(batch['analysis_execution'], 'parallel_processes')
                self.assertEqual(batch['analysis_workers'], 3)
                self.assertEqual(len(batch['captures']), 3)
                for name in batch['captures']:
                    self.assertEqual((store.path/name/'samples.adc.bin').read_bytes(), payload)
            finally:
                if service:
                    service.close()
                engine.close()

    def test_deadline_terminates_all_fits_without_a_late_result_and_pool_recovers(self):
        from replay_fixture import ReplayService
        from pathlib import Path
        payload, meta, _ = generate(Case(wave='square', frequency_hz=237.193, noise_pp_ratio=.2))
        engine = Engine(None, io.StringIO(), workers=3)
        try:
            engine.warm()
            service = ReplayService(engine, Path('.'), [(payload, meta, Path(str(i))) for i in range(3)])
            captures = [service.capture(14.) for _ in range(3)]
            with self.assertRaises(TimeoutError):
                engine.process_frames(captures, deadline=time.perf_counter()+.01)
            self.assertEqual(engine.output.getvalue(), '')
            self.assertEqual(engine.parallel_pool._processes, [])
            self.assertTrue(all(r.info['expired_request'] for _, _, r in captures))
            engine.warm()
            fitted, _ = engine.process_frames(captures, deadline=time.perf_counter()+14.)
            self.assertTrue(all(line.startswith('square,') for line, _, _ in fitted))
        finally:
            engine.close()

    def test_rejected_frame_cancels_siblings_and_never_emits(self):
        from replay_fixture import ReplayService
        from pathlib import Path
        payload, meta, _ = generate(Case(noise_pp_ratio=0.))
        raw = np.frombuffer(payload, dtype='<u2').copy()
        raw[19] = 4096
        captures = [(payload, meta, Path('0')), (raw.tobytes(), meta, Path('1')),
                    (payload, meta, Path('2'))]
        engine = Engine(None, io.StringIO(), workers=2)
        try:
            engine.warm()
            service = ReplayService(engine, Path('.'), captures)
            with self.assertRaisesRegex(ValueError, 'block 2 rejected'):
                engine.process_frames([service.capture(14.) for _ in captures], deadline=time.perf_counter()+14.)
            self.assertEqual(engine.output.getvalue(), '')
            self.assertEqual(service.records[1].info['algorithm']['status'], 6)
            self.assertEqual(engine.parallel_pool._processes, [])
        finally:
            engine.close()

    def test_cli_defaults_to_three_workers_and_has_serial_switch(self):
        self.assertEqual(parser().parse_args(['R']).r_workers, 3)
        self.assertEqual(parser().parse_args(['R', '--r-workers', '1']).r_workers, 1)

    def test_worker_exit_cancels_request_without_emitting(self):
        from replay_fixture import ReplayService
        from pathlib import Path
        payload, meta, _ = generate(Case(frequency_hz=237.193, noise_pp_ratio=.2))
        engine = Engine(None, io.StringIO(), workers=3)
        timer = None
        try:
            engine.warm()
            service = ReplayService(engine, Path('.'), [(payload, meta, Path(str(i))) for i in range(3)])
            timer = threading.Timer(.1, engine.parallel_pool._processes[0].terminate)
            timer.start()
            with self.assertRaisesRegex(RuntimeError, 'worker exited'):
                engine.process_frames([service.capture(14.) for _ in range(3)], deadline=time.perf_counter()+14.)
            self.assertEqual(engine.output.getvalue(), '')
            self.assertEqual(engine.parallel_pool._processes, [])
        finally:
            if timer:
                timer.cancel()
                timer.join()
            engine.close()


if __name__ == '__main__':
    unittest.main()
