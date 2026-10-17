"""Current entry-point defaults and calibration-only isolation/recovery."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.capture_store import CaptureStore
from tools.ro_test import run, select_ro_cases
from tools.waveform_server import parser
from tools.waveform_server import run_server


class CommandTests(unittest.TestCase):
    def test_current_defaults_and_matrix(self):
        for command in ('R', 'RO', 'RT', 'RS'):
            args = parser().parse_args([command])
            self.assertEqual((args.r_frames, args.r_workers), (3, 3))
            self.assertEqual(args.ro_calibrate, 'saved')
            self.assertEqual(args.ro_transport, 'visa')
            self.assertEqual(args.ro_oversample, 32)
            self.assertTrue(args.ro_vpp_calibrate)
        cases = select_ro_cases(parser().parse_args(['RO']))
        self.assertEqual(len(cases), 296)
        self.assertEqual(sum(c.group == 'final_weak' for c in cases), 148)
        self.assertTrue(all(100 <= c.frequency_hz <= 600 for c in cases))

    def test_rs_requires_board(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'RS requires'):
                run(None, CaptureStore(directory), parser().parse_args(['RS']),
                    io.StringIO(), calibration_only=True)

    def exercise_rs(self, fail):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            parameters = Path(directory) / 'algorithm_parameters.json'
            parameters.write_text('original profile', encoding='utf-8')
            profile = object()
            service = SimpleNamespace(parameters_path=parameters, calibration_profile=profile,
                                      use_saved_calibration=True)
            store = CaptureStore(directory)
            generator = MagicMock()
            generator.__enter__.return_value = generator
            generator.transport_name = 'visa'
            generator.transport_label = 'visa'
            generator.resource_label = 'test'
            generator.shutdown_error = None
            generator.failure = None
            generator.readback_info = {}
            stack.enter_context(patch('tools.ro_test.Generator', return_value=generator))
            selection = stack.enter_context(patch('tools.ro_test.select_ro_cases', side_effect=AssertionError('matrix selected')))
            cases = stack.enter_context(patch('tools.ro_test._run_cases', side_effect=AssertionError('matrix run')))
            adc = stack.enter_context(patch('tools.ro_test.calibrate_link', return_value={'complete': True}))
            stack.enter_context(patch('tools.ro_test.calibrate_playback', return_value={'complete': True}))
            def transfer(*args):
                parameters.write_text('new profile', encoding='utf-8')
                service.calibration_profile = 'new'
                if fail:
                    raise ValueError('calibration interrupted')
            dense = stack.enter_context(patch('tools.dynamic_amplitude_calibration.calibrate_adc_transfer', side_effect=transfer))
            dac = stack.enter_context(patch('tools.dynamic_amplitude_calibration.calibrate_dac_transfer'))
            args = parser().parse_args(['RS', '--ro-calibrate', 'off', '--ro-reuse-reference-report', 'ignored'])
            if fail:
                with self.assertRaisesRegex(ValueError, 'calibration interrupted'):
                    run(service, store, args, io.StringIO(), calibration_only=True)
                self.assertEqual(parameters.read_text(), 'original profile')
                self.assertIs(service.calibration_profile, profile)
                dac.assert_not_called()
            else:
                self.assertEqual(run(service, store, args, io.StringIO(), calibration_only=True), 0)
                self.assertEqual(parameters.read_text(), 'new profile')
                dac.assert_called_once()
            selection.assert_not_called()
            cases.assert_not_called()
            adc.assert_called_once()
            dense.assert_called_once()
            generator.__exit__.assert_called_once()
            report = next(store.path.glob('rs-*'))
            self.assertFalse((report / 'matrix.json').exists())
            summary = json.loads((report / 'summary.json').read_text())
            self.assertEqual(summary['mode'], 'calibration-only')
            self.assertEqual(summary['selected'], 0)
            self.assertEqual(summary['complete'], not fail)

    def test_rs_skips_matrix_and_saves_calibration(self):
        self.exercise_rs(False)

    def test_rs_interruption_keeps_previous_calibration(self):
        self.exercise_rs(True)

    def test_dispatch_routes_current_commands(self):
        for command in ('R', 'RO', 'RT', 'RS'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                args = parser().parse_args([command, '--port', 'FAKE'])
                stack.enter_context(patch('tools.waveform_server.Engine'))
                stack.enter_context(patch('serial.Serial'))
                service = stack.enter_context(patch('tools.waveform_server.SerialService')).return_value
                measure = stack.enter_context(patch('tools.precision_batch.measure_batch'))
                ro = stack.enter_context(patch('tools.ro_test.run', return_value=0))
                rt = stack.enter_context(patch('tools.self_test.run', return_value=0))
                stack.enter_context(patch('sys.stderr', io.StringIO()))
                self.assertEqual(run_server(args, CaptureStore(directory), io.StringIO()), 0)
                if command == 'R':
                    measure.assert_called_once_with(service, 3, 15.)
                elif command == 'RT':
                    rt.assert_called_once()
                else:
                    self.assertEqual(ro.call_args.kwargs['calibration_only'], command == 'RS')
                service.close.assert_called_once()

    def test_ro_reuses_saved_independent_references(self):
        from tools.acquisition_calibration import load_latest_calibration
        root = Path(__file__).resolve().parents[1]
        profile = load_latest_calibration(root / 'algorithm_parameters.json')
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            parameters = Path(directory) / 'algorithm_parameters.json'
            parameters.write_bytes((root / 'algorithm_parameters.json').read_bytes())
            service = SimpleNamespace(parameters_path=parameters, calibration_profile=profile,
                                      use_saved_calibration=True, refresh_calibration=lambda: profile,
                                      publish_calibration=MagicMock())
            generator = MagicMock()
            generator.__enter__.return_value = generator
            generator.transport_name = generator.transport_label = 'visa'
            generator.resource_label = 'test'
            generator.shutdown_error = generator.failure = None
            generator.readback_info = {}
            stack.enter_context(patch('tools.ro_test.Generator', return_value=generator))
            stack.enter_context(patch('tools.ro_test.select_ro_cases', return_value=[]))
            run_cases = stack.enter_context(patch('tools.ro_test._run_cases'))
            adc = stack.enter_context(patch('tools.ro_test.calibrate_link'))
            playback = stack.enter_context(patch('tools.ro_test.calibrate_playback'))
            self.assertEqual(run(service, CaptureStore(directory), parser().parse_args(['RO']), io.StringIO()), 0)
            adc.assert_not_called()
            playback.assert_not_called()
            run_cases.assert_called_once()
            service.publish_calibration.assert_called_once()
            self.assertIsNotNone(generator.playback_transfer)


if __name__ == '__main__':
    unittest.main()
