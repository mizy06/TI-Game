#ifndef BOARD_CALIBRATION_H_
#define BOARD_CALIBRATION_H_

/* MSPM0G3507 ADC timer calibration for the board tested on 2026-09-29.
 * TIMER_0 counts 4000 nominal 80 MHz clock ticks per sample. The external
 * source readings at 100, 200 and 370 Hz imply about 20240 samples/second.
 * This is an estimate from serial measurements, NOT an independent clock
 * measurement. Measure 4096 timer intervals on the actual board and replace
 * this number if needed. Do not set CLOCK_CALIBRATED until then.
 * Override from compiler definitions when building for a different board. */
#ifndef WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ
#define WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ (20240000UL)
#endif

#endif /* BOARD_CALIBRATION_H_ */
