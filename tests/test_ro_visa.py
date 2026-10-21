"""USB/VISA framing, response limits, and recovery against the SCPI fixture."""
from contextlib import nullcontext
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.capture_store import CaptureStore
from tools.ro_test import Generator, VisaTransport, encode, run
from tools.self_test import Case
from tools.waveform_server import parser
from test_ro import ScpiServer

try:
    from pyvisa.constants import StatusCode
    from pyvisa.errors import VisaIOError
except ImportError:
    StatusCode = VisaIOError = None


class VisaDevice:
    def __init__(self, fixture):
        self.fixture = fixture
        self.pending = bytearray()
        self.timeout = 10000
        self.closed = False
        self.cleared = False
        self.session = 1
        self.visalib = SimpleNamespace(read=self.read)
        self.fail_next_read = False

    def write_raw(self, data):
        if data.startswith((b"C1:WVDT ", b"C2:WVDT ")):
            offset = data.index(b"WAVEDATA,") + len(b"WAVEDATA,")
            self.fixture.commands.append(data[:offset].decode("ascii"))
            self.fixture.uploads.append(data[offset:])
        else:
            command = data.decode("ascii").strip()
            self.fixture.commands.append(command)
            reply = self.fixture.dispatch(command)
            if reply is not None:
                self.pending.extend(reply if isinstance(reply, bytes) else
                                    (reply + "\r\n").encode("ascii"))
        return len(data)

    def read(self, session, size):
        if self.fail_next_read:
            self.fail_next_read = False
            raise VisaIOError(StatusCode.error_timeout)
        size = min(size, 1024)
        part = bytes(self.pending[:size])
        del self.pending[:size]
        return part, (StatusCode.success_max_count_read if self.pending else StatusCode.success)

    def ignore_warning(self, *args):
        return nullcontext()

    def clear(self):
        self.pending.clear()
        self.cleared = True

    def close(self):
        self.closed = True


