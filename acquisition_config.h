#ifndef ACQUISITION_CONFIG_H_
#define ACQUISITION_CONFIG_H_

/* Values describing ADC0.CH2 / TIMA0 on this board.  Override only after
 * measuring the physical clock, reference voltage or analogue front end.
 * flags bit 0 = contiguous, bit 1 = calibrated clock, bit 2 = calibrated Vref.
 */
#define ACQ_SAMPLE_COUNT             (8192U)
#define ACQ_PROTOCOL_VERSION         (3U)
/* SCOMP0/1 register count; ADCCLK=ULPCLK and SAMPCLK=ULPCLK/2.
 * Leave ample analogue settling time within each 50 us timer interval. */
#define ACQ_ADC_SAMPLE_TIMER_COUNT   (32U)
#define ACQ_ADC_FULL_SCALE           (4095U)
/* Uniform sampling within each frame. R uses this default; S/T and Udddd\n
 * select alternative timer periods to resolve unobserved square edges. */
#ifndef ACQ_TIMER_PERIOD_TICKS
#define ACQ_TIMER_PERIOD_TICKS       (1600U)
#endif
#if ACQ_TIMER_PERIOD_TICKS < 1500U || ACQ_TIMER_PERIOD_TICKS > 1700U
#error "Acquisition timer trial must stay near the validated 20 kS/s rate"
#endif
#define ACQ_RATE_FOR_TICKS(ticks)    \
    ((uint32_t)((32000000000ULL + (ticks) / 2U) / (ticks)))
#define ACQ_SAMPLE_RATE_MILLIHZ      ACQ_RATE_FOR_TICKS(ACQ_TIMER_PERIOD_TICKS)
/* Stock LP-MSPM0G3507 Y2 is 40 MHz (SLAU873, section 2.7).
 * A different physical crystal also requires matching empty.syscfg changes. */
#ifndef ACQ_HFXT_FREQ_HZ
#define ACQ_HFXT_FREQ_HZ              (40000000UL)
#endif
#define ACQ_REFERENCE_MICROVOLTS     (3300000UL)
#define ACQ_FRONTEND_GAIN_PPM        (1000000UL)
/* Bit 3 advertises S/T and Udddd\n (1500..1700 ticks), uniformly per frame. */
#define ACQ_FLAGS                    (9UL)

#endif
