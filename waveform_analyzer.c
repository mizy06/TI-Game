#include "waveform_analyzer.h"

#include <stddef.h>
#include <string.h>

#define PHASE_BINS                    (128U)
#define HISTOGRAM_BINS                (256U)
#define MAX_CROSSINGS                 (192U)
#define MAX_PERIOD_LAG                (512U)
#define MIN_INPUT_SAMPLES             (1024U)
#define MIN_VALID_CYCLES              (3U)
#define MIN_FREQUENCY_MILLIHZ         (98000U)
#define MAX_FREQUENCY_MILLIHZ         (402000U)
#define MIN_MODEL_SCORE_PPM           (760000UL)
#define MIN_MODEL_MARGIN_PPM          (5000UL)
#define LOW_MODEL_MARGIN_PPM          (15000UL)
#define MAX_EDGE_PPM                  (100000UL)
#define PHASE_SCALE                   (4096UL)
#define PERIOD_COST_SCALE             (256UL)
#define MAX_CAPTURE_RATE_MILLIHZ      (50000000UL)
#define MIN_CAPTURE_RATE_MILLIHZ      (10000000UL)

static int16_t g_filtered[WAVEFORM_MAX_SAMPLE_COUNT];
static uint16_t g_histogram[HISTOGRAM_BINS];
static uint32_t g_period_cost[MAX_PERIOD_LAG + 1U];
static uint32_t g_crossings_q12[MAX_CROSSINGS];
static uint32_t g_interval_scratch[MAX_CROSSINGS - 1U];
static int32_t g_fold_accumulator[PHASE_BINS];
static uint16_t g_fold_minimum[PHASE_BINS];
static uint16_t g_fold_maximum[PHASE_BINS];
static int16_t g_folded[PHASE_BINS];
static int16_t g_template[PHASE_BINS];
static int16_t g_sort_buffer[PHASE_BINS];

/* sin(2*pi*(bin+0.5)/128), Q10. */
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

static uint32_t absolute_difference_u32(uint32_t a, uint32_t b)
{
    return (a >= b) ? (a - b) : (b - a);
}

static int32_t absolute_i32(int32_t value)
{
    return (value < 0) ? -value : value;
}

static uint16_t histogram_quantile(uint32_t target, uint16_t full_scale)
{
    uint32_t cumulative = 0U;
    uint16_t bin;

    for (bin = 0U; bin < HISTOGRAM_BINS; ++bin) {
        cumulative += g_histogram[bin];
        if (cumulative >= target) {
            uint32_t numerator = ((uint32_t)(2U * bin + 1U) *
                                  ((uint32_t)full_scale + 1U));
            uint32_t value = (numerator + HISTOGRAM_BINS) /
                             (2U * HISTOGRAM_BINS);
            return (value > full_scale) ? full_scale : (uint16_t)value;
        }
    }
    return full_scale;
}

static void filter_and_histogram(const waveform_frame_t *frame,
                                 waveform_result_t *result)
{
    uint32_t i;
    uint32_t guard = (uint32_t)frame->adc_full_scale / 1024U;
    uint16_t high_clip_code;
    int16_t first;
    int16_t second;
    int16_t previous_two;
    int16_t previous_one;

    if (guard < 3U) {
        guard = 3U;
    }
    high_clip_code = (uint16_t)((uint32_t)frame->adc_full_scale - guard);
    memset(g_histogram, 0, sizeof(g_histogram));

    for (i = 0U; i < frame->sample_count; ++i) {
        uint16_t sample = frame->samples[i];
        uint32_t bin;
        if (sample > frame->adc_full_scale) {
            ++result->invalid_samples;
            sample = frame->adc_full_scale;
        }
        if (sample >= high_clip_code) {
            ++result->high_clip_samples;
        }
        if (sample <= guard) {
            ++result->low_clip_samples;
        }
        bin = ((uint32_t)sample * HISTOGRAM_BINS) /
              ((uint32_t)frame->adc_full_scale + 1U);
        if (bin >= HISTOGRAM_BINS) {
            bin = HISTOGRAM_BINS - 1U;
        }
        ++g_histogram[bin];
        g_filtered[i] = (int16_t)sample;
    }

    /* Symmetric 1-2-1 FIR. Original samples are carried in local variables so
     * this is genuinely symmetric rather than an accidental recursive filter. */
    first = g_filtered[0];
    second = g_filtered[1];
    previous_two = first;
    previous_one = second;
    for (i = 2U; i < frame->sample_count; ++i) {
        int16_t current = g_filtered[i];
        uint32_t value = (uint32_t)previous_two +
                         (2U * (uint32_t)previous_one) +
                         (uint32_t)current;
        g_filtered[i - 1U] = (int16_t)((value + 2U) >> 2U);
        previous_two = previous_one;
        previous_one = current;
    }
    g_filtered[0] = (int16_t)(((3U * (uint32_t)first) +
                               (uint32_t)second + 2U) >> 2U);
    g_filtered[frame->sample_count - 1U] =
        (int16_t)(((uint32_t)previous_two +
                   (3U * (uint32_t)previous_one) + 2U) >> 2U);
}

