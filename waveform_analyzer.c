#include "waveform_analyzer.h"

#include <stddef.h>
#include <string.h>

#define PHASE_BINS             (128U)
#define MAX_CROSSINGS          (96U)
#define MIN_PERIOD_SAMPLES     (45U)
#define MAX_PERIOD_SAMPLES     (220U)
#define MIN_VALID_CYCLES       (3U)
#define MIN_ROBUST_SPAN_COUNTS (48U)
#define HIGH_CLIP_CODE         (4092U)
#define LOW_CLIP_CODE          (3U)
#define MAX_HIGH_CLIP_SAMPLES  (WAVEFORM_SAMPLE_COUNT / 200U)
#define MAX_LOW_CLIP_SAMPLES   (WAVEFORM_SAMPLE_COUNT / 200U)
#define MIN_FREQUENCY_MILLIHZ  (98000U)
#define MAX_FREQUENCY_MILLIHZ  (402000U)
#define MIN_CLASS_MARGIN_PERMILLE (8U)

static int16_t g_filtered[WAVEFORM_SAMPLE_COUNT];
static uint16_t g_histogram[256];
static uint32_t g_crossings_q8[MAX_CROSSINGS];
static uint16_t g_periods_q8[MAX_CROSSINGS - 1U];
static uint16_t g_sorted_periods_q8[MAX_CROSSINGS - 1U];
static int32_t g_fold_accumulator[PHASE_BINS];
static int16_t g_folded[PHASE_BINS];
static int16_t g_template[PHASE_BINS];

/* sin(2*pi*(bin+0.5)/128), Q10. Mid-bin sampling avoids edge ambiguity. */
static const int16_t g_sine_template_q10[PHASE_BINS] = {
    25, 75, 125, 175, 224, 273, 321, 369, 415, 460, 505, 548, 590, 630, 669, 706,
    742, 775, 807, 837, 865, 891, 915, 936, 955, 972, 987, 999, 1009, 1016, 1021, 1024,
    1024, 1021, 1016, 1009, 999, 987, 972, 955, 936, 915, 891, 865, 837, 807, 775, 742,
    706, 669, 630, 590, 548, 505, 460, 415, 369, 321, 273, 224, 175, 125, 75, 25,
    -25, -75, -125, -175, -224, -273, -321, -369, -415, -460, -505, -548, -590, -630, -669, -706,
    -742, -775, -807, -837, -865, -891, -915, -936, -955, -972, -987, -999, -1009, -1016, -1021, -1024,
    -1024, -1021, -1016, -1009, -999, -987, -972, -955, -936, -915, -891, -865, -837, -807, -775, -742,
    -706, -669, -630, -590, -548, -505, -460, -415, -369, -321, -273, -224, -175, -125, -75, -25
};

static uint16_t histogram_quantile(uint32_t target)
{
    uint32_t cumulative = 0U;
    uint16_t bin;

    for (bin = 0U; bin < 256U; ++bin) {
        cumulative += g_histogram[bin];
        if (cumulative >= target) {
            return (uint16_t)((bin << 4U) + 8U);
        }
    }
    return WAVEFORM_ADC_FULL_SCALE;
}

static void filter_and_histogram(const uint16_t *samples,
                                 uint16_t *high_clip_samples,
                                 uint16_t *low_clip_samples,
                                 uint16_t *invalid_samples)
{
    uint32_t i;
    uint16_t clips = 0U;
    uint16_t low_clips = 0U;
    uint16_t invalid = 0U;

    memset(g_histogram, 0, sizeof(g_histogram));
    for (i = 0U; i < WAVEFORM_SAMPLE_COUNT; ++i) {
        uint16_t sample = samples[i];
        if (sample > WAVEFORM_ADC_FULL_SCALE) {
            ++invalid;
            sample = WAVEFORM_ADC_FULL_SCALE;
        }
        if (sample >= HIGH_CLIP_CODE) {
            ++clips;
        }
        if (sample <= LOW_CLIP_CODE) {
            ++low_clips;
        }
        ++g_histogram[sample >> 4U];
        g_filtered[i] = (int16_t)sample;
    }

    /* Symmetric 1-2-1 FIR: suppresses high-frequency noise with little
     * attenuation at the worst-case 400 Hz input frequency. */
    for (i = 1U; i < (WAVEFORM_SAMPLE_COUNT - 1U); ++i) {
        uint32_t value = (uint32_t)g_filtered[i - 1U] +
                         (2U * (uint32_t)g_filtered[i]) +
                         (uint32_t)g_filtered[i + 1U];
        g_filtered[i] = (int16_t)((value + 2U) >> 2U);
    }
    *high_clip_samples = clips;
    *low_clip_samples = low_clips;
    *invalid_samples = invalid;
}

