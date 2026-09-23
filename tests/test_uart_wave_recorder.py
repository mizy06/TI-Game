"""UART 测试波记录器的无硬件回归。

全部测试都使用假串口，不打开真实 COM 口、不加载 DLL、不依赖 pyserial。
重点是"拒识帧不能丢"与"畸形行不能伪装成 unknown"这两条纪律，以及 CSV
列定义必须与既有工具保持一致，否则同一个日志文件会出现两套表头语义。
"""

import csv
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name):
    """按文件名加载 tools 下的脚本，不要求它成为可导入包。"""
    spec = importlib.util.spec_from_file_location(
        name, ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder = load_tool('record_uart_waveforms')
legacy = load_tool('test_serial_waveform')
audited = load_tool('audited_serial_test')


class FakePort:
    """最小 pyserial 替身：按脚本逐行吐字，并记录主机写入的字节。"""

    def __init__(self, lines):
        self._lines = [line.encode('ascii') + b'\r\n' for line in lines]
        self.written = bytearray()
        self._cursor = 0

    def write(self, data):
        self.written.extend(data)
        # 字节数与 pyserial 的返回值语义保持一致，便于断言短写。
        return len(data)

    def flush(self):
        return None

    def readline(self):
        if self._cursor >= len(self._lines):
            return b''
        line = self._lines[self._cursor]
        self._cursor += 1
        return line


class RecorderProtocolTests(unittest.TestCase):
    """事务顺序、拒识保留与畸形行处理。"""

    def test_ok_transaction_records_full_diagnostics(self):
        """成功帧必须带出得分、余量和周期数。"""
        line = ('RESULT,status=OK,type=sine,freq_hz=250.000,vpp_v=2.000,'
                'duty_pct=NA,score_pct=100.0,margin_pct=1.4,cycles=20,'
                'sequence=7,quality_flags=8')
        port = FakePort(['CAPTURING', 'PROCESSING', line, 'READY'])
        record = recorder.run_transaction(port, 'R', b'', 1.0)
        self.assertTrue(record['accepted'])
        self.assertEqual(record['fields']['score_pct'], '100.0')
        self.assertFalse(record['diagnostics_omitted_by_firmware'])
        # 原始字节必须留存，便于区分"没触发"和"RESULT 畸形"。
        self.assertEqual([entry['text'] for entry in record['log']],
                         ['CAPTURING', 'PROCESSING', line, 'READY'])

    def test_rejection_frame_is_preserved_not_dropped(self):
        """拒识帧是本工具存在的理由，绝不能被丢弃。"""
        line = ('RESULT,status=CLASSIFICATION_AMBIGUOUS,type=unknown,'
                'high_clip_samples=0,low_clip_samples=0,invalid_samples=0,'
                'sequence=9,quality_flags=8')
        port = FakePort(['CAPTURING', 'PROCESSING', line, 'READY'])
        record = recorder.run_transaction(port, 'R', b'', 1.0)
        self.assertFalse(record['accepted'])
        self.assertEqual(record['reason_family'], 'model_margin_gate')
        # 固件确实没有回传这三个量，标记出来供后续离线回放补全。
        self.assertTrue(record['diagnostics_omitted_by_firmware'])
        self.assertEqual(record['result_line'], line)

    def test_statusless_result_is_malformed(self):
        """缺少 status 的行不得被当成 unknown 状态掩盖过去。"""
        port = FakePort(['CAPTURING', 'PROCESSING', 'RESULT,foo=1', 'READY'])
        record = recorder.run_transaction(port, 'R', b'', 1.0)
        self.assertEqual(record['protocol_error'], 'MALFORMED_RESULT')

    def test_handshake_failure_sends_no_payload(self):
        """未收到 READY_FOR_DATA 时，一个二进制字节都不能发。"""
        port = FakePort(['READY'])
        record = recorder.run_transaction(port, 'W', b'\x01' * 8192, 1.0)
        self.assertEqual(record['protocol_error'], 'HANDSHAKE_FAILED')
        self.assertEqual(bytes(port.written), b'W')

    def test_injection_flow_checks_receipt_integrity(self):
        """回执的字节数与 FNV-1a 都必须匹配，错误回执立即终止。"""
        payload = b'\x00' * 8192
        good = f'DATA,bytes=8192,fnv1a={audited.fnv1a(payload)}'
        port = FakePort(['READY_FOR_DATA', good, 'PROCESSING',
                         'RESULT,status=SIGNAL_TOO_SMALL,type=unknown,'
                         'high_clip_samples=0,low_clip_samples=0,'
                         'invalid_samples=0,sequence=1,quality_flags=0',
                         'READY'])
        record = recorder.run_transaction(port, 'W', payload, 1.0)
        self.assertNotIn('protocol_error', record)
        self.assertEqual(record['reason_family'], 'amplitude_gate')

        bad = FakePort(['READY_FOR_DATA', 'DATA,bytes=8192,fnv1a=0'])
        failed = recorder.run_transaction(bad, 'W', payload, 1.0)
        self.assertEqual(failed['protocol_error'], 'DATA_INTEGRITY')


class RecorderContractTests(unittest.TestCase):
    """端口校验、状态归类与 CSV 列约定的外部约束。"""

    def test_verify_port_rejects_non_xds110(self):
        """显式指定的端口若不是 XDS110 应用口，必须拒绝而不是将就使用。"""
        class FakePortInfo:
            def __init__(self, device, description):
                self.device = device
                self.description = description
                self.hwid = 'BTHENUM'

        ports = [FakePortInfo('COM3', '蓝牙链接上的标准串行 (COM3)')]
        selected, message = recorder.verify_port('COM3', ports)
        self.assertIsNone(selected)
        self.assertIn('不是 XDS110', message)

        good = [FakePortInfo('COM15', 'XDS110 Class Application/User UART (COM15)')]
        selected, message = recorder.verify_port('COM15', good)
        self.assertIsNotNone(selected)
        self.assertEqual(message, 'ok')

    def test_verify_port_reports_missing_port_with_listing(self):
        """端口不存在时要列出实际枚举结果，方便定位是不是换了口。"""
        selected, message = recorder.verify_port('COM15', [])
        self.assertIsNone(selected)
        self.assertIn('未枚举到任何串口', message)

    def test_status_families_cover_every_firmware_status(self):
        """头文件里的每个状态都要有中文归因，避免出现未登记状态。"""
        expectations = {
            'SIGNAL_TOO_SMALL': 'amplitude_gate',
            'ADC_HIGH_CLIPPED': 'saturation_gate',
            'ADC_LOW_CLIPPED': 'saturation_gate',
            'PERIOD_NOT_FOUND': 'period_gate',
            'FREQUENCY_OUT_OF_RANGE': 'frequency_gate',
            'ADC_SAMPLE_INVALID': 'data_validity',
            'CLASSIFICATION_AMBIGUOUS': 'model_margin_gate',
            'INSUFFICIENT_SAMPLES': 'sample_count_gate',
            'BAD_ARGUMENT': 'api_contract',
            'BAD_METADATA': 'api_contract',
        }
        for status, family in expectations.items():
            _reason, actual = recorder.classify({'status': status})
            self.assertEqual(actual, family, status)

    def test_unknown_status_is_surfaced_not_hidden(self):
        """固件新增状态时应当显式暴露，而不是静默归入某一类。"""
        reason, family = recorder.classify({'status': 'SOMETHING_NEW'})
        self.assertEqual(family, 'unknown_status')
        self.assertIn('SOMETHING_NEW', reason)

    def test_csv_header_matches_legacy_log(self):
        """两套工具写同一个日志文件，列定义必须逐字一致。"""
        legacy_header = ['时间戳', '序号', '状态', '波形类型', '频率(Hz)',
                         '峰峰值(V)', '方波占空比(%)', '匹配得分(%)',
                         '分类余量(%)', '有效周期数']
        self.assertEqual(recorder.CSV_HEADER, legacy_header)
        # 与磁盘上既有日志的表头比对，确认没有第三套约定。
        with open(ROOT / 'waveform_test_log.csv', newline='',
                  encoding='utf-8-sig') as handle:
            on_disk = next(csv.reader(handle))
        self.assertEqual(on_disk, legacy_header)


if __name__ == '__main__':
    unittest.main()