static uint32_t period_cost_for_lag(uint16_t sample_count, uint16_t lag)
{
    uint64_t sum = 0U;
    uint32_t count = 0U;
    uint32_t i;

    for (i = 0U; (i + lag) < sample_count; i += 2U) {
        sum += absolute_difference_u32((uint16_t)g_filtered[i],
                                       (uint16_t)g_filtered[i + lag]);
        ++count;
    }
    if (count == 0U) {
        return UINT32_MAX;
    }
    return (uint32_t)((sum * PERIOD_COST_SCALE + (count / 2U)) / count);
}

static bool estimate_period_amdf(const waveform_frame_t *frame,
                                 uint16_t robust_span,
                                 uint32_t *period_q12)
{
    uint32_t min_lag = frame->sample_rate_millihz /
                       MAX_FREQUENCY_MILLIHZ;
    uint32_t max_lag = (frame->sample_rate_millihz +
                        MIN_FREQUENCY_MILLIHZ - 1U) /
                       MIN_FREQUENCY_MILLIHZ;
    uint32_t best_lag;
    uint32_t best_cost = UINT32_MAX;
    uint32_t lag;
    uint32_t threshold;

    if (min_lag > 2U) {
        --min_lag;
    }
    ++max_lag;
    if (min_lag < 2U) {
        min_lag = 2U;
    }
    if ((max_lag > MAX_PERIOD_LAG) ||
        (max_lag >= ((uint32_t)frame->sample_count - 2U))) {
        return false;
    }

    best_lag = min_lag;
    for (lag = min_lag; lag <= max_lag; ++lag) {
        g_period_cost[lag] =
            period_cost_for_lag(frame->sample_count, (uint16_t)lag);
        if (g_period_cost[lag] < best_cost) {
            best_cost = g_period_cost[lag];
            best_lag = lag;
        }
    }

    /* Exact multiples are also AMDF minima. Select the earliest convincing
     * local minimum so the fundamental wins over 2f/3f aliases. */
    threshold = best_cost + (best_cost / 3U) +
                ((uint32_t)robust_span * PERIOD_COST_SCALE / 64U) + 1U;
    for (lag = min_lag; lag <= max_lag; ++lag) {
        bool left_ok = (lag == min_lag) ||
                       (g_period_cost[lag] <= g_period_cost[lag - 1U]);
        bool right_ok = (lag == max_lag) ||
                        (g_period_cost[lag] <= g_period_cost[lag + 1U]);
        if (left_ok && right_ok && (g_period_cost[lag] <= threshold)) {
            best_lag = lag;
            break;
        }
    }

    *period_q12 = best_lag << 12U;
    if ((best_lag > min_lag) && (best_lag < max_lag)) {
        int64_t left = g_period_cost[best_lag - 1U];
        int64_t middle = g_period_cost[best_lag];
        int64_t right = g_period_cost[best_lag + 1U];
        int64_t curvature = left - (2 * middle) + right;
        if (curvature > 0) {
            int64_t correction_q12 =
                ((left - right) * (int64_t)(PHASE_SCALE / 2U)) /
                curvature;
            if (correction_q12 > (int64_t)(PHASE_SCALE / 2U)) {
                correction_q12 = PHASE_SCALE / 2U;
            } else if (correction_q12 < -(int64_t)(PHASE_SCALE / 2U)) {
                correction_q12 = -(int64_t)(PHASE_SCALE / 2U);
            }
            *period_q12 = (uint32_t)((int64_t)*period_q12 +
                                     correction_q12);
        }
    }
    return true;
}

static uint32_t interpolate_rising_crossing_q12(uint32_t right_index,
                                                 int16_t center)
{
    uint32_t left = right_index - 1U;
    int32_t y0;
    int32_t delta;
    uint32_t fraction_q12;

    while ((left > 0U) && (g_filtered[left] > center)) {
        --left;
    }
    y0 = g_filtered[left];
    delta = (int32_t)g_filtered[left + 1U] - y0;
    if (delta <= 0) {
        return left << 12U;
    }
    fraction_q12 =
        (uint32_t)((((int32_t)center - y0) * (int32_t)PHASE_SCALE) /
                   delta);
    if (fraction_q12 >= PHASE_SCALE) {
        fraction_q12 = PHASE_SCALE - 1U;
    }
    return (left << 12U) + fraction_q12;
}

static uint16_t find_rising_crossings(uint16_t sample_count,
                                      int16_t center,
                                      int16_t hysteresis,
                                      uint32_t minimum_period_q12)
{
    const int16_t low_threshold = (int16_t)(center - hysteresis);
    const int16_t high_threshold = (int16_t)(center + hysteresis);
    const uint32_t refractory_q12 = (minimum_period_q12 * 11U) / 20U;
    uint32_t last_crossing_q12 = 0U;
    uint16_t count = 0U;
    bool armed = (g_filtered[0] <= low_threshold);
    uint32_t i;

    for (i = 1U; i < sample_count; ++i) {
        if (g_filtered[i] <= low_threshold) {
            armed = true;
        } else if (armed && (g_filtered[i] >= high_threshold)) {
            uint32_t crossing_q12 =
                interpolate_rising_crossing_q12(i, center);
            if ((count == 0U) ||
                ((crossing_q12 - last_crossing_q12) >= refractory_q12)) {
                if (count < MAX_CROSSINGS) {
                    g_crossings_q12[count++] = crossing_q12;
                }
                last_crossing_q12 = crossing_q12;
            }
            armed = false;
        }
    }
    return count;
}