static uint32_t interpolate_rising_crossing_q8(uint32_t right_index,
                                                int16_t center)
{
    uint32_t left = right_index - 1U;
    int32_t y0;
    int32_t dy;
    uint32_t fraction_q8;

    while ((left > 0U) && (g_filtered[left] > center)) {
        --left;
    }
    y0 = g_filtered[left];
    dy = (int32_t)g_filtered[left + 1U] - y0;
    if (dy <= 0) {
        return left << 8U;
    }
    fraction_q8 = (uint32_t)((((int32_t)center - y0) << 8U) / dy);
    if (fraction_q8 > 255U) {
        fraction_q8 = 255U;
    }
    return (left << 8U) + fraction_q8;
}

static uint16_t find_rising_crossings(int16_t center, int16_t hysteresis)
{
    const int16_t low_threshold = (int16_t)(center - hysteresis);
    const int16_t high_threshold = (int16_t)(center + hysteresis);
    const uint32_t refractory_q8 = (MIN_PERIOD_SAMPLES * 3U / 5U) << 8U;
    uint32_t last_crossing_q8 = 0U;
    uint16_t count = 0U;
    bool armed = (g_filtered[0] <= low_threshold);
    uint32_t i;

    for (i = 1U; i < WAVEFORM_SAMPLE_COUNT; ++i) {
        if (g_filtered[i] <= low_threshold) {
            armed = true;
        } else if (armed && (g_filtered[i] >= high_threshold)) {
            uint32_t crossing_q8 = interpolate_rising_crossing_q8(i, center);
            if ((count == 0U) || ((crossing_q8 - last_crossing_q8) >= refractory_q8)) {
                if (count < MAX_CROSSINGS) {
                    g_crossings_q8[count++] = crossing_q8;
                }
                last_crossing_q8 = crossing_q8;
            }
            armed = false;
        }
    }
    return count;
}

static uint16_t median_period_q8(uint16_t interval_count)
{
    uint16_t i;

    memcpy(g_sorted_periods_q8, g_periods_q8,
           interval_count * sizeof(g_sorted_periods_q8[0]));
    for (i = 1U; i < interval_count; ++i) {
        uint16_t value = g_sorted_periods_q8[i];
        uint16_t j = i;
        while ((j > 0U) && (g_sorted_periods_q8[j - 1U] > value)) {
            g_sorted_periods_q8[j] = g_sorted_periods_q8[j - 1U];
            --j;
        }
        g_sorted_periods_q8[j] = value;
    }
    return g_sorted_periods_q8[interval_count / 2U];
}

static int16_t interpolate_filtered_q8(uint32_t position_q8)
{
    uint32_t index = position_q8 >> 8U;
    uint32_t fraction = position_q8 & 0xFFU;
    int32_t y0;
    int32_t delta;

    if (index >= (WAVEFORM_SAMPLE_COUNT - 1U)) {
        return g_filtered[WAVEFORM_SAMPLE_COUNT - 1U];
    }
    y0 = g_filtered[index];
    delta = (int32_t)g_filtered[index + 1U] - y0;
    return (int16_t)(y0 + ((delta * (int32_t)fraction) >> 8U));
}