@unittest.skipIf(StatusCode is None, "optional pyvisa dependency is not installed")
class VisaTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ScpiServer()
        self.devices = []
        self.managers = []

        def manager_factory(*args):
            device = VisaDevice(self.fixture)
            manager = SimpleNamespace(open_resource=lambda resource: device, closed=False)
            manager.close = lambda: setattr(manager, "closed", True)
            self.devices.append(device)
            self.managers.append(manager)
            return manager

        self.patch = patch("pyvisa.ResourceManager", side_effect=manager_factory)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.fixture.close()

    def test_binary_upload_readback_and_shutdown_are_exact(self):
        _, data, config = encode(Case(), 3.3, 1.)
        # The waveform contains binary line feeds; they must not end the read.
        self.assertIn(b"\n", data)
        with tempfile.TemporaryDirectory() as directory:
            with Generator("unused", transport="visa", resource="USB0::fixture::INSTR") as generator:
                self.assertEqual(self.fixture.combine, "OFF")
                reply = generator.configure(data, config, directory)
                self.assertEqual(reply["transport"], "VISA USBTMC")
                self.assertTrue(reply["waveform"]["verified"])
                saved = (Path(directory) / "generator.readback.bin").read_bytes()
                self.assertIn(data, saved)
        self.assertEqual(self.fixture.uploads, [data])
        self.assertEqual(self.fixture.output, "OFF")
        self.assertTrue(all(d.closed for d in self.devices))
        self.assertTrue(all(m.closed for m in self.managers))

    def test_read_limit_is_enforced_before_another_chunk(self):
        transport = VisaTransport("USB0::fixture::INSTR", 2.)
        try:
            transport.open()
            self.devices[-1].pending.extend(bytes(range(256)) * 10)
            with self.assertRaisesRegex(RuntimeError, "exceeds 1200 bytes"):
                transport.read(limit=1200)
            self.assertEqual(len(self.devices[-1].pending), 1360)
            self.assertEqual(self.devices[-1].timeout, 2000)
        finally:
            transport.close()

    def test_timeout_reopens_and_clears_before_output_off(self):
        generator = Generator("unused", transport="visa", resource="USB0::fixture::INSTR")
        generator.trace = io.StringIO()
        with self.assertRaises(TimeoutError):
            with generator:
                generator.send("C1:OUTP ON")
                self.devices[-1].fail_next_read = True
                generator.query("*OPC?")
        self.assertEqual(self.fixture.output, "OFF")
        self.assertEqual(len(self.devices), 2)
        self.assertTrue(self.devices[-1].cleared)
        self.assertTrue(all(d.closed for d in self.devices))
        self.assertTrue(all(m.closed for m in self.managers))
        self.assertFalse(generator.shutdown_error)

    def test_combining_must_be_disabled_before_references(self):
        dispatch = self.fixture.dispatch
        self.fixture.dispatch = lambda command: (
            "C1:CMBN ON" if command == "C1:CMBN?" else dispatch(command))
        with self.assertRaisesRegex(RuntimeError, "waveform combining is not disabled"):
            with Generator("unused", transport="visa", resource="USB0::fixture::INSTR"):
                self.fail("combined output must not be used for calibration")
        self.assertEqual(self.fixture.output, "OFF")
        self.assertNotIn("ON", self.fixture.events)
        self.assertTrue(all(d.closed for d in self.devices))

    def test_calibration_continues_after_usb_system_error(self):
        with Generator("unused", transport="visa", resource="USB0::fixture::INSTR") as generator:
            failed_device = self.devices[-1]
            failed_device.write_raw = lambda data: (_ for _ in ()).throw(
                VisaIOError(StatusCode.error_system_error))
            reference = generator.reference("sine", .57, .72, 173.371)
            self.assertEqual(reference["offset_v"], .57)
            self.assertEqual(self.fixture.output, "ON")
            self.assertEqual(generator.connection_recoveries, 1)
            self.assertIsNone(generator.failure)
            self.assertTrue(failed_device.closed)
            self.assertTrue(self.devices[-1].cleared)
        self.assertEqual(self.fixture.output, "OFF")

    def test_query_io_error_discards_reply_before_repeating_query(self):
        with Generator("unused", transport="visa", resource="USB0::fixture::INSTR") as generator:
            failed_device = self.devices[-1]
            failed_device.visalib.read = lambda *args: (_ for _ in ()).throw(
                VisaIOError(StatusCode.error_io))
            self.assertEqual(generator.query("*OPC?"), "1")
            self.assertEqual(generator.query("C1:OUTP?"), "C1:OUTP OFF,LOAD,HZ,PLRT,NOR")
            self.assertTrue(failed_device.closed)
            self.assertTrue(self.devices[-1].cleared)
            self.assertEqual(generator.connection_recoveries, 1)
            self.assertIsNone(generator.failure)

    def test_identification_recovers_from_previous_broken_usb_session(self):
        original_write = VisaDevice.write_raw
        failed = False
        def write(device, data):
            nonlocal failed
            if data == b"*IDN?\n" and not failed:
                failed = True
                raise VisaIOError(StatusCode.error_io)
            return original_write(device, data)
        with patch.object(VisaDevice, "write_raw", write):
            with Generator("unused", transport="visa", resource="USB0::fixture::INSTR") as generator:
                self.assertIn("SDG6022X-E", generator.identity)
                self.assertEqual(generator.connection_recoveries, 1)
        self.assertEqual(self.fixture.output, "OFF")

    def test_persistent_io_error_stops_after_two_reconnects(self):
        generator = Generator("unused", transport="visa", resource="USB0::fixture::INSTR")
        with self.assertRaisesRegex(RuntimeError, "VI_ERROR_IO"):
            with generator:
                original_write = VisaDevice.write_raw
                def write(device, data):
                    if data == b"C1:BSWV?\n":
                        raise VisaIOError(StatusCode.error_io)
                    return original_write(device, data)
                with patch.object(VisaDevice, "write_raw", write):
                    generator.query("C1:BSWV?")
        self.assertEqual(generator.connection_recoveries, 2)
        self.assertEqual(generator.failure["command"], "C1:BSWV?")
        self.assertEqual(self.fixture.output, "OFF")
        self.assertTrue(all(d.closed for d in self.devices))

    def test_binary_upload_io_error_is_not_replayed(self):
        _, data, config = encode(Case(), 3.3, 1.)
        generator = Generator("unused", transport="visa", resource="USB0::fixture::INSTR")
        original_write = VisaDevice.write_raw
        attempts = []
        def write(device, payload):
            if payload.startswith(b"C1:WVDT "):
                attempts.append(payload)
                raise VisaIOError(StatusCode.error_io)
            return original_write(device, payload)
        with patch.object(VisaDevice, "write_raw", write):
            with self.assertRaisesRegex(RuntimeError, "VI_ERROR_IO"):
                with generator:
                    generator.configure(data, config)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(generator.connection_recoveries, 0)
        self.assertEqual(self.fixture.output, "OFF")

    def test_short_write_is_rejected(self):
        transport = VisaTransport("USB0::fixture::INSTR", 2.)
        try:
            transport.open()
            self.devices[-1].write_raw = lambda data: len(data) - 1
            with self.assertRaisesRegex(ConnectionError, "incomplete VISA write"):
                transport.write(b"*IDN?\n")
        finally:
            transport.close()

    def test_ro_uses_visa_and_records_the_selected_resource(self):
        args = parser().parse_args(["RO", "--ro-transport", "visa", "--ro-resource",
                                    "USB0::fixture::INSTR", "--max-cases", "1"])
        with tempfile.TemporaryDirectory() as directory:
            store = CaptureStore(directory)
            with patch("socket.create_connection", side_effect=AssertionError("unexpected LAN")):
                self.assertEqual(run(None, store, args, io.StringIO()), 0)
            report = next(store.path.glob("ro-*"))
            for name in ("instrument.json", "summary.json"):
                info = json.loads((report / name).read_text(encoding="utf8"))
                self.assertEqual(info["transport"], "VISA USBTMC")
                self.assertEqual(info["resource"], "USB0::fixture::INSTR")
            self.assertTrue(json.loads((report / "summary.json").read_text())["complete"])
            self.assertEqual(self.fixture.output, "OFF")


if __name__ == "__main__":
    unittest.main()
