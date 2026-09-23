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
    def test_algorithm_commands_are_explicit(self):
        self.assertEqual(rt_self_test.command_for_algorithm('1'), 'RT1')
        self.assertEqual(rt_self_test.command_for_algorithm('2'), 'RT2')
        with self.assertRaises(ValueError):
            rt_self_test.command_for_algorithm('both')

    def test_case_index_accepts_fractional_and_integer_forms(self):
        self.assertEqual(rt_self_test.parse_case_index({'index': '1/296'}), 1)
        self.assertEqual(rt_self_test.parse_case_index({'index': '296/296'}), 296)
        self.assertEqual(rt_self_test.parse_case_index({'index': '7'}), 7)

    def test_case_index_rejects_out_of_range_values(self):
        with self.assertRaises(ValueError):
            rt_self_test.parse_case_index({'index': '297/296'})

    def test_na_optional_integer_uses_default(self):
        self.assertIsNone(rt_self_test.as_int({'duty_ppm': 'NA'}, 'duty_ppm'))
        self.assertEqual(rt_self_test.as_int({'cycles': '41'}, 'cycles'), 41)

    def test_matrix_contract_rejects_old_twelve_case_firmware(self):
        start = {'cases': '12', 'custom': '12', 'challenge': '0'}
        error = rt_self_test.validate_matrix_contract(start, [], {}, None)
        self.assertTrue(error.startswith('WRONG_MATRIX'))

    def test_matrix_contract_accepts_exact_200_plus_96(self):
        start = {'algorithm': '2', 'cases': '296', 'custom': '200',
                 'challenge': '96'}
        cases = ([{'group': 'custom'}] * 200
                 + [{'group': 'challenge'}] * 96)
        summary = {
            'pass': '295', 'fail': '1',
            'custom_pass': '200', 'custom_fail': '0',
            'challenge_pass': '95', 'challenge_fail': '1',
        }
        self.assertIsNone(
            rt_self_test.validate_matrix_contract(start, cases, summary, None))

    def test_matrix_contract_rejects_wrong_algorithm(self):
        start = {'algorithm': '1', 'cases': '296', 'custom': '200',
                 'challenge': '96'}
        cases = ([{'group': 'custom'}] * 200
                 + [{'group': 'challenge'}] * 96)
        summary = {
            'pass': '295', 'fail': '1',
            'custom_pass': '200', 'custom_fail': '0',
            'challenge_pass': '95', 'challenge_fail': '1',
        }
        self.assertEqual(
            rt_self_test.validate_matrix_contract(
                start, cases, summary, None, expected_algorithm='2'),
            'WRONG_ALGORITHM(1)')


if __name__ == '__main__':
    unittest.main()