static uint16_t fold_accepted_cycles(uint16_t crossing_count,
                                     uint16_t median_q8,
                                     uint64_t *accepted_period_sum_q8)
{
    const uint32_t min_accepted = ((uint32_t)median_q8 * 3U) / 4U;
    const uint32_t max_accepted = ((uint32_t)median_q8 * 5U) / 4U;
    uint16_t accepted = 0U;
    uint16_t cycle;
    uint16_t bin;

    memset(g_fold_accumulator, 0, sizeof(g_fold_accumulator));
    *accepted_period_sum_q8 = 0U;

    for (cycle = 0U; cycle < (crossing_count - 1U); ++cycle) {
        uint32_t start_q8 = g_crossings_q8[cycle];
        uint32_t period_q8 = g_crossings_q8[cycle + 1U] - start_q8;
        if ((period_q8 < min_accepted) || (period_q8 > max_accepted)) {
            continue;
        }
        for (bin = 0U; bin < PHASE_BINS; ++bin) {
            /* Sample at each phase bin's center, not at a discontinuity. */
            uint32_t numerator = period_q8 * (uint32_t)((2U * bin) + 1U);
            uint32_t position_q8 = start_q8 + (numerator / (2U * PHASE_BINS));
            g_fold_accumulator[bin] += interpolate_filtered_q8(position_q8);
        }
        *accepted_period_sum_q8 += period_q8;
        ++accepted;
    }

    if (accepted != 0U) {
        for (bin = 0U; bin < PHASE_BINS; ++bin) {
            g_folded[bin] = (int16_t)(g_fold_accumulator[bin] / accepted);
        }
    }
    return accepted;
}

static uint16_t estimate_duty_permille(int16_t center)
{
    uint16_t bin;

    for (bin = 1U; bin < PHASE_BINS; ++bin) {
        int32_t y0 = g_folded[bin - 1U];
        int32_t y1 = g_folded[bin];
        if ((y0 > center) && (y1 <= center) && (y0 != y1)) {
            uint32_t fraction_q8 =
                (uint32_t)(((y0 - center) << 8U) / (y0 - y1));
            uint32_t phase_bin_q8 = (((uint32_t)bin - 1U) << 8U) + 128U + fraction_q8;
            uint32_t duty = (phase_bin_q8 * 1000U + (PHASE_BINS * 128U)) /
                            (PHASE_BINS * 256U);
            if (duty > 1000U) {
                duty = 1000U;
            }
            return (uint16_t)duty;
        }
    }
    return 500U;
}

static int16_t robust_fold_center(void)
{
    int32_t low_sum = 0;
    int32_t high_sum = 0;
    uint16_t i;

    memcpy(g_template, g_folded, sizeof(g_folded));
    for (i = 1U; i < PHASE_BINS; ++i) {
        int16_t value = g_template[i];
        uint16_t j = i;
        while ((j > 0U) && (g_template[j - 1U] > value)) {
            g_template[j] = g_template[j - 1U];
            --j;
        }
        g_template[j] = value;
    }
    for (i = 0U; i < 16U; ++i) {
        low_sum += g_template[i];
        high_sum += g_template[PHASE_BINS - 1U - i];
    }
    return (int16_t)((low_sum + high_sum) / 32);
}

static void make_template(waveform_type_t type, uint16_t duty_permille)
{
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        uint16_t phase_u8 = (uint16_t)((2U * bin) + 1U);
        int16_t value;

        switch (type) {
        case WAVEFORM_TYPE_SINE:
            value = g_sine_template_q10[bin];
            break;
        case WAVEFORM_TYPE_SQUARE:
            value = (((uint32_t)phase_u8 * 1000U) <
                     ((uint32_t)duty_permille * 256U)) ? 1024 : -1024;
            break;
        case WAVEFORM_TYPE_TRIANGLE:
            if (phase_u8 < 64U) {
                value = (int16_t)(16 * (int16_t)phase_u8);
            } else if (phase_u8 < 192U) {
                value = (int16_t)(2048 - (16 * (int16_t)phase_u8));
            } else {
                value = (int16_t)((16 * (int16_t)phase_u8) - 4096);
            }
            break;
        case WAVEFORM_TYPE_SAWTOOTH:
            value = (int16_t)(1024 - (8 * (int16_t)phase_u8));
            break;
        default:
            value = 0;
            break;
        }
        g_template[bin] = value;
    }
}