static uint32_t median_crossing_period_q12(uint16_t crossing_count)
{
    uint16_t interval_count;
    uint16_t i;

    if (crossing_count < 2U) {
        return 0U;
    }
    interval_count = (uint16_t)(crossing_count - 1U);
    for (i = 0U; i < interval_count; ++i) {
        g_interval_scratch[i] = g_crossings_q12[i + 1U] -
                                g_crossings_q12[i];
    }
    for (i = 1U; i < interval_count; ++i) {
        uint32_t value = g_interval_scratch[i];
        uint16_t j = i;
        while ((j > 0U) && (g_interval_scratch[j - 1U] > value)) {
            g_interval_scratch[j] = g_interval_scratch[j - 1U];
            --j;
        }
        g_interval_scratch[j] = value;
    }
    return g_interval_scratch[interval_count / 2U];
}

static uint16_t interval_cycle_step(uint32_t interval_q12,
                                    uint32_t coarse_period_q12)
{
    uint32_t steps = (interval_q12 + (coarse_period_q12 / 2U)) /
                     coarse_period_q12;
    uint32_t expected;
    uint32_t error;

    if ((steps < 1U) || (steps > 3U)) {
        return 0U;
    }
    expected = steps * coarse_period_q12;
    error = absolute_difference_u32(interval_q12, expected);
    if (error > (expected / 5U)) {
        return 0U;
    }
    return (uint16_t)steps;
}

static bool refine_period_from_crossings(uint16_t crossing_count,
                                         uint32_t coarse_period_q12,
                                         uint32_t *period_q12)
{
    uint16_t current_start = 0U;
    uint16_t current_points = 1U;
    uint16_t current_span = 0U;
    uint16_t best_start = 0U;
    uint16_t best_points = 0U;
    uint16_t best_span = 0U;
    uint16_t i;
    uint64_t sum_x = 0U;
    uint64_t sum_x2 = 0U;
    uint64_t sum_y = 0U;
    uint64_t sum_xy = 0U;
    uint32_t cycle = 0U;
    uint64_t denominator;
    int64_t numerator;
    uint32_t refined;

    if (crossing_count < 4U) {
        return false;
    }
    for (i = 0U; i < (uint16_t)(crossing_count - 1U); ++i) {
        uint32_t interval = g_crossings_q12[i + 1U] -
                            g_crossings_q12[i];
        uint16_t step = interval_cycle_step(interval, coarse_period_q12);
        if (step != 0U) {
            ++current_points;
            current_span = (uint16_t)(current_span + step);
        } else {
            if ((current_points > best_points) ||
                ((current_points == best_points) &&
                 (current_span > best_span))) {
                best_start = current_start;
                best_points = current_points;
                best_span = current_span;
            }
            current_start = (uint16_t)(i + 1U);
            current_points = 1U;
            current_span = 0U;
        }
    }
    if ((current_points > best_points) ||
        ((current_points == best_points) && (current_span > best_span))) {
        best_start = current_start;
        best_points = current_points;
        best_span = current_span;
    }
    if ((best_points < 4U) || (best_span < MIN_VALID_CYCLES)) {
        return false;
    }

    for (i = 0U; i < best_points; ++i) {
        uint32_t y = g_crossings_q12[best_start + i];
        if (i != 0U) {
            uint32_t interval =
                g_crossings_q12[best_start + i] -
                g_crossings_q12[best_start + i - 1U];
            cycle += interval_cycle_step(interval, coarse_period_q12);
        }
        sum_x += cycle;
        sum_x2 += (uint64_t)cycle * cycle;
        sum_y += y;
        sum_xy += (uint64_t)cycle * y;
    }
    denominator = ((uint64_t)best_points * sum_x2) - (sum_x * sum_x);
    if (denominator == 0U) {
        return false;
    }
    numerator = (int64_t)((uint64_t)best_points * sum_xy) -
                (int64_t)(sum_x * sum_y);
    if (numerator <= 0) {
        return false;
    }
    refined = (uint32_t)(((uint64_t)numerator + (denominator / 2U)) /
                         denominator);
    if ((refined < ((coarse_period_q12 * 4U) / 5U)) ||
        (refined > ((coarse_period_q12 * 6U) / 5U))) {
        return false;
    }
    *period_q12 = refined;
    return true;
}

static uint16_t interpolate_raw_q12(const waveform_frame_t *frame,
                                    uint32_t position_q12)
{
    uint32_t index = position_q12 >> 12U;
    uint32_t fraction = position_q12 & (PHASE_SCALE - 1U);
    uint32_t y0;
    int32_t delta;
    int32_t value;

    if (index >= ((uint32_t)frame->sample_count - 1U)) {
        return frame->samples[frame->sample_count - 1U];
    }
    y0 = frame->samples[index];
    delta = (int32_t)frame->samples[index + 1U] - (int32_t)y0;
    value = (int32_t)y0 +
            (int32_t)((delta * (int32_t)fraction) >> 12U);
    if (value < 0) {
        value = 0;
    } else if (value > frame->adc_full_scale) {
        value = frame->adc_full_scale;
    }
    return (uint16_t)value;
}

