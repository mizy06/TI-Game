import unittest
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools.vpp_empirical_scan import (SocketInstrument, Vxi11Instrument, aggregate,
                                      cases, check_connection, key, main, schedule)


class EmpiricalScanTests(unittest.TestCase):
    def test_source_change_requires_opt_in_and_preserves_results(self):
        from tools.vpp_empirical_scan import validate_resume
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            before = dict(cases=[100], parameters_sha256='cal', source_sha256={'a': 'old'})
            after = dict(before, source_sha256={'a': 'new'})
            validate_resume(folder, before)
            (folder/'results.jsonl').write_text('retained')
            with self.assertRaisesRegex(ValueError, '--accept-source-change'):
                validate_resume(folder, after)
            validate_resume(folder, after, True)
            self.assertEqual((folder/'results.jsonl').read_text(), 'retained')
            self.assertEqual(json.loads((folder/'plan.json').read_text()), after)
            history = list(folder.glob('source-change-*.json'))
            self.assertEqual(len(history), 1)
            self.assertEqual(json.loads(history[0].read_text())['previous_manifest'], before)
            with self.assertRaisesRegex(ValueError, 'parameters_sha256'):
                validate_resume(folder, dict(after, parameters_sha256='different'), True)
            with self.assertRaisesRegex(ValueError, 'cases'):
                validate_resume(folder, dict(after, cases=[180]), True)

    def test_two_pass_grid_and_noise(self):
        plan = list(cases(10))
        self.assertEqual(len(plan), 4216)
        self.assertEqual({c['frequency_hz'] for c in plan}, set(range(100, 401, 10)))
        for amplitude in range(80, 161, 5):
            subset = [c for c in plan if c['wave'] == 'sine' and
                      c['frequency_hz'] == 100 and c['vpp_v'] == amplitude/100]
            self.assertEqual({c['noise_ratio'] for c in subset}, {.1, .4})
        first_midpoint = next(i for i, c in enumerate(plan) if c['vpp_v'] == .85)
        self.assertTrue(all(round(c['vpp_v']*100) % 10 == 0 for c in plan[:first_midpoint]))
        self.assertEqual([c['wave'] for c in plan[:4]], ['sine', 'square', 'triangle', 'sawtooth'])
        self.assertEqual(len({(c['frequency_hz'], c['vpp_v'], c['noise_ratio']) for c in plan[:4]}), 1)

    def test_full_rounds_and_resume(self):
        plan = list(cases(10))[:8]
        scheduled = list(schedule(plan, set()))
        self.assertEqual([r for r, _, _ in scheduled], [0]*8+[1]*8+[2]*8)
        self.assertEqual([i for _, i, _ in scheduled], list(range(8))*3)
        done = {(key(plan[0]), 0), (key(plan[3]), 1)}
        resumed = list(schedule(plan, done))
        self.assertEqual(len(resumed), 22)
        self.assertTrue(all((key(c), r) not in done for r, _, c in resumed))

    def test_direct_scpi_line_framing(self):
        connection = MagicMock()
        connection.makefile.return_value = io.BytesIO(b'Siglent,SDG6022X-E\n1\n')
        with patch('tools.vpp_empirical_scan.socket.create_connection', return_value=connection):
            instrument = SocketInstrument('192.0.2.1')
        self.assertEqual(instrument.query('*IDN?'), 'Siglent,SDG6022X-E')
        self.assertEqual(instrument.query('*OPC?'), '1')
        connection.sendall.assert_called_with(b'*OPC?\n')
        with self.assertRaisesRegex(ConnectionError, 'incomplete'):
            instrument.query('*IDN?')
        instrument.close()
        connection.close.assert_called_once()

    def test_connection_failure_names_host_and_network_checks(self):
        with patch('tools.vpp_empirical_scan.socket.create_connection', side_effect=TimeoutError('timeout')):
            with self.assertRaisesRegex(ConnectionError, '192.0.2.1:5025.*Ethernet'):
                SocketInstrument('192.0.2.1')

    def test_vxi11_scpi_framing_and_resource_cleanup(self):
        with patch('tools.vpp_empirical_scan.Vxi11') as client:
            client.return_value.read.return_value = b'Siglent,SDG6022X-E\n'
            instrument = Vxi11Instrument('192.0.2.1')
            self.assertEqual(instrument.query('*IDN?'), 'Siglent,SDG6022X-E')
            client.assert_called_once_with('192.0.2.1', 111, 5.)
            client.return_value.open.assert_called_once_with()
            client.return_value.write.assert_called_once_with(b'*IDN?\n')
            client.return_value.read.assert_called_once_with(limit=65536, text=True)
            instrument.close()
            client.return_value.close.assert_called_once_with()

    def test_vxi11_failure_reports_protocol_and_closes_link(self):
        with patch('tools.vpp_empirical_scan.Vxi11') as client:
            client.return_value.open.side_effect = TimeoutError('timeout')
            with self.assertRaisesRegex(ConnectionError, 'VXI-11.*192.0.2.1:111'):
                Vxi11Instrument('192.0.2.1')
            client.return_value.close.assert_called_once_with()

    def test_cli_defaults_to_vxi11_and_offline_plan_does_not_lock_uart(self):
        with patch('sys.argv', ['scan', '--output', 'unused', '--plan-only']), \
                patch('tools.vpp_empirical_scan.device_lock') as lock, \
                patch('tools.vpp_empirical_scan.run') as run:
            main()
            args = run.call_args.args[0]
            self.assertEqual((args.transport, args.instrument_port), ('vxi11', 111))
            lock.assert_not_called()

    def test_cli_retains_explicit_raw_socket_port(self):
        with patch('sys.argv', ['scan', '--output', 'unused', '--plan-only',
                                '--transport', 'socket', '--instrument-port', '5555']), \
                patch('tools.vpp_empirical_scan.run') as run:
            main()
            args = run.call_args.args[0]
            self.assertEqual((args.transport, args.instrument_port), ('socket', 5555))
        with patch('tools.vpp_empirical_scan.socket.create_connection') as connect:
            instrument = SocketInstrument('192.0.2.1', port=5555)
            connect.assert_called_once_with(('192.0.2.1', 5555), 5.)
            instrument.close()

    def test_connection_check_reads_source_and_only_opens_uart(self):
        source = MagicMock()
        with patch('tools.vpp_empirical_scan.open_instrument', return_value=source), \
                patch('tools.vpp_empirical_scan.snapshot', return_value={
                    '*IDN?': 'Siglent,SDG6022X-E', 'C1:OUTP?': 'C1:OUTP ON,LOAD,HZ',
                    'C2:OUTP?': 'C2:OUTP OFF,LOAD,HZ'}), \
                patch('serial.Serial') as uart, patch('sys.stdout', new_callable=io.StringIO):
            check_connection(SimpleNamespace(port='COM15'))
            uart.assert_called_once_with('COM15', 115200, timeout=3, write_timeout=3)
            uart.return_value.__enter__.return_value.write.assert_not_called()
            source.write.assert_not_called()
            source.close.assert_called_once_with()

    def test_signed_mean_and_incomplete_failures(self):
        case = dict(wave='sine', frequency_hz=100, vpp_v=1., noise_ratio=0.)
        rows = [dict(case, valid=True, measured_vpp_v=v) for v in (1.001, 1.002, 1.003)]
        rows.append(dict(case, valid=False))
        summary = aggregate(rows)[0]
        self.assertTrue(summary['complete'])
        self.assertAlmostEqual(summary['bias_v'], .002)
        self.assertAlmostEqual(summary['correction_to_add_v'], -.002)
        self.assertAlmostEqual(summary['repeat_std_v'], .001)
        self.assertFalse(aggregate(rows[:2])[0]['complete'])
