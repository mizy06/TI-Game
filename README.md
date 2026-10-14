# TI-algorithm-patch-7.0 (Unified Waveform Solver 7.0)

This replacement package upgrades the host-side waveform estimation engine (`tools/unified_solver.py`, `tools/host_waveform.py`, `tools/waveform_server.py`) for the 2026 TI Cup Track 1 Finals (`TI-game-finals`), combining the strengths of `patch-5.0` and `patch-6.0` under the competition handbook rules (15 seconds per sample, PC-side calculation permitted, grading on waveform type, frequency, Vpp, and square-wave duty cycle).

## Quick Installation

### Option 1: One-Command Automatic Installer (with Backup)
```bash
python3 install_patch.py /path/to/TI-game-finals
```

### Option 2: Direct File Copy
Copy the three files in `tools/` directly over your project's `tools/` directory:
- `tools/unified_solver.py`
- `tools/host_waveform.py`
- `tools/waveform_server.py`

## Verification

Run the bundled 48-test verification suite (covering unit tests, out-of-band projection, quantization polytope centering, theoretical counterexamples, installer integrity, and live TCP server round-trips):
```bash
python3 checks/run_checks.py
```

## Head-to-Head Benchmark Summary (`patch-5.0` vs `patch-6.0` vs `patch-7.0`)

### 1. Paired 221-Case Benchmark (`validation/matrix_221.json`)
| Metric | Patch 5.0 | Patch 6.0 | **Patch 7.0** |
|---|---:|---:|---:|
| Waveform Classification | 221 / 221 | 221 / 221 | **221 / 221** |
| Engineering Pass | 213 / 221 | 215 / 221 | **215 / 221** |
| Competition Relative `< 1 ppm` (Freq + Vpp + Duty) | 14 / 221 | 18 / 221 | **21 / 221** |
| All 4 Relative `< 1 ppm` (incl. DC) | 7 / 221 | 11 / 221 | **14 / 221** |
| Absolute Error `< 1e-6` (excl. DC) | 3 / 221 | 7 / 221 | **11 / 221** |
| Absolute Error `< 1e-6` (incl. DC) | 1 / 221 | 4 / 221 | **6 / 221** |
| Median Frequency Error (ppm) | 0.255 | 0.141 | **0.083** |
| P90 Frequency Error (ppm) | 40.912 | 27.599 | **16.137** |
| Max Frequency Error (ppm) | 290.125 | 285.629 | **135.434** |
| Median Vpp Error (ppm) | 713.591 | 691.500 | **505.575** |
| Median Square Duty Error (ppm) | 94.661 | 44.504 | **32.959** |

### 2. Unseen 96-Case Stress Benchmark (`validation/matrix_fresh96.json`)
| Metric | Patch 5.0 | Patch 6.0 | **Patch 7.0** |
|---|---:|---:|---:|
| Waveform Classification | 93 / 96 | 96 / 96 | **96 / 96** |
| Engineering Pass | 89 / 96 | 95 / 96 | **96 / 96 (100%)** |
| Competition Relative `< 1 ppm` | 0 / 96 | 1 / 96 | **1 / 96** |
| Median Frequency Error (ppm) | 7.211 | 3.462 | **1.387** |
| P90 Frequency Error (ppm) | 61.981 | 27.603 | **20.419** |
| Max Frequency Error (ppm) | 158.706 | 94.111 | **45.843** |
| Median Vpp Error (ppm) | 2,175.606 | 1,680.822 | **448.229** |
| P90 Vpp Error (ppm) | 49,330.630 | 7,351.841 | **6,432.669** |
| Max Vpp Error (ppm) | 195,976.332 | 111,039.248 | **20,680.249** |
| Median Square Duty Error (ppm) | 267.854 | 31.015 | **15.449** |
| P90 Square Duty Error (ppm) | 2,523.860 | 1,182.890 | **410.198** |

### 3. Original 296-Case Regression Suite (`regression296`)
- **296 / 296 (100%)** passed.