static uint16_t fold_cycles(const waveform_frame_t *frame,
                            uint32_t period_q12)
{
    uint32_t cycle;
    uint16_t accepted = 0U;
    uint16_t bin;
    const uint32_t last_position_q12 =
        ((uint32_t)frame->sample_count - 1U) << 12U;

    memset(g_fold_accumulator, 0, sizeof(g_fold_accumulator));
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        g_fold_minimum[bin] = UINT16_MAX;
        g_fold_maximum[bin] = 0U;
    }

    for (cycle = 0U;; ++cycle) {
        uint32_t start_q12 = cycle * period_q12;
        uint32_t last_bin_position =
            start_q12 +
            (uint32_t)(((uint64_t)period_q12 *
                        (2U * PHASE_BINS - 1U)) /
                       (2U * PHASE_BINS));
        if (last_bin_position >= last_position_q12) {
            break;
        }
        for (bin = 0U; bin < PHASE_BINS; ++bin) {
            uint32_t position_q12 =
                start_q12 +
                (uint32_t)(((uint64_t)period_q12 * (2U * bin + 1U)) /
                           (2U * PHASE_BINS));
            uint16_t value = interpolate_raw_q12(frame, position_q12);
            g_fold_accumulator[bin] += value;
            if (value < g_fold_minimum[bin]) {
                g_fold_minimum[bin] = value;
            }
            if (value > g_fold_maximum[bin]) {
                g_fold_maximum[bin] = value;
            }
        }
        ++accepted;
    }

    if (accepted >= MIN_VALID_CYCLES) {
        for (bin = 0U; bin < PHASE_BINS; ++bin) {
            int32_t sum = g_fold_accumulator[bin];
            uint16_t divisor = accepted;
            if (accepted >= 5U) {
                sum -= g_fold_minimum[bin];
                sum -= g_fold_maximum[bin];
                divisor = (uint16_t)(accepted - 2U);
            }
            g_folded[bin] = (int16_t)((sum + (divisor / 2U)) / divisor);
        }
    }
    return accepted;
}

static void sort_folded(void)
{
    uint16_t i;

    memcpy(g_sort_buffer, g_folded, sizeof(g_folded));
    for (i = 1U; i < PHASE_BINS; ++i) {
        int16_t value = g_sort_buffer[i];
        uint16_t j = i;
        while ((j > 0U) && (g_sort_buffer[j - 1U] > value)) {
            g_sort_buffer[j] = g_sort_buffer[j - 1U];
            --j;
        }
        g_sort_buffer[j] = value;
    }
}

static uint32_t crossing_phase_q12(uint16_t bin, int16_t threshold)
{
    uint16_t next = (uint16_t)((bin + 1U) % PHASE_BINS);
    int32_t y0 = g_folded[bin];
    int32_t y1 = g_folded[next];
    int32_t denominator = y1 - y0;
    uint32_t fraction = 0U;
    uint32_t phase;

    if (denominator != 0) {
        int32_t numerator = ((int32_t)threshold - y0) *
                            (int32_t)PHASE_SCALE;
        int32_t signed_fraction = numerator / denominator;
        if (signed_fraction < 0) {
            signed_fraction = 0;
        } else if (signed_fraction >= (int32_t)PHASE_SCALE) {
            signed_fraction = (int32_t)PHASE_SCALE - 1;
        }
        fraction = (uint32_t)signed_fraction;
    }
    phase = ((uint32_t)bin << 12U) + (PHASE_SCALE / 2U) + fraction;
    return phase % (PHASE_BINS * PHASE_SCALE);
}

static uint32_t estimate_duty_ppm(void)
{
    uint32_t rising[8];
    uint32_t falling[8];
    uint16_t rising_count = 0U;
    uint16_t falling_count = 0U;
    uint16_t level_count = PHASE_BINS / 16U;
    int32_t low_sum = 0;
    int32_t high_sum = 0;
    int16_t threshold;
    uint16_t above = 0U;
    uint16_t i;
    uint32_t target_ppm;
    uint32_t best_duty = 500000U;
    uint32_t best_error = UINT32_MAX;
    const uint32_t cycle_q12 = PHASE_BINS * PHASE_SCALE;

    sort_folded();
    for (i = 0U; i < level_count; ++i) {
        low_sum += g_sort_buffer[i];
        high_sum += g_sort_buffer[PHASE_BINS - 1U - i];
    }
    threshold = (int16_t)(((low_sum / level_count) +
                           (high_sum / level_count)) / 2);

    for (i = 0U; i < PHASE_BINS; ++i) {
        uint16_t next = (uint16_t)((i + 1U) % PHASE_BINS);
        if (g_folded[i] > threshold) {
            ++above;
        }
        if ((g_folded[i] <= threshold) &&
            (g_folded[next] > threshold) &&
            (rising_count < 8U)) {
            rising[rising_count++] = crossing_phase_q12(i, threshold);
        } else if ((g_folded[i] > threshold) &&
                   (g_folded[next] <= threshold) &&
                   (falling_count < 8U)) {
            falling[falling_count++] = crossing_phase_q12(i, threshold);
        }
    }
    target_ppm = ((uint32_t)above * 1000000UL + (PHASE_BINS / 2U)) /
                 PHASE_BINS;
    for (i = 0U; i < rising_count; ++i) {
        uint16_t j;
        for (j = 0U; j < falling_count; ++j) {
            uint32_t distance =
                (falling[j] + cycle_q12 - rising[i]) % cycle_q12;
            uint32_t duty =
                (uint32_t)(((uint64_t)distance * 1000000UL +
                            (cycle_q12 / 2U)) / cycle_q12);
            uint32_t error;
            if ((duty < 100000U) || (duty > 900000U)) {
                continue;
            }
            error = absolute_difference_u32(duty, target_ppm);
            if (error < best_error) {
                best_error = error;
                best_duty = duty;
            }
        }
    }
    return best_duty;
}