static uint16_t correlation_score_permille(void)
{
    int32_t sum_y = 0;
    int32_t sum_t = 0;
    int64_t dot = 0;
    uint64_t energy_y = 0U;
    uint64_t energy_t = 0U;
    uint64_t numerator;
    uint64_t denominator;
    uint64_t divisor;
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        sum_y += g_folded[bin];
        sum_t += g_template[bin];
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        int32_t y = ((int32_t)g_folded[bin] * (int32_t)PHASE_BINS) - sum_y;
        int32_t t = ((int32_t)g_template[bin] * (int32_t)PHASE_BINS) - sum_t;
        dot += (int64_t)y * t;
        energy_y += (uint64_t)((int64_t)y * y);
        energy_t += (uint64_t)((int64_t)t * t);
    }
    if ((energy_y == 0U) || (energy_t == 0U)) {
        return 0U;
    }

    /* y and t were scaled by PHASE_BINS above. Shift all terms by the same
     * amount before squaring so Cortex-M0+ 64-bit arithmetic cannot overflow. */
    {
        uint64_t positive_dot;
        if (dot <= 0) {
            return 0U;
        }
        positive_dot = (uint64_t)dot;
        while ((positive_dot > 0x3FFFFFFFU) ||
               (energy_y > 0x3FFFFFFFU) || (energy_t > 0x3FFFFFFFU)) {
            positive_dot >>= 1U;
            energy_y >>= 1U;
            energy_t >>= 1U;
        }
        numerator = positive_dot * positive_dot;
        denominator = energy_y * energy_t;
    }
    if (numerator >= denominator) {
        return 1000U;
    }
    divisor = (denominator + 999U) / 1000U;
    if (divisor == 0U) {
        return 0U;
    }
    return (uint16_t)(numerator / divisor);
}

static waveform_type_t classify_fold(uint16_t duty_permille,
                                     uint16_t *best_score,
                                     uint16_t *second_score)
{
    waveform_type_t type;
    waveform_type_t best_type = WAVEFORM_TYPE_UNKNOWN;
    uint16_t best = 0U;
    uint16_t second = 0U;

    for (type = WAVEFORM_TYPE_SINE; type <= WAVEFORM_TYPE_SAWTOOTH;
         type = (waveform_type_t)(type + 1)) {
        uint16_t score;
        make_template(type, duty_permille);
        score = correlation_score_permille();
        if (score > best) {
            second = best;
            best = score;
            best_type = type;
        } else if (score > second) {
            second = score;
        }
    }
    *best_score = best;
    *second_score = second;
    return best_type;
}

