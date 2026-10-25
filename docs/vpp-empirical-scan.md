# LAN Vpp empirical sweep

The scan compares raw internal Vpp with the generator's native waveform
setpoint. It does not include the final display deduction and does not modify
the active calibration or install a compensation table.

- Source: SDG6022X-E at `10.11.13.220`, raw SCPI socket port 5025.
  The scanner uses a direct TCP socket; no VISA resource discovery is needed.
- Four waveforms: sine, square, triangle, sawtooth.
- Frequency: 100–400 Hz inclusive, default step 10 Hz.
- Vpp: first 0.80, 0.90, …, 1.60 V; then 0.85, 0.95, …, 1.55 V.
- Every amplitude has 10% and 40% Gaussian noise, with no other noise levels.
- Noise sigma is ratio × source Vpp / 6; bandwidth is 6f for 10% noise and
  3f for 40% noise, representative settings within the weak/strong ranges. Native random
  noise has no harmonic guards; it differs from the identifiable RO replay
  matrix. This scan does not cover all competition noise bandwidths.
- DC offset held at 1.6 V, square duty held at 50%, both loads must be HZ.
- Each combination has three independent measurements. Failed measurements
  are recorded and do not contribute to the mean.
- For each frequency/amplitude/noise combination, measure sine, square,
  triangle and sawtooth in that order. Finish the entire coarse and midpoint
  sweep before starting round two, then repeat the full sweep for round three.

There are 4,216 combinations and 12,648 measurements. Each measurement uses
the existing three-frame R estimator. Pilot timings of about 8.4 seconds per R
imply roughly 30 hours plus source switching and analysis variation.

```powershell
python -u -m tools.vpp_empirical_scan --host 10.11.13.220 --port COM15 --frequency-step 10 --output data/vpp-empirical-lan-20261025-r2
```

Use the identical command to resume. Source hashes, calibration hash and the
entire plan must match. Runs stop after three consecutive invalid measurements.
The old r1 directory uses a different order and is retained separately; use a
new directory for this version. A per-serial-port OS lock rejects duplicate scans.
Generator state is saved and restored on normal completion or Python exceptions.
An abrupt process kill cannot guarantee restoration. Do not run another server
against COM15 or another controller against the generator during scanning.

`results.jsonl` retains each attempt and batch diagnostics; raw UART frames and
per-frame archives are in `captures/`. `offsets.csv` and `offsets.json` contain:

- `bias_v`: mean measured Vpp minus source Vpp.
- `correction_to_add_v`: source Vpp minus mean measured Vpp.
- Repeat count, complete status, standard deviation and range.

`progress.json` reports completed combinations. A partial group is explicitly
marked incomplete; a missing result is never replaced with zero. Resume reruns
missing successful repeats. The voltage reference is the generator setpoint,
not an independent voltmeter; observed bias includes source and receiver errors.

If connecting to port 5025 times out, verify the generator's displayed LAN IP,
Ethernet adapter address, link and subnet. On 2026-10-25 the previous PC Ethernet
address 10.11.13.219 disappeared and 10.11.13.220:5025 timed out. Changing the
Python transport does not restore a disconnected network.