static int32_t cyclic_distance_ppm(uint32_t phase, uint32_t center)
{
    int32_t distance = (int32_t)phase - (int32_t)center;
    if (distance > 500000) {
        distance -= 1000000;
    } else if (distance < -500000) {
        distance += 1000000;
    }
    return distance;
}

static int16_t square_template_value(uint32_t phase_ppm,
                                     uint32_t duty_ppm,
                                     uint32_t rise_edge_ppm,
                                     uint32_t fall_edge_ppm)
{
    int32_t rise_distance = cyclic_distance_ppm(phase_ppm, 0U);
    int32_t fall_distance = cyclic_distance_ppm(phase_ppm, duty_ppm);

    if ((rise_edge_ppm != 0U) &&
        ((uint32_t)absolute_i32(rise_distance) <=
         (rise_edge_ppm / 2U))) {
        int32_t value = (rise_distance * 2048) /
                        (int32_t)rise_edge_ppm;
        if (value > 1024) {
            value = 1024;
        } else if (value < -1024) {
            value = -1024;
        }
        return (int16_t)value;
    }
    if ((fall_edge_ppm != 0U) &&
        ((uint32_t)absolute_i32(fall_distance) <=
         (fall_edge_ppm / 2U))) {
        int32_t value = -(fall_distance * 2048) /
                        (int32_t)fall_edge_ppm;
        if (value > 1024) {
            value = 1024;
        } else if (value < -1024) {
            value = -1024;
        }
        return (int16_t)value;
    }
    if ((phase_ppm > (rise_edge_ppm / 2U)) &&
        (phase_ppm < (duty_ppm - (fall_edge_ppm / 2U)))) {
        return 1024;
    }
    return -1024;
}

