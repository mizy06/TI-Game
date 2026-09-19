#ifndef WAVEFORM_ANALYZER_H_
#define WAVEFORM_ANALYZER_H_

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define WAVEFORM_ALGORITHM_VERSION       (0x00020000UL)
#define WAVEFORM_SAMPLE_RATE_HZ          (20000U)
#define WAVEFORM_SAMPLE_COUNT            (4096U)
#define WAVEFORM_MAX_SAMPLE_COUNT        (4096U)
#define WAVEFORM_ADC_FULL_SCALE          (4095U)
#define WAVEFORM_ADC_VDDA_MV             (3300U)
#define WAVEFORM_UNITY_GAIN_PPM          (1000000UL)

#ifndef WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ
#define WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ \
    (WAVEFORM_SAMPLE_RATE_HZ * 1000UL)
#endif
#ifndef WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS
#define WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS \
    (WAVEFORM_ADC_VDDA_MV * 1000UL)
#endif
#ifndef WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM
#define WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM (WAVEFORM_UNITY_GAIN_PPM)
#endif

/* Acquisition-side facts. Calibration flags are deliberately explicit: an
 * algorithm cannot remove sample-clock or voltage-reference scale error. */
enum {
    WAVEFORM_FRAME_FLAG_CONTIGUOUS          = (1UL << 0),
    WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED    = (1UL << 1),
    WAVEFORM_FRAME_FLAG_VREF_CALIBRATED     = (1UL << 2),
    WAVEFORM_FRAME_FLAG_HARDWARE_AVERAGED   = (1UL << 3)
};

#ifndef WAVEFORM_ACQUISITION_FLAGS
#define WAVEFORM_ACQUISITION_FLAGS (WAVEFORM_FRAME_FLAG_CONTIGUOUS)
#endif

enum {
    WAVEFORM_QUALITY_CLOCK_UNCALIBRATED = (1U << 0),
    WAVEFORM_QUALITY_VREF_UNCALIBRATED  = (1U << 1),
    WAVEFORM_QUALITY_FEW_CYCLES         = (1U << 2),
    WAVEFORM_QUALITY_LOW_MODEL_MARGIN   = (1U << 3)
};

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
    WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE,
    WAVEFORM_STATUS_ADC_LOW_CLIPPED,
    WAVEFORM_STATUS_ADC_SAMPLE_INVALID,
    WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS,
    WAVEFORM_STATUS_BAD_ARGUMENT,
    WAVEFORM_STATUS_BAD_METADATA,
    WAVEFORM_STATUS_INSUFFICIENT_SAMPLES
} waveform_status_t;

/* Contract between the acquisition/peripheral layer and the algorithm.
 * Samples are immutable, contiguous, right-aligned unsigned ADC codes. Units
 * are encoded in field names to prevent Hz/mHz and V/uV integration errors.
 * frontend_gain_ppm is ADC-pin amplitude / source-input amplitude. */
typedef struct {
    const uint16_t *samples;
    uint16_t sample_count;
    uint16_t adc_full_scale;
    uint32_t sample_rate_millihz;
    uint32_t adc_reference_microvolts;
    uint32_t frontend_gain_ppm;
    uint32_t sequence;
    uint32_t flags;
} waveform_frame_t;

typedef struct {
    /* Original result prefix retained for source/binary migration simplicity. */
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
    uint16_t low_clip_samples;
    uint16_t invalid_samples;

    /* Version-2 precision and traceability fields. */
    uint32_t algorithm_version;
    uint32_t sequence;
    uint32_t vpp_microvolts;
    uint32_t duty_ppm;
    uint32_t period_q16_samples;
    uint32_t vpp_counts_q16;
    uint16_t quality_flags;
    uint16_t model_margin_permille;
} waveform_result_t;

/* Preferred version-2 interface. The frame remains owned by the acquisition
 * layer and must not be modified until this synchronous call returns. */
bool waveform_analyze_frame(const waveform_frame_t *frame,
                            waveform_result_t *result);

/* Compatibility wrapper using the fixed project defaults above. */
bool waveform_analyze(const uint16_t *samples, waveform_result_t *result);

const char *waveform_type_name(waveform_type_t type);
const char *waveform_status_name(waveform_status_t status);

#ifdef __cplusplus
}
#endif

#endif /* WAVEFORM_ANALYZER_H_ */
