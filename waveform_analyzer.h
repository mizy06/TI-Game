#ifndef WAVEFORM_ANALYZER_H_
#define WAVEFORM_ANALYZER_H_

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Must match TIMER_0 in empty.syscfg: 0.05 ms -> 20 ksample/s. */
#define WAVEFORM_SAMPLE_RATE_HZ   (20000U)
#define WAVEFORM_SAMPLE_COUNT     (4096U)
#define WAVEFORM_ADC_FULL_SCALE   (4095U)
#define WAVEFORM_ADC_VDDA_MV      (3300U)

typedef enum {
    WAVEFORM_TYPE_SINE = 0,
    WAVEFORM_TYPE_SQUARE,
    WAVEFORM_TYPE_TRIANGLE,
    WAVEFORM_TYPE_SAWTOOTH,
    WAVEFORM_TYPE_UNKNOWN
} waveform_type_t;

typedef enum {
    WAVEFORM_STATUS_OK = 0,
    WAVEFORM_STATUS_SIGNAL_TOO_SMALL,
    WAVEFORM_STATUS_ADC_HIGH_CLIPPED,
    WAVEFORM_STATUS_PERIOD_NOT_FOUND,
    WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE
} waveform_status_t;

typedef struct {
    waveform_type_t type;
    waveform_status_t status;
    uint32_t frequency_millihz;
    uint32_t vpp_millivolts;
    uint16_t duty_permille;
    uint16_t confidence_permille;
    uint16_t best_score_permille;
    uint16_t second_score_permille;
    uint16_t accepted_cycles;
    uint16_t high_clip_samples;
    uint16_t dc_offset_counts;
} waveform_result_t;

/*
 * The input buffer contains WAVEFORM_SAMPLE_COUNT unsigned 12-bit ADC values.
 * All working buffers are statically allocated inside waveform_analyzer.c so
 * the Cortex-M0+ stack remains small and deterministic.
 */
bool waveform_analyze(const uint16_t *samples, waveform_result_t *result);

const char *waveform_type_name(waveform_type_t type);
const char *waveform_status_name(waveform_status_t status);

#ifdef __cplusplus
}
#endif

#endif /* WAVEFORM_ANALYZER_H_ */