static void make_template(waveform_type_t type,
                          uint32_t duty_ppm,
                          uint32_t rise_edge_ppm,
                          uint32_t fall_edge_ppm,
                          uint16_t shift)
{
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        uint16_t source_bin =
            (uint16_t)(((uint32_t)bin + shift) % PHASE_BINS);
        uint16_t phase_u8 = (uint16_t)(2U * source_bin + 1U);
        int16_t value;

        switch (type) {
        case WAVEFORM_TYPE_SINE:
            value = g_sine_template_q10[source_bin];
            break;
        case WAVEFORM_TYPE_SQUARE:
        {
            uint32_t phase_ppm =
                ((uint32_t)(2U * source_bin + 1U) * 500000UL) /
                PHASE_BINS;
            value = square_template_value(phase_ppm, duty_ppm,
                                          rise_edge_ppm,
                                          fall_edge_ppm);
            break;
        }
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

static uint32_t correlation_score_ppm(void)
{
    int32_t sum_y = 0;
    int32_t sum_t = 0;
    int64_t dot = 0;
    uint64_t energy_y = 0U;
    uint64_t energy_t = 0U;
    uint64_t positive_dot;
    uint64_t numerator;
    uint64_t denominator;
    uint64_t divisor;
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        sum_y += g_folded[bin];
        sum_t += g_template[bin];
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        int32_t y = ((int32_t)g_folded[bin] * (int32_t)PHASE_BINS) -
                    sum_y;
        int32_t t = ((int32_t)g_template[bin] * (int32_t)PHASE_BINS) -
                    sum_t;
        dot += (int64_t)y * t;
        energy_y += (uint64_t)((int64_t)y * y);
        energy_t += (uint64_t)((int64_t)t * t);
    }
    if ((dot <= 0) || (energy_y == 0U) || (energy_t == 0U)) {
        return 0U;
    }
    positive_dot = (uint64_t)dot;
    while ((positive_dot > 0x3FFFFFFFUL) ||
           (energy_y > 0x3FFFFFFFUL) ||
           (energy_t > 0x3FFFFFFFUL)) {
        positive_dot >>= 1U;
        energy_y >>= 1U;
        energy_t >>= 1U;
    }
    numerator = positive_dot * positive_dot;
    denominator = energy_y * energy_t;
    if (numerator >= denominator) {
        return 1000000UL;
    }
    divisor = (denominator + 999999UL) / 1000000UL;
    if (divisor == 0U) {
        return 0U;
    }
    return (uint32_t)(numerator / divisor);
}

typedef struct {
    waveform_type_t type;
    uint32_t best_score_ppm;
    uint32_t second_score_ppm;
    uint32_t duty_ppm;
    uint32_t rise_edge_ppm;
    uint32_t fall_edge_ppm;
    uint16_t shift;
} classification_t;

static classification_t classify_fold(uint32_t duty_ppm)
{
    static const uint32_t edge_options[] = {
        0U, 25000U, 50000U, 75000U, MAX_EDGE_PPM
    };
    uint32_t type_scores[4] = {0U, 0U, 0U, 0U};
    uint16_t type_shifts[4] = {0U, 0U, 0U, 0U};
    uint32_t square_rise = 0U;
    uint32_t square_fall = 0U;
    waveform_type_t type;
    classification_t classification;

    classification.type = WAVEFORM_TYPE_UNKNOWN;
    classification.best_score_ppm = 0U;
    classification.second_score_ppm = 0U;
    classification.duty_ppm = duty_ppm;
    classification.rise_edge_ppm = 0U;
    classification.fall_edge_ppm = 0U;
    classification.shift = 0U;

    for (type = WAVEFORM_TYPE_SINE; type <= WAVEFORM_TYPE_SAWTOOTH;
         type = (waveform_type_t)(type + 1)) {
        uint16_t shift;
        if (type == WAVEFORM_TYPE_SQUARE) {
            uint16_t rise_index;
            for (rise_index = 0U;
                 rise_index < (uint16_t)(sizeof(edge_options) /
                                          sizeof(edge_options[0]));
                 ++rise_index) {
                uint16_t fall_index;
                for (fall_index = 0U;
                     fall_index < (uint16_t)(sizeof(edge_options) /
                                              sizeof(edge_options[0]));
                     ++fall_index) {
                    for (shift = 0U; shift < PHASE_BINS; ++shift) {
                        uint32_t score;
                        make_template(type, duty_ppm,
                                      edge_options[rise_index],
                                      edge_options[fall_index], shift);
                        score = correlation_score_ppm();
                        if (score > type_scores[type]) {
                            type_scores[type] = score;
                            type_shifts[type] = shift;
                            square_rise = edge_options[rise_index];
                            square_fall = edge_options[fall_index];
                        }
                    }
                }
            }
        } else {
            for (shift = 0U; shift < PHASE_BINS; ++shift) {
                uint32_t score;
                make_template(type, duty_ppm, 0U, 0U, shift);
                score = correlation_score_ppm();
                if (score > type_scores[type]) {
                    type_scores[type] = score;
                    type_shifts[type] = shift;
                }
            }
        }
    }

    for (type = WAVEFORM_TYPE_SINE; type <= WAVEFORM_TYPE_SAWTOOTH;
         type = (waveform_type_t)(type + 1)) {
        if (type_scores[type] > classification.best_score_ppm) {
            classification.second_score_ppm =
                classification.best_score_ppm;
            classification.best_score_ppm = type_scores[type];
            classification.type = type;
        } else if (type_scores[type] > classification.second_score_ppm) {
            classification.second_score_ppm = type_scores[type];
        }
    }
    if (classification.type == WAVEFORM_TYPE_UNKNOWN) {
        return classification;
    }
    classification.shift = type_shifts[classification.type];
    if (classification.type == WAVEFORM_TYPE_SQUARE) {
        classification.rise_edge_ppm = square_rise;
        classification.fall_edge_ppm = square_fall;
    }
    make_template(classification.type, duty_ppm,
                  classification.rise_edge_ppm,
                  classification.fall_edge_ppm,
                  classification.shift);
    return classification;
}

static uint32_t fitted_vpp_counts_q16(void)
{
    int32_t sum_y = 0;
    int32_t sum_t = 0;
    int64_t covariance = 0;
    uint64_t template_energy = 0U;
    uint64_t positive_covariance;
    uint64_t multiplier = 2048ULL * 65536ULL;
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        sum_y += g_folded[bin];
        sum_t += g_template[bin];
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        int32_t y = ((int32_t)g_folded[bin] * (int32_t)PHASE_BINS) -
                    sum_y;
        int32_t t = ((int32_t)g_template[bin] * (int32_t)PHASE_BINS) -
                    sum_t;
        covariance += (int64_t)y * t;
        template_energy += (uint64_t)((int64_t)t * t);
    }
    if ((covariance <= 0) || (template_energy == 0U)) {
        return 0U;
    }
    positive_covariance = (uint64_t)covariance;
    while (positive_covariance > (UINT64_MAX / multiplier)) {
        positive_covariance >>= 1U;
        template_energy >>= 1U;
    }
    return (uint32_t)((positive_covariance * multiplier +
                       (template_energy / 2U)) / template_energy);
}

