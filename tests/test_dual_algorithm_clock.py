"""发布前静态契约：双算法命令、API 版本和 80 MHz 时钟必须同时存在。"""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DualAlgorithmClockTests(unittest.TestCase):
    def test_public_api_exposes_two_real_algorithms(self):
        header = (ROOT / 'waveform_analyzer.h').read_text(encoding='utf-8')
        source = (ROOT / 'waveform_analyzer.c').read_text(encoding='utf-8')
        self.assertIn('#define WAVEFORM_ALGORITHM_VERSION       (0x00020100UL)',
                      header)
        self.assertIn('WAVEFORM_ALGORITHM_LEGACY_AMDF_CROSSING = 1', header)
        self.assertIn('WAVEFORM_ALGORITHM_GLOBAL_HARMONIC_MODEL = 2', header)
        self.assertIn('WAVEFORM_ALGORITHM_FAST_PHASE_MODEL = 3', header)
        self.assertIn('waveform_analyze_frame_with_algorithm', header)
        self.assertIn('select_global_harmonic_period', source)

    def test_uart_commands_select_each_algorithm_and_both(self):
        source = (ROOT / 'empty.c').read_text(encoding='utf-8')
        self.assertIn("suffix == '1'", source)
        self.assertIn("suffix == '2'", source)
        self.assertIn("suffix == '3'", source)
        self.assertIn('RT-SUITE-START,algorithms=2', source)
        self.assertIn('FW=algorithm-v2.1,API=0x00020100,CPU_HZ=', source)

    def test_sysconfig_requests_rated_80_mhz_and_retimes_peripherals(self):
        config = (ROOT / 'empty.syscfg').read_text(encoding='utf-8')
        required = [
            'SYSCTL.SYSPLL_CLK2XEn        = true;',
            'SYSCTL.SYSPLL_Pdiv           = 1;',
            'SYSCTL.SYSPLL_Qdiv           = 5;',
            'SYSCTL.SYSPLL_CLK2XDiv       = 3;',
            'SYSCTL.MCLKSource            = "HSCLK";',
            'SYSCTL.HSCLKSource           = "SYSPLLCLK2X";',
            'SYSCTL.UDIV                  = "2";',
            'SYSCTL.validateClkStatus     = true;',
            'UART1.targetBaudRate    = 115200;',
            'TIMER1.timerPeriod                 = "0.05 ms";',
        ]
        for line in required:
            self.assertIn(line, config)
        self.assertNotRegex(config, re.compile(r'CPUCLK\s*=\s*32\s*MHz'))

    def test_host_stub_tracks_generated_cpu_frequency(self):
        stub = (ROOT / 'tests' / 'stubs' / 'ti_msp_dl_config.h').read_text(
            encoding='utf-8')
        self.assertRegex(stub, r'#define CPUCLK_FREQ\s+\(80000000U\)')


if __name__ == '__main__':
    unittest.main()
