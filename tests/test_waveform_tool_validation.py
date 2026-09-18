import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


serial_tool = load_tool("test_serial_waveform")
simulate_tool = load_tool("simulate_serial_waveform")


class WaveformToolValidationTests(unittest.TestCase):
    def test_legacy_parser_rejects_partial_success(self):
        line = (
            "RESULT,status=OK,type=square,freq_hz=250.000,"
            "duty_pct=50.0,score_pct=99.0,margin_pct=20.0,cycles=50"
        )
        self.assertIsNone(serial_tool.parse_result_line(line))

    def test_legacy_parser_rejects_trailing_text(self):
        line = (
            "RESULT,status=OK,type=square,freq_hz=250.000,vpp_v=2.000,"
            "duty_pct=50.0,score_pct=99.0,margin_pct=20.0,cycles=50,garbage"
        )
        self.assertIsNone(serial_tool.parse_result_line(line))

    def test_evaluator_does_not_pass_missing_measurements(self):
        ground_truth = {"type": "sine", "freq_hz": 250.0, "vpp_v": 2.0,
                        "duty_pct": None}
        result = {"status": "OK", "type": "sine", "freq_hz": None,
                  "vpp_v": None, "duty_pct": None}
        passed, details = simulate_tool.evaluate_case(ground_truth, result)
        self.assertFalse(passed)
        self.assertEqual(details["freq_err_pct"], 100.0)
        self.assertEqual(details["vpp_err_pct"], 100.0)

    def test_evaluator_requires_square_duty(self):
        ground_truth = {"type": "square", "freq_hz": 250.0, "vpp_v": 2.0,
                        "duty_pct": 50.0}
        result = {"status": "OK", "type": "square", "freq_hz": 250.0,
                  "vpp_v": 2.0, "duty_pct": None}
        passed, _ = simulate_tool.evaluate_case(ground_truth, result)
        self.assertFalse(passed)


if __name__ == "__main__":
    unittest.main()
