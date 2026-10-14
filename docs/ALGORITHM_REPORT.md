# Unified Waveform Solver 7.0 — Technical Report

## 1. Design Philosophy & Competition Constraints
Under the 2026 TI Cup Track 1 Finals rules (`2026TI杯赛道一赛事手册.pdf`, Section 2 & Section 5), each test sample allows up to **15 seconds** for measurement and display, and the graded waveform parameters are **waveform class**, **frequency**, **peak-to-peak voltage ($V_{pp}$)**, and **square-wave duty cycle**. Running on the host PC removes embedded MCU compute bottlenecks and allows us to target **part-per-million ($10^{-6}$, 1 ppm)** parameter estimation accuracy while staying comfortably within a ~10 s worst-case runtime envelope.

## 2. Five Core Algorithmic Upgrades in 7.0 (Fusing 5.0 + 6.0)

1. **Dual-Regime Quantization Polytope Centering (`_quantized_refine`)**:
   - **Noiseless / Multi-Period Regime (`K == 0`)**: Uses exact 1D Chebyshev polytope bisection (`_refine_noiseless_1d_polytope`) for continuous waveforms (`sine`, `triangle`, `sawtooth`) and exact transition-interval midpointing for sharp-edge `square` waves, recovering sub-ppm accuracy on locked/quantized cases (`ID 32: 0.997 ppm`, `ID 72: 0.038 ppm`, `ID 118: 0.016 ppm`, `ID 36 & 138: < 0.02 ppm`) where `patch-6.0`'s LP bounding-box center drifted due to slack rows.
   - **Single-Loop Coherent Interference Regime (`K >= 1`)**: Uses HiGHS linear programming (`scipy.optimize.linprog`) with active-row filtering (`rhs < 0.52` in Stage 1, `rhs < 0.35` in Stage 2) and **weighted amplitude-frequency polytope endpoint averaging** (`0.2125 * (v_f_min + v_f_max) + 0.2875 * (v_a_min + v_a_max)`), achieving `< 1 ppm` across **21/221** cases on `matrix_221` (vs 14 in `5.0` and 18 in `6.0`).

2. **Hybrid Few-Cycle Hann / Multi-Cycle Blackman-Harris Out-of-Band Projection (`_bandlimited_refine`)**:
   - Fixed a critical failure mode in `patch-6.0` where low-frequency signals (`f <= 45 Hz` at `N = 4096`) had harmonic spacing `q[0] < 9 * binwidth`, causing the Blackman-Harris guard band (`4.5 * binwidth`) to mask out 100% of the spectrum (`usable` empty).
   - Automatically switches to a narrow-mainlobe Hann window (`guard = 2.1 * binwidth`) when `q[0] < 10.0 * binwidth`, reducing `ID 54` (`square`, 40 Hz) from `237.35 ppm` freq / `24,812 ppm` Vpp error in `6.0` down to **`0.48 ppm` freq / `88.08 ppm` Vpp error**, and recovering 100% engineering pass (`96/96`) on `matrix_fresh96` (`ID 37` dropped from `111,039 ppm` to `961 ppm`).

3. **Usable-Band Relative Cutoff Threshold & Coprime High-Band Sawtooth Refinement**:
   - Adds a usable-band relative floor (`max_u * 2e-5`) to the out-of-band interference cutoff detector so $O(10^{-7})$ 4096-point lookup-table interpolation artifacts at high frequencies do not falsely trigger `cutoff_hz > 0.7 * fs / 2` (`ID 74` in `fresh_96` improved from `ferr = 74.61 ppm, aerr = 44,050 ppm` to **`ferr = 0.00 ppm, aerr = 19.97 ppm`**).
   - Refines `sawtooth` frequency over alias-free high-band coprime harmonics (`saw_hb_loss`), cutting `ID 76` in `fresh_96` from `94.11 ppm` to **`0.05 ppm`**.

4. **Adjacent Integer Nuisance Order Refinement (`K - 1`) & Sub-Harmonic Comb Sine Refinement (`_subharmonic_sine_refine`)**:
   - Evaluates `K - 1` (`4, 6, 9`) alongside coarse `K in (5, 7, 10)` for `square` and `triangle` candidates, and removes 8-period sub-harmonic interference on `sine` waves (`ID 8, 25, 29` reduced from `93-134 ppm` to `< 0.02 ppm`).

5. **Prefix-Sliced QR + Rank-1 Gram-Schmidt Joint Solver & Single-Thread OpenBLAS**:
   - Pre-computes QR factors across nuisance harmonic orders and orthonormalizes the waveform template via a single matrix-vector product inside the nonlinear least-squares loop, while locking OpenBLAS/MKL/OMP to 1 thread via `ctypes`.
