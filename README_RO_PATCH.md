# TI-RO-365 Calibration & Matrix Patch (Minimal Replacement Package)

## 1. Overview
This minimal replacement package updates the `RO` (Ethernet VXI-11 generator + UART `R` loop test) matrix from the legacy 296-case preliminary matrix to the **365-case Finals matrix set (`48 + 96 + 221`)** (`new48` + `fresh96` + `paired221`), while preserving and upgrading the two-stage initial hardware calibration strategy.

## 2. Included Files (7 Core Files)
- `tools/ro_test.py`: Updates `RO` default matrix to `ro_matrix()` (`48 new48 + 96 fresh96 + 221 paired221 = 365` cases), supports `4096`- and `8192`-sample frames, phase-aligned carrier seam loop-closure (`close_record`), sub-suite filtering (`--case-id new48|fresh96|paired221|preliminary`), and backward compatibility with `--case-id competition-*`.
- `tools/acquisition_calibration.py`: Retains Stage-1 independent DDS reference calibration (`3 DC + 2 Sine + 3 independent validation checks`) and upgrades it with:
  1. **Fisher-information ($f^2$) weighted multi-tone timebase fusion** across the 100 Hz and 400 Hz reference tones (~1.85x lower RMS clock error).
  2. **Gated 5-point DC + harmonic-projected Sine-center affine fit** (~33% lower midpoint offset error).
  3. **Coherent harmonic projection AC amplitude & quadratic DC curvature diagnostics** (`harmonic_amplitude_v`, `ac_gain_ratio`, `dc_quadratic_curvature_counts_per_v2`).
- `tools/precision_measurement.py`: Upgrades `harmonic_frequency` with tighter Brent optimization (`xatol=1e-12, maxiter=80`), **2-step analytical Golub-Pereyra variable-projection Gauss-Newton polishing**, and **1D Chebyshev quantization-polytope centering** (`< 0.03 ppm` reference tone period recovery).
- `tools/generator_calibration.py`: Retains Stage-2 local 5-point ARB transfer calibration around the 50% operating point and upgrades `verify_playback` with exact float64 `sample_rate_hz_exact` propagation, coherent harmonic AC amplitude verification, and design matrix condition number diagnostics.
- `validation/matrix_new48.json`: 48-case `new48` matrix specification (completing `matrix_new48.json` + `matrix_fresh96.json` + `matrix_221.json`).
- `validation/fingerprints_new48.json`: Exact SHA-256 ADC payload fingerprints for all 48 `new48` cases.
- `tests/test_ro.py`: Updated `RO` unit test suite verifying both the 365-case Finals matrix set and the 296-case legacy matrix.

## 3. Usage
Extract directly over your `TI-game` project root:
```bash
unzip -o TI-RO-365-calibration-patch.zip -d /path/to/TI-game
```

Run `RO` (365-case `48+96+221` matrix by default):
```bash
python server.py RO --port COM6 --ro-host 10.11.13.220
```
Or run a specific sub-matrix (`new48`, `fresh96`, `paired221`, or `preliminary`):
```bash
python server.py RO --port COM6 --case-id new48
python server.py RO --port COM6 --case-id fresh96
python server.py RO --port COM6 --case-id paired221
```
