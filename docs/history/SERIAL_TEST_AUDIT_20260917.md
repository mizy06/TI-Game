# Serial waveform test audit - 2026-09-17

## Follow-up after approved firmware flash

The user approved rebuilding and flashing the reviewed injection firmware.
The earlier blocked outcome below is retained as historical evidence.

- Full ARM rebuild succeeded using CCS gmake and the configured TI toolchain.
  SysConfig was run through its CLI; generated files remained unchanged.
- DSLite flash and verification succeeded, followed by System Reset and run.
  The live serial boot banner confirmed FW=audited-20260917-v1.
- Firmware SHA-256:
  212fa89f859151a0250812436a488c7e740811f3aa49a4648a2bcef950337faf.
- W now responds normally. Every payload receives a byte count and decimal
  FNV-1a-32 receipt, checked independently by the host before accepting results.
  This is an accidental-corruption check, not a cryptographic guarantee.
- All 54 planned cases were executed on the MCU: 54 classifications correct,
  54 engineering passes, 54 matching data receipts, no retries or dropped cases.
- Worst errors: frequency 0.026%, Vpp 6.30%, square duty 0.4 percentage points.
  Trigger-to-result times were 0.734-0.766 seconds, including UART upload;
  these are host-observed times, not isolated MCU execution benchmarks.
- Four classes at 100/250/400 Hz, 0/20% finite Gaussian noise Vpp;
  square duty 20/50/80% and edges 0/10% of the period; 2 Vpp, 1.65 V offset.
- Additional device checks: constant input returned SIGNAL_TOO_SMALL;
  alternating 1000/4095 codes returned ADC_HIGH_CLIPPED with 2048 clipped
  samples; codes of 4096 returned ERR_SAMPLE_RANGE and READY without analysis.
- Four T commands correctly classified sine/square/triangle/sawtooth after
  fixing the self-test triangle formula and halving its noise amplitude to
  15% peak-to-peak. T still uses uniform noise and is not the Gaussian suite.
- R still returns SIGNAL_TOO_SMALL (about 0.235 seconds to READY). No known
  analog signal has been provided, so ADC end-to-end accuracy is NOT validated.
- Eight independent Python unit tests passed. The existing 2400-case host
  regression also passed, but it is recorded only as supplementary regression,
  not as evidence for the 54 MCU serial results. waveform_analyzer.c is unchanged.

New evidence is in tests/results/serial_20260917_flashed/: results.jsonl,
summary.json, metadata.json, input_001.bin through input_054.bin, and
additional_checks.json. Build/flash/boot evidence is in tests/results/:
build_20260917.log, flash_20260917.log, boot_20260917.bin.

Reproduce with a new output directory (existing directories are not overwritten):

```
py -3.11 -B -m unittest discover -s tests -p test_audited_serial.py -v
py -3.11 -B tools/audited_serial_test.py --port COM15 --output tests/results/NEW_RUN
```

Protocol: send exactly one W byte with NO newline; wait for READY_FOR_DATA;
send exactly 8192 little-endian bytes (4096 unsigned 12-bit codes stored as
uint16); validate DATA,bytes=8192,fnv1a=DECIMAL; require PROCESSING, a complete
RESULT, and terminal READY. On handshake/integrity failure, stop rather than
retrying blindly. The frame remains fixed-length and has no sequence identifier.
After a partial-frame timeout, reset/resynchronize before sending another frame.
Use the audited script rather than the legacy scripts for this firmware.

## Outcome

Serial communication and the ADC-trigger command were exercised on COM15,
XDS110 Class Application/User UART, USB VID:PID 0451:BEF3, serial MG350001,
115200 baud, 8-N-1. No firmware was flashed or modified.

Three observed R captures returned SIGNAL_TOO_SMALL, type=unknown,
high_clip_samples=0, approximately 0.218-0.219 seconds after triggering.
Two of these captures are preserved in diagnostic.json. They establish that
the capture command returns, not that analog waveform classification works.
No independent analog input ground truth was available.