bool waveform_analyze(const uint16_t *samples, waveform_result_t *result)
{
    uint16_t low_quantile;
    uint16_t high_quantile;
    uint16_t crossing_count;
    uint16_t interval_count;
    uint16_t median_q8;
    uint64_t accepted_period_sum_q8;
    uint16_t accepted_cycles;
    int16_t center;
    int16_t hysteresis;
    int16_t fold_min;
    int16_t fold_max;
    uint16_t duty_permille;
    uint16_t best_score;
    uint16_t second_score;
    uint16_t i;

    if ((samples == NULL) || (result == NULL)) {
        return false;
    }
    memset(result, 0, sizeof(*result));
    result->type = WAVEFORM_TYPE_UNKNOWN;
    result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;

    filter_and_histogram(samples, &result->high_clip_samples,
                         &result->low_clip_samples,
                         &result->invalid_samples);
    if (result->invalid_samples != 0U) {
        result->status = WAVEFORM_STATUS_ADC_SAMPLE_INVALID;
        return false;
    }
    low_quantile = histogram_quantile(WAVEFORM_SAMPLE_COUNT / 20U);
    high_quantile = histogram_quantile((WAVEFORM_SAMPLE_COUNT * 19U) / 20U);
    result->dc_offset_counts = (uint16_t)(((uint32_t)low_quantile + high_quantile) / 2U);

    if ((uint16_t)(high_quantile - low_quantile) < MIN_ROBUST_SPAN_COUNTS) {
        result->status = WAVEFORM_STATUS_SIGNAL_TOO_SMALL;
        return false;
    }
    if (result->high_clip_samples > MAX_HIGH_CLIP_SAMPLES) {
        result->status = WAVEFORM_STATUS_ADC_HIGH_CLIPPED;
        return false;
    }
    if (result->low_clip_samples > MAX_LOW_CLIP_SAMPLES) {
        result->status = WAVEFORM_STATUS_ADC_LOW_CLIPPED;
        return false;
    }

    center = (int16_t)(((int32_t)low_quantile + high_quantile) / 2);
    hysteresis = (int16_t)((high_quantile - low_quantile) / 6U);
    if (hysteresis < 4) {
        hysteresis = 4;
    }
    crossing_count = find_rising_crossings(center, hysteresis);
    if (crossing_count < (MIN_VALID_CYCLES + 1U)) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }

    interval_count = (uint16_t)(crossing_count - 1U);
    for (i = 0U; i < interval_count; ++i) {
        uint32_t period_q8 = g_crossings_q8[i + 1U] - g_crossings_q8[i];
        if (period_q8 > 65535U) {
            period_q8 = 65535U;
        }
        g_periods_q8[i] = (uint16_t)period_q8;
    }
    median_q8 = median_period_q8(interval_count);
    if ((median_q8 < (MIN_PERIOD_SAMPLES << 8U)) ||
        (median_q8 > (MAX_PERIOD_SAMPLES << 8U))) {
        result->status = WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE;
        return false;
    }

    accepted_cycles = fold_accepted_cycles(crossing_count, median_q8,
                                            &accepted_period_sum_q8);
    result->accepted_cycles = accepted_cycles;
    if (accepted_cycles < MIN_VALID_CYCLES) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }
    result->frequency_millihz =
        (uint32_t)((((uint64_t)WAVEFORM_SAMPLE_RATE_HZ * 1000U * 256U *
                     accepted_cycles) + (accepted_period_sum_q8 / 2U)) /
                   accepted_period_sum_q8);
    if ((result->frequency_millihz < MIN_FREQUENCY_MILLIHZ) ||
        (result->frequency_millihz > MAX_FREQUENCY_MILLIHZ)) {
        result->status = WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE;
        return false;
    }

    fold_min = g_folded[0];
    fold_max = g_folded[0];
    for (i = 1U; i < PHASE_BINS; ++i) {
        if (g_folded[i] < fold_min) {
            fold_min = g_folded[i];
        }
        if (g_folded[i] > fold_max) {
            fold_max = g_folded[i];
        }
    }
    center = robust_fold_center();
    duty_permille = estimate_duty_permille(center);
    result->type = classify_fold(duty_permille, &best_score, &second_score);
    result->best_score_permille = best_score;
    result->second_score_permille = second_score;
    result->confidence_permille = (best_score > 1000U) ? 1000U : best_score;
    if ((best_score < MIN_CLASS_MARGIN_PERMILLE) ||
        ((best_score - second_score) < MIN_CLASS_MARGIN_PERMILLE)) {
        result->type = WAVEFORM_TYPE_UNKNOWN;
        result->duty_permille = 0U;
        result->status = WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS;
        return false;
    }

    {
        uint32_t vpp_counts = (uint16_t)(fold_max - fold_min);
        if (result->type == WAVEFORM_TYPE_SAWTOOTH) {
            /* Mid-bin phase sampling omits 1/128 of the ideal ramp span. */
            vpp_counts = (vpp_counts * 128U + 63U) / 127U;
        }
        result->vpp_millivolts =
            (vpp_counts * WAVEFORM_ADC_VDDA_MV + (WAVEFORM_ADC_FULL_SCALE / 2U)) /
            WAVEFORM_ADC_FULL_SCALE;
    }
    result->duty_permille =
        (result->type == WAVEFORM_TYPE_SQUARE) ? duty_permille : 0U;
    result->status = WAVEFORM_STATUS_OK;
    return true;
}

const char *waveform_type_name(waveform_type_t type)
{
    static const char *const names[] = {
        "sine", "square", "triangle", "sawtooth", "unknown"
    };
    if ((type < WAVEFORM_TYPE_SINE) || (type > WAVEFORM_TYPE_UNKNOWN)) {
        type = WAVEFORM_TYPE_UNKNOWN;
    }
    return names[type];
}

const char *waveform_status_name(waveform_status_t status)
{
    static const char *const names[] = {
        "OK",
        "SIGNAL_TOO_SMALL",
        "ADC_HIGH_CLIPPED",
        "PERIOD_NOT_FOUND",
        "FREQUENCY_OUT_OF_RANGE",
        "ADC_LOW_CLIPPED",
        "ADC_SAMPLE_INVALID",
        "CLASSIFICATION_AMBIGUOUS"
    };
    if ((status < WAVEFORM_STATUS_OK) ||
        (status > WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS)) {
        return "UNKNOWN_STATUS";
    }
    return names[status];
}