static bool validate_frame(const waveform_frame_t *frame,
                           waveform_result_t *result)
{
    if ((frame == NULL) || (frame->samples == NULL)) {
        result->status = WAVEFORM_STATUS_BAD_ARGUMENT;
        return false;
    }
    if ((frame->sample_count < MIN_INPUT_SAMPLES) ||
        (frame->sample_count > WAVEFORM_MAX_SAMPLE_COUNT)) {
        result->status = WAVEFORM_STATUS_INSUFFICIENT_SAMPLES;
        return false;
    }
    if ((frame->adc_full_scale < 255U) ||
        (frame->adc_full_scale > 16383U) ||
        (frame->sample_rate_millihz < MIN_CAPTURE_RATE_MILLIHZ) ||
        (frame->sample_rate_millihz > MAX_CAPTURE_RATE_MILLIHZ) ||
        (frame->adc_reference_microvolts < 1000000UL) ||
        (frame->adc_reference_microvolts > 5500000UL) ||
        (frame->frontend_gain_ppm < 100000UL) ||
        (frame->frontend_gain_ppm > 10000000UL) ||
        ((frame->flags & WAVEFORM_FRAME_FLAG_CONTIGUOUS) == 0U)) {
        result->status = WAVEFORM_STATUS_BAD_METADATA;
        return false;
    }
    return true;
}