W did not receive READY_FOR_DATA within 10 seconds. In one uninterrupted
connection, R succeeded, W was silent for 10 seconds, and R succeeded again.
This behavior differs from empty.c, which implements W injection. The device
firmware identity has not been verified; an older/different build is a likely
explanation, not a proven diagnosis.

No binary payload was transmitted without a handshake. Consequently, the
planned 54-case injection matrix was NOT executed. There is no valid waveform
accuracy percentage to report. A transport/protocol failure is not a classifier
misclassification.

## Manual requirements

The supplied competition manual pages 1-2 requires sine, square, triangle,
descending sawtooth; 100-400 Hz; square duty 20-80%; each square edge at most
10% of a period; Gaussian noise Vpp at most 20% of signal Vpp and bandwidth
upper limit at least 5 times the signal frequency. Outputs include frequency,
Vpp, and square duty. Preliminary identification limit is 10 seconds.
The manual does not specify numerical preliminary measurement-error thresholds.
The 2% frequency, 8% Vpp, and 3 percentage-point duty limits used by local
tests are engineering criteria, NOT official scoring limits.

## Existing test defects

- tools/simulate_serial_waveform.py: inject_waveform_and_get_result retries W
  and transmits 8192 bytes even when READY_FOR_DATA was never received.
- Its evaluate_case initializes frequency and Vpp error to zero, leaving them
  passing when measurements are absent or zero. Missing square duty also passes.
- Both legacy serial tools can silently select another port when an explicitly
  requested port is absent, including falling back to an unrelated first port.
- Legacy regex parsers accept partial RESULT lines; measurements are optional.
- Legacy onchip expectations do not track the persistent firmware case index.
  They use 2.2 V square and 1.8 V triangle expectations, whereas firmware intends
  approximately 2.0 V for every self-test, and randomized extra cases do not
  correspond to the firmware's fixed four-case cycle.
- empty.c generate_simulated_samples triangle has incorrect piecewise scaling:
  the middle segment reaches approximately -1.5 times vpp_counts, followed by a
  discontinuity. It may be clipped and is not a valid 2 Vpp triangle.
- That firmware self-test's uniform noise is +/-15% signal Vpp, approximately
  30% peak-to-peak, not the advertised 15%, and is not Gaussian.
- The legacy host mode loads a preexisting desktop DLL. Its results are not
  serial/MCU measurements and must not substitute for this request.
- The existing W protocol has no checksum or sample readback. Even a successful
  injection would not independently prove byte-perfect reception or ADC quality.

## Independent test entry point

tools/audited_serial_test.py uses explicit verified XDS110 Application port
selection, exact handshake and response ordering, monotonic 10-second deadline,
required finite positive measurements, no retry/fallback, and terminal READY.
It saves raw received bytes, timings, input files and SHA-256 hashes, and all
failures. It imports neither legacy test code nor the desktop DLL.

The planned matrix contains 54 cases: all four classes at 100/250/400 Hz,
zero/20% noise, and square duty 20/50/80% with ideal/10%-period edges.
Inputs are 2 Vpp with 1.65 V offset. Finite Gaussian white-noise records are
scaled to the requested peak-to-peak ratio, without clipping samples; the
discrete noise band extends to Nyquist (10 kHz). This is one defined noise
model, not coverage of every possible competition noise spectrum or front end.

Six unit tests passed before the independent tool opened COM15, covering
missing/invalid fields, failed handshakes without payload, transaction ordering,
missing terminal READY, reproducibility, and input range for all 54 cases.

Evidence: tests/results/serial_20260917_audited/metadata.json,
results.jsonl, diagnostic.json, initial_rx.bin, and input_001.bin.

## Required next step

Either verify/build/flash a reviewed firmware with W support for a digital
injection test, or connect a known, safely bounded analog source to PA25 and
provide its independently measured class/frequency/Vpp/duty for R testing.
The former tests MCU algorithm execution and UART transport, not ADC performance.
The latter is required for competition-representative end-to-end validation.
