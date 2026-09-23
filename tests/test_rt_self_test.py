"""RT self-test protocol parsing without a connected board."""

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'rt_self_test', ROOT / 'tools' / 'rt_self_test.py')
rt_self_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt_self_test)


class RTSelfTestProtocolTests(unittest.TestCase):
    def test_case_index_accepts_fractional_and_integer_forms(self):
        self.assertEqual(rt_self_test.parse_case_index({'index': '1/12'}), 1)
        self.assertEqual(rt_self_test.parse_case_index({'index': '12/12'}), 12)
        self.assertEqual(rt_self_test.parse_case_index({'index': '7'}), 7)

    def test_case_index_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            rt_self_test.parse_case_index({'index': '13/12'})

    def test_na_optional_integer_uses_default(self):
        self.assertIsNone(rt_self_test.as_int({'duty_ppm': 'NA'}, 'duty_ppm'))
        self.assertEqual(rt_self_test.as_int({'cycles': '41'}, 'cycles'), 41)


if __name__ == '__main__':
    unittest.main()
