# TI-algorithm-patch-8.0 (Unified Waveform Solver 8.0)

This replacement package upgrades the host-side waveform estimation engine (`tools/unified_solver.py`, `tools/host_waveform.py`, `tools/waveform_server.py`) for the 2026 TI Cup Track 1 Finals (`TI-game-finals`).

## Installation & Rollback

### Option 1: Guarded Transactional Installer (Supports 4.0, 5.0, 6.0, and 7.0)
```bash
# Dry-run hash check (modifies no files):
python3 install_patch.py --project /path/to/TI-game-finals --check

# Install 8.0 and create automatic backup in algorithm-backup-before-8.0/:
python3 install_patch.py --project /path/to/TI-game-finals

# Rollback to previous version:
python3 install_patch.py --project /path/to/TI-game-finals --rollback
```

### Option 2: Direct Three-File Replacement
Copy `tools/unified_solver.py`, `tools/host_waveform.py`, and `tools/waveform_server.py` directly into `/path/to/TI-game-finals/tools/`.

## Running Verification Suites
```bash
# Run the full 48-test unit + TCP server + pipeline verification suite:
python3 checks/run_checks.py --project /path/to/TI-game-finals
```

## Four-Version Head-to-Head Benchmark Summary (`5.0` vs `6.0` vs `7.0` vs `8.0`)

All four versions were evaluated on identical ADC byte payloads (`SHA-256` verified) across 365 total cases:
1. **Unseen 48-Case Generalization Matrix (`new48`, seed `2026101421` / payload `98131029`)**
2. **96-Case Multi-Regime Stress Matrix (`fresh_96`, seed `72014391`)**
3. **221-Case Historical Paired Matrix (`paired_221`, seed `20261012`)**

### 1. Unseen 48-Case Generalization Matrix (`new48`)
| Metric | Patch 5.0 | Patch 6.0 | Patch 7.0 | **Patch 8.0** |
|---|---:|---:|---:|---:|
| Valid Returned | 47 / 48 | 47 / 48 | 47 / 48 | **48 / 48 (100%)** |
| Waveform Classification | 47 / 48 | 46 / 48 | 46 / 48 | **48 / 48 (100%)** |
| Engineering Pass | 47 / 48 | 46 / 48 | 46 / 48 | **48 / 48 (100%)** |
| Median Frequency Error (ppm) | 0.480 | 0.818 | 0.732 | **0.017** |
| P90 Frequency Error (ppm) | 7.344 | 9.792 | 9.792 | **4.110** |
| Max Frequency Error (ppm) | 2,000.441 | 84.487 | 84.487 | **31.470** |
| Median Vpp Error (ppm) | 3,027.872 | 2,563.246 | 1,534.061 | **130.710** |
| P90 Vpp Error (ppm) | 30,717.345 | 20,359.818 | 20,359.818 | **4,974.000** |
| Max Vpp Error (ppm) | 59,555.800 | 276,367.354 | 276,367.354 | **17,633.700** |
| Median Square Duty Error (ppm) | 1,827.709 | 1,639.985 | 95.265 | **10.370** |
| P90 Square Duty Error (ppm) | 15,000.000 | 12,495.943 | 12,495.943 | **5,342.800** |
| Max Square Duty Error (ppm) | 19,178.586 | 1,000,000.000 | 1,000,000.000 | **14,267.300** |
| Median DC Error (ppm) | 46.423 | 36.494 | 35.513 | **11.450** |

### 2. 96-Case Multi-Regime Stress Matrix (`fresh_96`)
| Metric | Patch 5.0 | Patch 6.0 | Patch 7.0 | **Patch 8.0** |
|---|---:|---:|---:|---:|
| Waveform Classification | 96 / 96 | 96 / 96 | 96 / 96 | **96 / 96 (100%)** |
| Engineering Pass | 96 / 96 | 96 / 96 | 96 / 96 | **96 / 96 (100%)** |
| Competition Relative `< 1 ppm` | 0 / 96 | 0 / 96 | 1 / 96 | **8 / 96** |
| Including DC Relative `< 1 ppm` | 0 / 96 | 0 / 96 | 0 / 96 | **7 / 96** |
| All Absolute `< 1e-6` (incl. DC) | 0 / 96 | 0 / 96 | 0 / 96 | **2 / 96** |
| Median Frequency Error (ppm) | 1.352 | 1.679 | 1.387 | **0.071** |
| P90 Frequency Error (ppm) | 21.113 | 25.884 | 20.419 | **16.540** |
| Max Frequency Error (ppm) | 42.359 | 94.108 | 45.843 | **42.359** |
| Median Vpp Error (ppm) | 1,379.964 | 918.978 | 448.229 | **57.990** |
| P90 Vpp Error (ppm) | 17,408.531 | 7,508.777 | 6,432.669 | **3,468.200** |
| Max Vpp Error (ppm) | 49,657.876 | 44,050.077 | 20,680.249 | **18,011.600** |
| Median Square Duty Error (ppm) | 188.509 | 146.362 | 25.272 | **6.250** |
| P90 Square Duty Error (ppm) | 4,082.531 | 980.702 | 662.883 | **481.700** |
| Max Square Duty Error (ppm) | 15,014.875 | 19,019.722 | 19,019.722 | **14,992.700** |
| Median DC Error (ppm) | 58.755 | 47.206 | 35.946 | **9.210** |

### 3. 221-Case Historical Paired Matrix (`paired_221`)
| Metric | Patch 5.0 | Patch 6.0 | Patch 7.0 | **Patch 8.0** |
|---|---:|---:|---:|---:|
| Waveform Classification | 221 / 221 | 221 / 221 | 221 / 221 | **221 / 221 (100%)** |
| Engineering Pass | 215 / 221 | 215 / 221 | 215 / 221 | **215 / 221** |
| Competition Relative `< 1 ppm` | 14 / 221 | 18 / 221 | 21 / 221 | **25 / 221** |
| Including DC Relative `< 1 ppm` | 7 / 221 | 11 / 221 | 14 / 221 | **15 / 221** |
| Median Frequency Error (ppm) | 0.344 | 0.302 | 0.074 | **0.018** |
| P90 Frequency Error (ppm) | 21.903 | 18.372 | 16.026 | **2.940** |
| Max Frequency Error (ppm) | 237.345 | 285.633 | 135.434 | **106.950** |
| Median Vpp Error (ppm) | 4,415.905 | 615.477 | 349.730 | **61.340** |
| P90 Vpp Error (ppm) | 37,192.595 | 31,041.235 | 31,041.794 | **21,976.600** |
| Median Square Duty Error (ppm) | 557.885 | 67.679 | 40.363 | **4.480** |
| P90 Square Duty Error (ppm) | 4,671.282 | 3,049.165 | 3,453.142 | **558.100** |
| Max Square Duty Error (ppm) | 46,177.571 | 82,130.440 | 82,130.794 | **6,705.600** |
| Median DC Error (ppm) | 53.291 | 9.497 | 12.216 | **3.690** |
