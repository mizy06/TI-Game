"""End-to-end UART frame parsing and the real host C cascade."""
import io
import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import host_waveform as host


class PartialSerial:
    def __init__(self, response):
        self.stream = io.BytesIO(response)
        self.written = b""

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def read(self, count):
        return self.stream.read(min(count, 73))

    def read_until(self, separator=b"\n", size=256):
        data = bytearray()
        while len(data) < size:
            item = self.stream.read(1)
            if not item:
                break
            data.extend(item)
            if item == separator:
                break
        return bytes(data)


def sine_payload():
    values = [int(2048 + 1100 * math.sin(2 * math.pi * 200 * i / 20000))
              for i in range(host.SAMPLES)]
    return struct.pack("<4096H", *values)


def response(payload, *, fs=20000000, vref=3300000, gain=1000000,
             checksum=None, trailer=b"DUMP_END\r\nREADY\r\n"):
    checksum = host.fnv1a(payload) if checksum is None else checksum
    header = (f"DUMP,proto=2,bytes=8192,fnv1a={checksum},seq=37,"
              f"fs_millihz={fs},vref_uv={vref},gain_ppm={gain},"
              "adc_max=4095,flags=1\r\n").encode("ascii")
    return b"TI-CUP ADC DMA HOST V2\r\nCAPTURING\r\n" + header + payload + trailer


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = host.load_api(str(host.ROOT / host.LIBRARY_NAME))

    def test_real_cascade_uses_metadata_from_wire(self):
        payload = sine_payload()
        serial = PartialSerial(response(payload))
        frame, meta = host.dump(serial)
        self.assertEqual(serial.written, b"D")
        result = host.classify(self.api, frame, meta).split(",")
        self.assertEqual(result[0], "sine")
        self.assertAlmostEqual(float(result[1]), 200.0, delta=1)
        self.assertAlmostEqual(float(result[2]), 1.77, delta=0.14)
        self.assertEqual(float(result[3]), 0)
        self.assertAlmostEqual(float(result[4]), 1.65, delta=0.02)

        frame, meta = host.dump(PartialSerial(response(payload, fs=21000000,
                                                      vref=3000000, gain=2000000)))
        adjusted = host.classify(self.api, frame, meta).split(",")
        self.assertEqual(adjusted[0], "sine")
        self.assertAlmostEqual(float(adjusted[1]), 210.0, delta=1)
        self.assertAlmostEqual(float(adjusted[2]), float(result[2]) * 3 / 6.6, delta=0.08)
        self.assertAlmostEqual(float(adjusted[4]), float(result[4]) * 3 / 6.6, delta=0.02)

    def test_checksum_error_does_not_call_algorithm(self):
        with self.assertRaisesRegex(ValueError, "integrity"):
            host.dump(PartialSerial(response(sine_payload(), checksum=123)))

    def test_old_firmware_requires_upgrade(self):
        with self.assertRaisesRegex(ValueError, "v2"):
            host.dump(PartialSerial(b"DUMP,bytes=8192,fnv1a=12\r\n"))

    def test_missing_trailer_and_capture_error(self):
        with self.assertRaisesRegex(TimeoutError, "serial line timeout"):
            host.dump(PartialSerial(response(sine_payload(), trailer=b"DUMP_END\r\n")))
        with self.assertRaisesRegex(RuntimeError, "ERR_ADC_DMA"):
            host.dump(PartialSerial(b"CAPTURING\r\nERR_ADC_DMA\r\nREADY\r\n"))


if __name__ == "__main__":
    unittest.main()
