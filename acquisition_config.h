#ifndef ACQUISITION_CONFIG_H_
#define ACQUISITION_CONFIG_H_

/* Values describing ADC0.CH2 / TIMA0 on this board.  Override only after
 * measuring the physical clock, reference voltage or analogue front end.
 * flags bit 0 = contiguous, bit 1 = calibrated clock, bit 2 = calibrated Vref.
 */
#define ACQ_SAMPLE_COUNT             (8192U)
#define ACQ_PROTOCOL_VERSION         (3U)
/* SCOMP0/1 register count; SysConfig retains sampling-clock divide-by-2.
 * Leave ample analogue settling time within each 50 us timer interval. */
#define ACQ_ADC_SAMPLE_TIMER_COUNT   (32U)
#define ACQ_ADC_FULL_SCALE           (4095U)
#define ACQ_SAMPLE_RATE_MILLIHZ      (20000000UL)
/* Stock LP-MSPM0G3507 Y2 is 40 MHz (SLAU873, section 2.7).
 * Set to 48000000UL ONLY if Y2 has physically been replaced with 48 MHz. */
#ifndef ACQ_HFXT_FREQ_HZ
#define ACQ_HFXT_FREQ_HZ              (40000000UL)
#endif
#define ACQ_REFERENCE_MICROVOLTS     (3300000UL)
#define ACQ_FRONTEND_GAIN_PPM        (1000000UL)
#define ACQ_FLAGS                    (1UL)

#endif