bool waveform_analyze_frame(const waveform_frame_t *frame,
                            waveform_result_t *result)
{
    uint16_t low_quantile;
    uint16_t high_quantile;
    uint16_t robust_span;
    uint16_t minimum_span;
    uint16_t max_clip_samples;
    uint32_t period_q12;
    uint32_t refined_period_q12;
    uint32_t crossing_period_q12;
    uint32_t regression_seed_q12;
    uint32_t minimum_period_q12;
    int16_t center;
    int16_t hysteresis;
    uint16_t crossing_count;
    classification_t classification;
    uint32_t vpp_counts_q16;
    uint64_t adc_vpp_microvolts;
    uint64_t input_vpp_microvolts;

    if (result == NULL) {
        return false;
    }
    memset(result, 0, sizeof(*result));
    result->type = WAVEFORM_TYPE_UNKNOWN;
    result->status = WAVEFORM_STATUS_BAD_ARGUMENT;
    result->algorithm_version = WAVEFORM_ALGORITHM_VERSION;
    if (frame != NULL) {
        result->sequence = frame->sequence;
    }
    if (!validate_frame(frame, result)) {
        return false;
    }

    if ((frame->flags & WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED) == 0U) {
        result->quality_flags |= WAVEFORM_QUALITY_CLOCK_UNCALIBRATED;
    }
    if ((frame->flags & WAVEFORM_FRAME_FLAG_VREF_CALIBRATED) == 0U) {
        result->quality_flags |= WAVEFORM_QUALITY_VREF_UNCALIBRATED;
    }

    filter_and_histogram(frame, result);
    if (result->invalid_samples != 0U) {
        result->status = WAVEFORM_STATUS_ADC_SAMPLE_INVALID;
        return false;
    }
    low_quantile = histogram_quantile(frame->sample_count / 20U,
                                      frame->adc_full_scale);
    high_quantile =
        histogram_quantile(((uint32_t)frame->sample_count * 19U) / 20U,
                           frame->adc_full_scale);
    result->dc_offset_counts =
        (uint16_t)(((uint32_t)low_quantile + high_quantile) / 2U);
    robust_span = (uint16_t)(high_quantile - low_quantile);
    minimum_span =
        (uint16_t)(((uint32_t)frame->adc_full_scale * 48U + 2047U) /
                   4095U);
    if (minimum_span < 16U) {
        minimum_span = 16U;
    }
    if (robust_span < minimum_span) {
        result->status = WAVEFORM_STATUS_SIGNAL_TOO_SMALL;
        return false;
    }
    max_clip_samples = (uint16_t)(frame->sample_count / 200U);
    if (result->high_clip_samples > max_clip_samples) {
        result->status = WAVEFORM_STATUS_ADC_HIGH_CLIPPED;
        return false;
    }
    if (result->low_clip_samples > max_clip_samples) {
        result->status = WAVEFORM_STATUS_ADC_LOW_CLIPPED;
        return false;
    }

    if (!estimate_period_amdf(frame, robust_span, &period_q12)) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }
    center = (int16_t)(((int32_t)low_quantile + high_quantile) / 2);
    hysteresis = (int16_t)(robust_span / 8U);
    if (hysteresis < 4) {
        hysteresis = 4;
    }
    minimum_period_q12 =
        (uint32_t)(((uint64_t)frame->sample_rate_millihz * PHASE_SCALE) /
                   MAX_FREQUENCY_MILLIHZ);
    crossing_count = find_rising_crossings(frame->sample_count, center,
                                            hysteresis,
                                            minimum_period_q12);
    crossing_period_q12 = median_crossing_period_q12(crossing_count);
    regression_seed_q12 = period_q12;
    if (crossing_period_q12 != 0U) {
        uint32_t smaller = (crossing_period_q12 < period_q12) ?
                           crossing_period_q12 : period_q12;
        uint32_t larger = (crossing_period_q12 < period_q12) ?
                          period_q12 : crossing_period_q12;
        uint32_t multiple = (larger + (smaller / 2U)) / smaller;
        uint32_t multiple_error =
            absolute_difference_u32(larger, multiple * smaller);
        if (absolute_difference_u32(crossing_period_q12, period_q12) <=
            (period_q12 / 5U)) {
            regression_seed_q12 = crossing_period_q12;
        } else if ((crossing_period_q12 < period_q12) &&
                   (multiple >= 2U) && (multiple <= 3U) &&
                   (multiple_error <= (larger / 8U))) {
            uint32_t crossing_lag =
                (crossing_period_q12 + (PHASE_SCALE / 2U)) >> 12U;
            uint32_t amdf_lag =
                (period_q12 + (PHASE_SCALE / 2U)) >> 12U;
            if ((crossing_lag <= MAX_PERIOD_LAG) &&
                (amdf_lag <= MAX_PERIOD_LAG) &&
                (g_period_cost[crossing_lag] <=
                 (g_period_cost[amdf_lag] +
                  ((uint32_t)robust_span * PERIOD_COST_SCALE / 10U)))) {
                period_q12 = crossing_period_q12;
                regression_seed_q12 = crossing_period_q12;
            }
        }
    }
    refined_period_q12 = period_q12;
    if (refine_period_from_crossings(crossing_count, regression_seed_q12,
                                     &refined_period_q12)) {
        period_q12 = refined_period_q12;
    }

    result->frequency_millihz =
        (uint32_t)((((uint64_t)frame->sample_rate_millihz * PHASE_SCALE) +
                    (period_q12 / 2U)) / period_q12);
    result->period_q16_samples = period_q12 << 4U;
    if ((result->frequency_millihz < MIN_FREQUENCY_MILLIHZ) ||
        (result->frequency_millihz > MAX_FREQUENCY_MILLIHZ)) {
        result->status = WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE;
        return false;
    }

    result->accepted_cycles = fold_cycles(frame, period_q12);
    if (result->accepted_cycles < MIN_VALID_CYCLES) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }
    if (result->accepted_cycles < 8U) {
        result->quality_flags |= WAVEFORM_QUALITY_FEW_CYCLES;
    }

    result->duty_ppm = estimate_duty_ppm();
    classification = classify_fold(result->duty_ppm);
    result->best_score_permille =
        (uint16_t)((classification.best_score_ppm + 500U) / 1000U);
    result->second_score_permille =
        (uint16_t)((classification.second_score_ppm + 500U) / 1000U);
    result->confidence_permille = result->best_score_permille;
    result->model_margin_permille =
        (uint16_t)((classification.best_score_ppm -
                    classification.second_score_ppm + 500U) / 1000U);
    if ((classification.best_score_ppm -
         classification.second_score_ppm) < LOW_MODEL_MARGIN_PPM) {
        result->quality_flags |= WAVEFORM_QUALITY_LOW_MODEL_MARGIN;
    }
    if ((classification.best_score_ppm < MIN_MODEL_SCORE_PPM) ||
        ((classification.best_score_ppm -
          classification.second_score_ppm) < MIN_MODEL_MARGIN_PPM)) {
        result->type = WAVEFORM_TYPE_UNKNOWN;
        result->duty_permille = 0U;
        result->duty_ppm = 0U;
        result->status = WAVEFORM_STATUS_CLASSIFICATION_AMBIGUOUS;
        return false;
    }
    result->type = classification.type;

    vpp_counts_q16 = fitted_vpp_counts_q16();
    result->vpp_counts_q16 = vpp_counts_q16;
    adc_vpp_microvolts =
        (((uint64_t)vpp_counts_q16 * frame->adc_reference_microvolts) +
         ((uint64_t)frame->adc_full_scale * 32768U)) /
        ((uint64_t)frame->adc_full_scale * 65536U);
    input_vpp_microvolts =
        ((adc_vpp_microvolts * WAVEFORM_UNITY_GAIN_PPM) +
         (frame->frontend_gain_ppm / 2U)) /
        frame->frontend_gain_ppm;
    if (input_vpp_microvolts > UINT32_MAX) {
        input_vpp_microvolts = UINT32_MAX;
    }
    result->vpp_microvolts = (uint32_t)input_vpp_microvolts;
    result->vpp_millivolts =
        (uint32_t)((input_vpp_microvolts + 500U) / 1000U);
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        result->duty_ppm = classification.duty_ppm;
        result->duty_permille =
            (uint16_t)((classification.duty_ppm + 500U) / 1000U);
    } else {
        result->duty_ppm = 0U;
        result->duty_permille = 0U;
    }
    result->status = WAVEFORM_STATUS_OK;
    return true;
}

bool waveform_analyze(const uint16_t *samples, waveform_result_t *result)
{
    waveform_frame_t frame;

    frame.samples = samples;
    frame.sample_count = WAVEFORM_SAMPLE_COUNT;
    frame.adc_full_scale = WAVEFORM_ADC_FULL_SCALE;
    frame.sample_rate_millihz = WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ;
    frame.adc_reference_microvolts =
        WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS;
    frame.frontend_gain_ppm = WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM;
    frame.sequence = 0U;
    frame.flags = WAVEFORM_ACQUISITION_FLAGS;
    return waveform_analyze_frame(&frame, result);
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
        "CLASSIFICATION_AMBIGUOUS",
        "BAD_ARGUMENT",
        "BAD_METADATA",
        "INSUFFICIENT_SAMPLES"
    };
    if ((status < WAVEFORM_STATUS_OK) ||
        (status > WAVEFORM_STATUS_INSUFFICIENT_SAMPLES)) {
        return "UNKNOWN_STATUS";
    }
    return names[status];
}
