"""An interrupted USB run must retain its original calibration on resume."""
from contextlib import ExitStack
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from tools.acquisition_calibration import AcquisitionCalibration, CalibrationProfile
from tools.capture_store import CaptureStore
from tools.generator_calibration import GeneratorCalibration
from tools.ro_test import run
from tools.waveform_server import parser


class BasicResumeTests(unittest.TestCase):
    def test_resume_without_dynamic_vpp_keeps_basic_adc_and_arb_calibration(self):
        nominal = dict(sample_rate_millihz=20000000, reference_microvolts=3300000,
                       gain_ppm=1000000, adc_full_scale=4095)
        profile = CalibrationProfile(AcquisitionCalibration(19999353, 1.004, -9., nominal), {})
        playback = GeneratorCalibration(1.002, 1.001, .001)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            report = root/'incomplete-ro'
            report.mkdir()
            snapshots = {
                'matrix.json': {'cases': []},
                'results.json': {'complete': False, 'results': []},
                'calibration.json': {'complete': True},
                'generator_calibration.json': {'complete': True, 'calibration': asdict(playback)},
            }
            for name, value in snapshots.items():
                (report/name).write_text(json.dumps(value), encoding='utf-8')
            (report/'results.jsonl').write_text('', encoding='utf-8')
            service = SimpleNamespace(parameters_path=root/'parameters.json',
                use_saved_calibration=True, calibration_profile=profile,
                calibration_profile_override=None,
                publish_calibration=MagicMock(return_value=profile))
            generator = MagicMock()
            generator.__enter__.return_value = generator
            generator.transport_name = generator.transport_label = 'visa'
            generator.resource_label = 'test'
            generator.shutdown_error = generator.failure = None
            generator.connection_recoveries = 0
            generator.readback_info = {}
            stack.enter_context(patch('tools.ro_test.Generator', return_value=generator))
            stack.enter_context(patch('tools.ro_test.select_ro_cases', return_value=[]))

            def check_calibration(*args, **kwargs):
                self.assertTrue(service.use_saved_calibration)
                self.assertEqual(service.calibration_profile.calibration, profile.calibration)
                self.assertEqual(generator.playback_calibration, playback)
                self.assertIsNone(generator.playback_transfer)

            run_cases = stack.enter_context(patch('tools.ro_test._run_cases', side_effect=check_calibration))
            args = parser().parse_args(['RO', '--no-ro-vpp-calibrate', '--ro-resume-from', str(report)])
            self.assertEqual(run(service, CaptureStore(root/'data'), args, io.StringIO()), 0)
            run_cases.assert_called_once()


if __name__ == '__main__':
    unittest.main()
