"""audited_serial_test.py 的纯主机单元测试。

测试不需要真实串口：FakePort 按预设顺序返回文本，并记录工具写出的全部
字节。因此可以快速验证解析器拒绝坏字段、握手失败不发送负载、完整事务
字节数正确，以及波形生成和 FNV-1a 可复现。
"""

import importlib.util
from pathlib import Path
import struct
import unittest

# 工具目录不是 Python 包，所以按绝对文件路径动态导入待测模块。
spec = importlib.util.spec_from_file_location('audited', Path(__file__).resolve().parents[1] / 'tools/audited_serial_test.py')
audited = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audited)
VALID = 'RESULT,status=OK,type=square,freq_hz=250.000,vpp_v=2.000,duty_pct=50.0,score_pct=99.0,margin_pct=20.0,cycles=50'
RECEIPT = f'DATA,bytes=8192,fnv1a={audited.fnv1a(bytes(8192))}'


class FakePort:
    """实现 transact() 所需的最小 pyserial 风格接口。

    ``lines`` 是固件将依次返回的行；``sent`` 保存电脑写出的每个数据块。
    write 总是假装完整成功，专注测试上层协议状态机。
    """

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
    """可审计注入工具的协议、生成器和校验函数回归。"""

    def test_missing_fields(self):
        """OK 行缺任一关键测量字段都不能被接受。"""
        for field in (',freq_hz=250.000', ',vpp_v=2.000', ',duty_pct=50.0'):
            with self.assertRaises((KeyError, ValueError)):
                audited.parse_result(VALID.replace(field, ''))

    def test_invalid_fields(self):
        """NaN、重复键和非正 Vpp 必须报 ValueError。"""
        for invalid in (VALID.replace('250.000', 'nan'), VALID + ',type=sine', VALID.replace('2.000', '0.000')):
            with self.assertRaises(ValueError):
                audited.parse_result(invalid)

    def test_failed_handshake_sends_no_payload(self):
        """未收到 READY_FOR_DATA 时只能写出单个 W，绝不能发送二进制。"""
        port = FakePort(['READY'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])
        self.assertEqual(port.sent, [b'W'])

    def test_transaction(self):
        """正常事务发送 1 字节命令+8192 字节负载，并解析数值。"""
        port = FakePort(['READY_FOR_DATA', RECEIPT, 'PROCESSING', VALID, 'READY'])
        result, elapsed = audited.transact(port, bytes(8192), [])
        self.assertEqual(sum(map(len, port.sent)), 8193)
        self.assertEqual(result['freq_hz'], 250)
        self.assertLess(elapsed, 10)

    def test_missing_ready_fails(self):
        """RESULT 后没有终态 READY 也算协议失败，不能静默成功。"""
        port = FakePort(['READY_FOR_DATA', RECEIPT, 'PROCESSING', VALID, 'ERR_TIMEOUT'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])

    def test_generation(self):
        """固定 seed 生成相同负载，且常规用例不碰 ADC 上下限。"""
        cases = audited.make_cases()
        self.assertEqual(len(cases), 54)
        for case in cases:
            payload = audited.generate(case, 123)
            self.assertEqual(payload, audited.generate(case, 123))
            samples = struct.unpack('<4096H', payload)
            self.assertGreater(min(samples), 0)
            self.assertLess(max(samples), 4095)

    def test_corrupted_receipt_fails(self):
        """固件回执的 FNV 与本地负载不一致时必须中止。"""
        port = FakePort(['READY_FOR_DATA', 'DATA,bytes=8192,fnv1a=0'])
        with self.assertRaises(RuntimeError):
            audited.transact(port, bytes(8192), [])

    def test_fnv_known_vectors(self):
        """用公开已知向量防止 FNV 实现被无意改坏。"""
        self.assertEqual(audited.fnv1a(b''), 0x811c9dc5)
        self.assertEqual(audited.fnv1a(b'hello'), 0x4f9f2cab)


if __name__ == '__main__':
    unittest.main()
