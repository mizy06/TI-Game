import importlib.util
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location('audited', Path(__file__).resolve().parents[1] / 'tools/audited_serial_test.py')
audited = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audited)
VALID = 'RESULT,status=OK,type=square,freq_hz=250.000,vpp_v=2.000,duty_pct=50.0,score_pct=99.0,margin_pct=20.0,cycles=50'
RECEIPT = f'DATA,bytes=8192,fnv1a={audited.fnv1a(bytes(8192))}'


class FakePort:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.sent = []

    def write(self, data):
        self.sent.append(data)
        return len(data)

    def flush(self):
        pass

    def readline(self):
        return next(self.lines).encode('ascii') + b'\r\n'


class AuditedTests(unittest.TestCase):
    def test_missing_fields(self):
        for field in (',freq_hz=250.000', ',vpp_v=2.000', ',duty_pct=50.0'):
            with self.assertRaises((KeyError, ValueError)):
                audited.parse_result(VALID.replace(field, ''))

    def test_invalid_fields(self):
        for invalid in (VALID.replace('250.000', 'nan'), VALID + ',type=sine', VALID.replace('2.000', '0.000')):
            with self.assertRaises(ValueError):
                audited.parse_result(invalid)

    def test_failed_handshake_sends_no_payload(self):
        port = FakePort(['READY'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])
        self.assertEqual(port.sent, [b'W'])

    def test_transaction(self):
        port = FakePort(['READY_FOR_DATA', RECEIPT, 'PROCESSING', VALID, 'READY'])
        result, elapsed = audited.transact(port, bytes(8192), [])
        self.assertEqual(sum(map(len, port.sent)), 8193)
        self.assertEqual(result['freq_hz'], 250)
        self.assertLess(elapsed, 10)

    def test_missing_ready_fails(self):
        port = FakePort(['READY_FOR_DATA', RECEIPT, 'PROCESSING', VALID, 'ERR_TIMEOUT'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])

    def test_generation(self):
        cases = audited.make_cases()
        self.assertEqual(len(cases), 54)
        for case in cases:
            payload = audited.generate(case, 123)
            self.assertEqual(payload, audited.generate(case, 123))
            samples = struct.unpack('<4096H', payload)
            self.assertGreater(min(samples), 0)
            self.assertLess(max(samples), 4095)

    def test_corrupted_receipt_fails(self):
        port = FakePort(['READY_FOR_DATA', 'DATA,bytes=8192,fnv1a=0'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])

    def test_fnv_known_vectors(self):
        self.assertEqual(audited.fnv1a(b''), 0x811c9dc5)
        self.assertEqual(audited.fnv1a(b'hello'), 0x4f9f2cab)


if __name__ == '__main__':
    unittest.main()
