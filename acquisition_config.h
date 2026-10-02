#ifndef ACQUISITION_CONFIG_H_
#define ACQUISITION_CONFIG_H_

/* Values describing ADC0.CH2 / TIMA0 on this board.  Override only after
 * measuring the physical clock, reference voltage or analogue front end.
 * flags bit 0 = contiguous, bit 1 = calibrated clock, bit 2 = calibrated Vref.
 */
#define ACQ_SAMPLE_COUNT             (4096U)
#define ACQ_ADC_FULL_SCALE           (4095U)
#define ACQ_SAMPLE_RATE_MILLIHZ      (20000000UL)
#define ACQ_REFERENCE_MICROVOLTS     (3300000UL)
#define ACQ_FRONTEND_GAIN_PPM        (1000000UL)
#define ACQ_FLAGS                    (1UL)

#endif
