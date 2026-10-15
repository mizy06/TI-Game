#include "waveform_analyzer.h"

#include <stddef.h>
#include <string.h>

/*
 * 波形识别核心（纯 C99、无动态内存、核心路径不使用浮点数）。
 *
 * 总流程：
 * 输入校验 -> 轻度 FIR 与直方图 -> AMDF 粗周期 -> 穿越点回归精周期
 * -> 多周期折叠成 128 点 -> 四类模板循环匹配 -> 模板拟合求 Vpp。
 *
 * 大工作数组使用静态存储，避免撑爆单片机栈；因此本模块不可重入，也就
 * 是说前一次 waveform_analyze_frame() 返回前不能再次调用它。
 */

/* 把任意完整周期统一表示为 128 个相位桶。桶越多形状越细，但 RAM、计算
 * 量和每桶平均到的样本都会变化，128 是精度与成本的折中。 */
#define PHASE_BINS                    (128U)
/* 直方图只保留高 8 位量级的分辨率，用于快速求 5%/95% 分位数。 */
#define HISTOGRAM_BINS                (256U)
/* 最多记录的上升穿越数。4096 点、最高 402 Hz 下实际远少于此值。 */
#define MAX_CROSSINGS                 (192U)
/* AMDF 允许搜索的最大整数延迟。最低约 98 Hz、最低 10 kHz 采样率时周期
 * 约 102 点，因此 512 留有充分余量。 */
#define MAX_PERIOD_LAG                (512U)
/* 少于 1024 点时记录过短，拒绝分析以保证至少覆盖足够多个周期。 */
#define MIN_INPUT_SAMPLES             (1024U)
/* 折叠至少需要 3 个完整周期，否则平均和模板匹配都不可靠。 */
#define MIN_VALID_CYCLES              (3U)
/* 内部频率门限使用 mHz：98000=98 Hz，402000=402 Hz。比赛名义范围外
 * 留少量余量，避免估计误差把恰好 100/400 Hz 的信号过早排除。 */
#define MIN_FREQUENCY_MILLIHZ         (98000U)
#define MAX_FREQUENCY_MILLIHZ         (402000U)
/* 分类接收门限用 ppm：最佳相关分数至少 0.760，且领先次佳至少 0.005。
 * 低于 0.015 的余量虽然仍可能通过，但会打上质量提示。 */
#define MIN_MODEL_SCORE_PPM           (760000UL)
#define MIN_MODEL_MARGIN_PPM          (5000UL)
#define LOW_MODEL_MARGIN_PPM          (15000UL)
/* 方波模板允许每个边沿最多占一周期的 10%。 */
#define MAX_EDGE_PPM                  (100000UL)
/* Q12 的缩放因子 2^12=4096：整数低 12 位当作小数。 */
#define PHASE_SCALE                   (4096UL)
/* AMDF 平均代价放大 256 倍，保留整数除法会丢掉的小数精度。 */
#define PERIOD_COST_SCALE             (256UL)
/* 对采集元数据的合理性保护：接受 10~50 kHz，用 mHz 表示。 */
#define MAX_CAPTURE_RATE_MILLIHZ      (50000000UL)
#define MIN_CAPTURE_RATE_MILLIHZ      (10000000UL)

/* 全部工作区都由一次分析独占。
 * g_filtered：轻度滤波后的 ADC 码；g_histogram：幅度直方图；
 * g_period_cost：每个候选延迟的 AMDF 代价；g_crossings_q12：上升穿越位置；
 * g_interval_scratch：相邻穿越间隔及排序临时区；
 * g_fold_*：把多个周期按相位叠加时的和、最小、最大；
 * g_folded：最终单周期；g_template：当前候选模板；g_sort_buffer：分位排序。 */
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
static int32_t g_centered_fold[PHASE_BINS];
static int16_t g_sort_buffer[PHASE_BINS];

/* 预先计算的正弦模板：sin(2*pi*(bin+0.5)/128)，Q10 格式。
 * Q10 中 1024 表示 +1.0，-1024 表示 -1.0。查表避免在 MCU 上逐点调用
 * sin()；bin+0.5 表示取每个相位桶的中心。 */
 /*这个模板是干嘛的？*/
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

/* 两个无符号整数的绝对差。不能写 abs(a-b)，因为 a-b 可能先无符号下溢。 */
static uint32_t absolute_difference_u32(uint32_t a, uint32_t b)
{
    return (a >= b) ? (a - b) : (b - a);
}

/* 32 位有符号绝对值。当前调用范围不会出现 INT32_MIN，故取负安全。 */
static int32_t absolute_i32(int32_t value)
{
    return (value < 0) ? -value : value;
}

/* 从 256 桶直方图中估计一个分位点。
 *
 * target 是从低到高累计到第几个样本。例如样本数/20 约等于 5% 分位。
 * 找到桶后返回该桶中心对应的 ADC 码，而不是桶的左边界，可减小量化偏差。
 * full_scale+1 是 ADC 可能的码值总数，例如 4096。 */
static uint16_t histogram_quantile(uint32_t target, uint16_t full_scale)
{
    uint32_t cumulative = 0U;
    uint16_t bin;

    for (bin = 0U; bin < HISTOGRAM_BINS; ++bin) {
        cumulative += g_histogram[bin];
        if (cumulative >= target) {
            /* 桶中心为 (bin+0.5)/256；乘 2 后全程可用整数表示。 */
            uint32_t numerator = ((uint32_t)(2U * bin + 1U) *
                                  ((uint32_t)full_scale + 1U));
            uint32_t value = (numerator + HISTOGRAM_BINS) /
                             (2U * HISTOGRAM_BINS);
            return (value > full_scale) ? full_scale : (uint16_t)value;
        }
    }
    return full_scale;
}

/* 扫描一帧并做输入健康统计，然后施加 1-2-1 三点 FIR。
 *
 * 统计直方图使用原始样本，因为后续幅度分位应该反映采集数据；周期搜索
 * 使用 g_filtered，因为轻度平滑能降低噪声造成的 AMDF 和穿越抖动。
 * result 在进入时已经清零，本函数只累加诊断计数。 */
static void filter_and_histogram(const waveform_frame_t *frame,
                                 waveform_result_t *result)
{
    uint32_t i;
    /* 满量程约 0.1% 作为削顶保护带，且至少 3 个 ADC 码。把极端附近的点
     * 视为可能削顶，比只数恰好等于 0/满量程更符合真实模拟误差。 */
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

    /* 第一遍：校验每点、统计削顶、构建直方图，并复制到滤波工作区。 */
    for (i = 0U; i < frame->sample_count; ++i) {
        uint16_t sample = frame->samples[i];
        uint32_t bin;
        if (sample > frame->adc_full_scale) {
            /* 为保证后面的数组和直方图索引仍安全，记录错误后临时钳到满
             * 量程；主入口稍后会因 invalid_samples 非零而拒绝整帧。 */
            ++result->invalid_samples;
            sample = frame->adc_full_scale;
        }
        if (sample >= high_clip_code) {
            ++result->high_clip_samples;
        }
        if (sample <= guard) {
            ++result->low_clip_samples;
        }
        /* 将 [0, full_scale] 线性映射到 [0,255]。先转 32 位避免乘法溢出。 */
        bin = ((uint32_t)sample * HISTOGRAM_BINS) /
              ((uint32_t)frame->adc_full_scale + 1U);
        if (bin >= HISTOGRAM_BINS) {
            bin = HISTOGRAM_BINS - 1U;
        }
        ++g_histogram[bin];
        g_filtered[i] = (int16_t)sample;
    }

    /* 对称 1-2-1 FIR：y[n]=(x[n-1]+2*x[n]+x[n+1])/4。
     * previous_two/previous_one/current 始终保存原始输入，所以原地写回不会
     * 把已经滤过的值当作下一点输入；否则会意外变成递归滤波器。
     * “+2 再右移 2 位”相当于除以 4 前四舍五入。首尾没有两侧邻居，采用
     * 3:1 的端点权重，仍保持输出长度不变。 */
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

static uint32_t period_cost_for_lag(uint16_t sample_count, uint16_t lag,
                                    uint16_t stride)
{
    /* AMDF（平均幅度差）在延迟 lag 处的代价：
     *     mean(|filtered[i] - filtered[i+lag]|)
     * 真周期处前后形状相似，代价会较小。每隔 2 点取样把计算量减半，
     * 对 100~400 Hz 的低频波形仍有充足数据。sum 用 64 位防止累加溢出。 */
    uint64_t sum = 0U;
    uint32_t count = 0U;
    uint32_t i;

    for (i = 0U; (i + lag) < sample_count; i += stride) {
        sum += absolute_difference_u32((uint16_t)g_filtered[i],
                                       (uint16_t)g_filtered[i + lag]);
        ++count;
    }
    if (count == 0U) {
        return UINT32_MAX;
    }
    /* 放大 256 倍保存小数；加 count/2 后再除相当于四舍五入。 */
    return (uint32_t)((sum * PERIOD_COST_SCALE + (count / 2U)) / count);
}

/* 用 AMDF 在允许频率范围内粗估周期，并做三点抛物线插值获得亚采样精度。
 * 输出 period_q12 的单位是“采样点，Q12”。成功只说明找到候选周期，主流程
 * 还会用穿越点、频率范围和分类结果继续验证。 */
static bool estimate_period_amdf(const waveform_frame_t *frame,
                                 uint16_t robust_span,
                                 uint16_t stride,
                                 uint32_t *period_q12)
{
    /* P=fs/f。最高频率给出最小周期，最低频率给出最大周期；整数除法边界
     * 向外各扩一个点，防止真实周期落在舍入后的搜索区外。 */
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

    /* 先计算每个候选延迟的 AMDF，并找到全局最低代价。 */
    best_lag = min_lag;
    for (lag = min_lag; lag <= max_lag; ++lag) {
        g_period_cost[lag] =
            period_cost_for_lag(frame->sample_count, (uint16_t)lag, stride);
        if (g_period_cost[lag] < best_cost) {
            best_cost = g_period_cost[lag];
            best_lag = lag;
        }
    }

    /* 一个周期的 2 倍、3 倍延迟也会让波形对齐，因此同样可能是 AMDF 谷底。
     * 从小延迟向大延迟找“第一个足够好”的局部最小值，优先保留基波周期，
     * 避免把重复两次或三次的延迟当成真正周期。robust_span 项会让强信号
     * 对绝对噪声有更合理的容忍度。 */
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

    /* 整数 lag 左移 12 位，即转换成 Q12。 */
    *period_q12 = best_lag << 12U;
    if ((best_lag > min_lag) && (best_lag < max_lag)) {
        /* 用谷底左、中、右三个代价拟合抛物线。抛物线顶点相对中点的偏移
         * 为 (left-right)/(2*(left-2*middle+right))。结果限制在 ±0.5 点，
         * 防止平坦或受噪声扰动的谷底产生不可信的大修正。 */
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

/* 在一次“低到高”的阈值穿越附近做线性插值，返回 Q12 采样位置。
 * right_index 是已经位于中心线右/上方的点。函数先向左找到中心线以下的
 * 相邻线段，再按两点直线求精确交点。 */
static uint32_t interpolate_rising_crossing_q12(uint32_t right_index,
                                                 int16_t center)
{
    uint32_t left = right_index - 1U;
    int32_t y0;
    int32_t delta;
    uint32_t fraction_q12;

    /* 滞回触发点可能已经高于真正中心穿越数点，所以向左回溯。 */
    while ((left > 0U) && (g_filtered[left] > center)) {
        --left;
    }
    y0 = g_filtered[left];
    delta = (int32_t)g_filtered[left + 1U] - y0;
    if (delta <= 0) {
        return left << 12U;
    }
    /* fraction=(center-y0)/(y1-y0)，再乘 4096 存为 Q12。 */
    fraction_q12 =
        (uint32_t)((((int32_t)center - y0) * (int32_t)PHASE_SCALE) /
                   delta);
    if (fraction_q12 >= PHASE_SCALE) {
        fraction_q12 = PHASE_SCALE - 1U;
    }
    return (left << 12U) + fraction_q12;
}

/* 在滤波波形中寻找可靠上升穿越。
 *
 * 必须先低于 center-hysteresis 才“武装”，随后高于 center+hysteresis 才
 * 触发。两个阈值之间的死区可阻止噪声在中心附近反复产生假穿越。
 * refractory_q12 为最高允许频率周期的 55%，同一次边沿之后这么短的重复
 * 触发会被忽略。返回实际写入 g_crossings_q12 的数量。 */
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
            /* 与上一次有效穿越保持不应期，避免边沿振铃造成双计数。 */
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

/* 求相邻上升穿越间隔的中位数。
 * 理想情况下相邻间隔就是一个周期。中位数比平均值更不怕偶尔漏检或误检。
 * 样本数很少（最多 191 个间隔），使用实现简单的插入排序足够快，也不需
 * 标准库 qsort 的函数指针和额外代码体积。偶数个元素时取靠上的中位数。 */
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
    /* 插入排序：左侧 [0,i) 始终有序，把当前 value 插入正确位置。 */
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

/* 判断两个穿越之间跨了几个周期。
 *
 * 噪声可能导致漏掉一两个上升沿，所以间隔可能约为 1P、2P 或 3P。先四舍
 * 五入得到周期数，再要求误差不超过期望值的 20%；不可信时返回 0。 */
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

/* 用一串上升穿越做最小二乘直线回归，精修周期。
 *
 * 把“累计周期编号”作为 x，把“Q12 采样位置”作为 y，模型为 y=a+b*x。
 * 斜率 b 就是每周期采样点数。使用多个远距离穿越点后，单个点的插值误差
 * 对斜率影响会显著变小。函数先挑出最长的一段连续可信穿越，跳过坏点。 */
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

    /* 少于 4 点无法形成足够稳定的多周期回归。 */
    if (crossing_count < 4U) {
        return false;
    }
    /* 第一遍：把可解释为 1~3 个周期的相邻间隔连成若干段，选择点数最多、
     * 若点数相同则跨周期最多的一段。 */
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

    /* 第二遍：累加回归公式需要的 Σx、Σx²、Σy、Σxy。
     * cycle 会在漏检时增加 2 或 3，而不是错误地把下一个点当作相邻周期。 */
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
    /* 斜率 b = (nΣxy-ΣxΣy)/(nΣx²-(Σx)²)。所有 y 都是 Q12，所以 b 也
     * 自然保持 Q12。 */
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
    /* 精修值必须位于 AMDF 粗值的 ±20%，否则更可能是错误穿越或谐波。 */
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
    /* 在原始 ADC 数组的任意 Q12 位置做两点线性插值。
     * index 是整数部分，fraction 是 0~4095 的小数部分。例如 10.5 点会在
     * samples[10] 和 samples[11] 中间取值。折叠阶段用原始点而非滤波点，
     * 避免滤波改变方波边沿和幅度。 */
    uint32_t index = position_q12 >> 12U;
    uint32_t fraction = position_q12 & (PHASE_SCALE - 1U);
    uint32_t y0;
    int32_t delta;
    int32_t value;

    /* 最后一个点没有右邻点，直接返回末值，避免读取越界。 */
    if (index >= ((uint32_t)frame->sample_count - 1U)) {
        return frame->samples[frame->sample_count - 1U];
    }
    y0 = frame->samples[index];
    delta = (int32_t)frame->samples[index + 1U] - (int32_t)y0;
    value = (int32_t)y0 +
            (int32_t)((delta * (int32_t)fraction) >> 12U);
    /* 正常线性插值应落在端点之间；钳位是对整数舍入和坏输入的防御。 */
    if (value < 0) {
        value = 0;
    } else if (value > frame->adc_full_scale) {
        value = frame->adc_full_scale;
    }
    return (uint16_t)value;
}

/* 将许多个周期相位对齐并平均成一个 128 点“代表周期”。
 *
 * 每个相位桶在周期的桶中心取样，而非边界取样；最后一个桶也必须仍位于
 * 数组倒数第二点之前，才能安全插值。不足一个完整周期的尾部直接舍弃。
 * 返回真正折叠的完整周期数。 */
static uint16_t fold_cycles(const waveform_frame_t *frame,
                            uint32_t period_q12)
{
    uint32_t cycle;
    uint16_t accepted = 0U;
    uint16_t bin;
    const uint32_t last_position_q12 =
        ((uint32_t)frame->sample_count - 1U) << 12U;

    /* 每次调用都重置累加、最小、最大工作区。 */
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
        /* position = 周期起点 + (bin+0.5)/128 * 周期长度。写成
         * (2*bin+1)/(2*PHASE_BINS) 可避免浮点数。 */
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
            /* 至少 5 周期时，逐桶去掉一次最大和最小再平均，即截尾平均。
             * 偶发尖峰只会污染某一周期，不易拉动最终代表波形。 */
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

/* 复制并升序排列 128 点折叠波形，供高低电平的稳健估计使用。
 * 不直接排序 g_folded，因为它的时间/相位顺序还要用于边沿和模板比较。 */
static void sort_folded(void)
{
    uint16_t i;

    memcpy(g_sort_buffer, g_folded, sizeof(g_folded));
    /* 128 点规模很小，插入排序简单、确定且不需要动态内存。 */
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

/* 求折叠波形在 bin 与下一桶之间穿过 threshold 的循环 Q12 相位。
 * 最后一个桶的下一桶是 0，所以使用取模形成闭合周期。桶样本位于中心，
 * 因而基础相位加半个桶；函数结果范围为 [0,128*4096)。 */
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

/* 从代表周期估计“高于阈值”的持续比例，结果单位 ppm。
 *
 * 先排序，取最低 1/16 与最高 1/16 的均值作为低/高平台，二者中点作为
 * 阈值。再寻找所有上升/下降交点。噪声或振铃可能给出多对边沿，因此用
 * 逐桶统计得到的粗占空比 target_ppm，挑选最接近它的上升-下降组合。 */
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

    /* 扫描闭合周期，同时数高电平桶并记录两种方向的阈值交点。最多保留
     * 8 个各向交点，远多于理想方波所需的 1+1。 */
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
    /* above/128 转成百万分比，并四舍五入。 */
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
            /* 排除小于 10% 或大于 90% 的荒谬配对；比赛关注 20%~80%。 */
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

/* 在长度为 1,000,000 ppm 的圆周上求有符号最短距离。
 * 例如 phase=990000、center=10000，线性相减是 980000，但跨周期边界的
 * 真正最短距离是 -20000。方波在相位 0 处有边沿，必须正确处理首尾相邻。 */
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

/* 生成某一相位的方波模板值，Q10 范围约为 [-1024,+1024]。
 *
 * 理想方波边沿宽度为 0；真实电路带宽有限时边沿是一段斜坡。rise/fall
 * 可以各自不同，故分别建模。位于斜坡区时做线性变化；位于高平台返回
 * +1024，其余低平台返回 -1024。 */
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

/* 构造一种候选波形的 128 点 Q10 模板，并循环移动 shift 个桶。
 *
 * 输入记录可能从周期任何相位开始，模板必须尝试全部循环位移。正弦用
 * 查表；方波调用可变占空比/边沿模型；三角和锯齿用分段一次公式生成。
 * phase_u8=2*bin+1 把桶中心表示成 0~255 的半桶单位。 */
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
            /* 一周分成上升 1/4、下降 1/2、再上升 1/4，首尾连续。 */
            if (phase_u8 < 64U) {
                value = (int16_t)(16 * (int16_t)phase_u8);
            } else if (phase_u8 < 192U) {
                value = (int16_t)(2048 - (16 * (int16_t)phase_u8));
            } else {
                value = (int16_t)((16 * (int16_t)phase_u8) - 4096);
            }
            break;
        case WAVEFORM_TYPE_SAWTOOTH:
            /* 从接近 +1024 线性降到接近 -1024。 */
            value = (int16_t)(1024 - (8 * (int16_t)phase_u8));
            break;
        default:
            value = 0;
            break;
        }
        g_template[bin] = value;
    }
}

/* 计算折叠波形与当前模板的去均值归一化相关分数平方，单位 ppm。
 *
 * 普通相关系数 r = dot/sqrt(Ey*Et)。为避免开平方和浮点数，这里直接算
 * r^2 = dot^2/(Ey*Et)。先减均值，所以直流偏置不影响形状评分；再除能量，
 * 所以整体幅度不同也不影响评分。负相关表示极性/相位不合，直接记 0。 */
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

    /* 第一遍求均值所需的和；第二遍用 PHASE_BINS*y-sum 的等比例形式去
     * 均值，完全避免整数除法。这个共同比例会在归一化时抵消。 */
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
    /* 三个量同步右移不会改变比值，目的是让后面的平方/乘法留出位宽余量。 */
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
    /* 要求 numerator/denominator * 1,000,000。先把 denominator 向上除以
     * 一百万，可避免 numerator 再乘一百万溢出；代价是极小量化误差。 */
    divisor = (denominator + 999999UL) / 1000000UL;
    if (divisor == 0U) {
        return 0U;
    }
    return (uint32_t)(numerator / divisor);
}

typedef struct {
    /* 内部分类搜索结果：最终类别、最佳/次佳分数，以及重建最佳模板所需的
     * 方波占空比、两侧边沿宽度和循环相移。全部 ppm 值除以 1e6 得比例。 */
    waveform_type_t type;
    uint32_t best_score_ppm;
    uint32_t second_score_ppm;
    uint32_t duty_ppm;
    uint32_t rise_edge_ppm;
    uint32_t fall_edge_ppm;
    uint16_t shift;
} classification_t;

/* 穷举四类模板及其可能相位，选择相关分数最高的类别。
 *
 * 正弦/三角/锯齿各尝试 128 个循环相移。方波额外尝试 5 种上升边沿宽度
 * ×5 种下降边沿宽度×128 个相移，以容纳模拟前端造成的非理想边沿。
 * 返回最佳和次佳分数，主流程再依据绝对分数及二者差值决定是否可信。 */
static classification_t classify_fold(uint32_t duty_ppm)
{
    /* 边沿占一周期的 0%、2.5%、5%、7.5% 或 10%。 */
    static const uint32_t edge_options[] = {
        0U, 25000U, 50000U, 75000U, MAX_EDGE_PPM
    };
    uint32_t type_scores[4] = {0U, 0U, 0U, 0U};
    uint16_t type_shifts[4] = {0U, 0U, 0U, 0U};
    uint32_t square_rise = 0U;
    uint32_t square_fall = 0U;
    waveform_type_t type;
    classification_t classification;

    /* 先给所有字段确定的安全初值，避免某类完全不得分时留下未初始化值。 */
    classification.type = WAVEFORM_TYPE_UNKNOWN;
    classification.best_score_ppm = 0U;
    classification.second_score_ppm = 0U;
    classification.duty_ppm = duty_ppm;
    classification.rise_edge_ppm = 0U;
    classification.fall_edge_ppm = 0U;
    classification.shift = 0U;

    /* 第一阶段：分别找出每个类别能达到的最高分和对应参数。 */
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

    /* 第二阶段：在四个类别冠军之间找总冠军和亚军。 */
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
    /* 把 g_template 恢复为最终最佳模板，后面的 Vpp 拟合直接使用它。 */
    make_template(classification.type, duty_ppm,
                  classification.rise_edge_ppm,
                  classification.fall_edge_ppm,
                  classification.shift);
    return classification;
}

/* Fast path: template energy is invariant under circular shift. Build each
 * shape once, find the largest positive covariance over its 128 rotations,
 * and perform the expensive score division once per shape (28 total rather
 * than 3584). All products retain the legacy 64-bit scoring scale. */
static uint32_t fast_shape_score(uint64_t energy_y, uint16_t *best_shift)
{
    int32_t sum_t = 0;
    uint64_t energy_t = 0U;
    int64_t best_dot = 0;
    uint16_t shift;
    uint16_t bin;
    uint64_t dot;
    uint64_t numerator;
    uint64_t denominator;
    uint64_t divisor;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        sum_t += g_template[bin];
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        int32_t t = (int32_t)PHASE_BINS * g_template[bin] - sum_t;
        energy_t += (uint64_t)((int64_t)t * t);
    }
    *best_shift = 0U;
    for (shift = 0U; shift < PHASE_BINS; ++shift) {
        int64_t raw_dot = 0;
        for (bin = 0U; bin < PHASE_BINS; ++bin) {
            raw_dot += (int64_t)g_centered_fold[bin] *
                       g_template[(bin + shift) & (PHASE_BINS - 1U)];
        }
        /* sum(g_centered_fold) is exactly zero: subtracting template's
         * mean cancels, leaving one factor of PHASE_BINS. */
        if (raw_dot > best_dot) {
            best_dot = raw_dot;
            *best_shift = shift;
        }
    }
    if ((best_dot <= 0) || (energy_y == 0U) || (energy_t == 0U)) {
        return 0U;
    }
    dot = (uint64_t)best_dot * PHASE_BINS;
    while ((dot > 0x3FFFFFFFUL) ||
           (energy_y > 0x3FFFFFFFUL) ||
           (energy_t > 0x3FFFFFFFUL)) {
        dot >>= 1U;
        energy_y >>= 1U;
        energy_t >>= 1U;
    }
    numerator = dot * dot;
    denominator = energy_y * energy_t;
    if (numerator >= denominator) {
        return 1000000UL;
    }
    divisor = (denominator + 999999UL) / 1000000UL;
    return divisor == 0U ? 0U : (uint32_t)(numerator / divisor);
}

static classification_t classify_fold_fast(uint32_t duty_ppm)
{
    static const uint32_t edges[] = {
        0U, 25000U, 50000U, 75000U, MAX_EDGE_PPM
    };
    uint32_t scores[4] = {0U, 0U, 0U, 0U};
    uint16_t shifts[4] = {0U, 0U, 0U, 0U};
    uint32_t square_rise = 0U;
    uint32_t square_fall = 0U;
    int32_t sum_y = 0;
    uint64_t energy_y = 0U;
    waveform_type_t type;
    classification_t answer = { WAVEFORM_TYPE_UNKNOWN, 0U, 0U,
                                duty_ppm, 0U, 0U, 0U };
    uint16_t bin;

    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        sum_y += g_folded[bin];
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        int32_t y = (int32_t)PHASE_BINS * g_folded[bin] - sum_y;
        g_centered_fold[bin] = y;
        energy_y += (uint64_t)((int64_t)y * y);
    }
    for (type = WAVEFORM_TYPE_SINE; type <= WAVEFORM_TYPE_SAWTOOTH;
         type = (waveform_type_t)(type + 1)) {
        if (type == WAVEFORM_TYPE_SQUARE) {
            uint16_t rise;
            for (rise = 0U; rise < 5U; ++rise) {
                uint16_t fall;
                for (fall = 0U; fall < 5U; ++fall) {
                    uint16_t shift;
                    uint32_t score;
                    make_template(type, duty_ppm, edges[rise], edges[fall], 0U);
                    score = fast_shape_score(energy_y, &shift);
                    if (score > scores[type]) {
                        scores[type] = score;
                        shifts[type] = shift;
                        square_rise = edges[rise];
                        square_fall = edges[fall];
                    }
                }
            }
        } else {
            make_template(type, duty_ppm, 0U, 0U, 0U);
            scores[type] = fast_shape_score(energy_y, &shifts[type]);
        }
    }
    for (type = WAVEFORM_TYPE_SINE; type <= WAVEFORM_TYPE_SAWTOOTH;
         type = (waveform_type_t)(type + 1)) {
        if (scores[type] > answer.best_score_ppm) {
            answer.second_score_ppm = answer.best_score_ppm;
            answer.best_score_ppm = scores[type];
            answer.type = type;
        } else if (scores[type] > answer.second_score_ppm) {
            answer.second_score_ppm = scores[type];
        }
    }
    if (answer.type != WAVEFORM_TYPE_UNKNOWN) {
        answer.shift = shifts[answer.type];
        if (answer.type == WAVEFORM_TYPE_SQUARE) {
            answer.rise_edge_ppm = square_rise;
            answer.fall_edge_ppm = square_fall;
        }
        make_template(answer.type, duty_ppm, answer.rise_edge_ppm,
                      answer.fall_edge_ppm, answer.shift);
    }
    return answer;
}

/* 把分类结果压成一个只用于“周期候选之间比较”的排序值。绝对相关分数是
 * 主证据，最佳与次佳之差作为同分时的消歧证据。 */
static uint64_t classification_rank(const classification_t *classification)
{
    uint32_t margin = classification->best_score_ppm -
                      classification->second_score_ppm;
    return ((uint64_t)classification->best_score_ppm * 1000000ULL) + margin;
}

static bool period_candidate_is_valid(const waveform_frame_t *frame,
                                      uint32_t period_q12)
{
    uint32_t frequency_millihz;

    if (period_q12 < (2U * PHASE_SCALE)) {
        return false;
    }
    frequency_millihz =
        (uint32_t)(((uint64_t)frame->sample_rate_millihz * PHASE_SCALE +
                    (period_q12 / 2U)) / period_q12);
    return (frequency_millihz >= MIN_FREQUENCY_MILLIHZ) &&
           (frequency_millihz <= MAX_FREQUENCY_MILLIHZ);
}

static void append_period_candidate(uint32_t *candidates,
                                    uint16_t *candidate_count,
                                    uint32_t candidate,
                                    const waveform_frame_t *frame)
{
    uint16_t index;

    if (!period_candidate_is_valid(frame, candidate)) {
        return;
    }
    for (index = 0U; index < *candidate_count; ++index) {
        /* 相差不到约 0.4% 的候选视为同一个周期，避免重复跑完整模板搜索。 */
        if (absolute_difference_u32(candidates[index], candidate) <=
            (candidate / 256U + 1U)) {
            return;
        }
    }
    if (*candidate_count < 12U) {
        candidates[(*candidate_count)++] = candidate;
    }
}

/* 第二种算法的倍频消歧层。
 *
 * AMDF 在 P、2P、3P... 都可能出现谷底；相干干扰还会让错误倍周期比真周期
 * 更深。正常帧沿用快速路径，只有初始模型证据偏弱或 AMDF/穿越明显冲突时，
 * 才把当前周期及其 1/2、1/3、1/4、2 倍候选和穿越候选逐一折叠，并用四类
 * 全局模板证据选出最可信的基波。这使算法 2 与原算法 1 有真实可测的行为
 * 差异，同时不让普通 200 题为极端情况付出额外计算量。 */
static uint32_t select_global_harmonic_period(
    const waveform_frame_t *frame,
    uint32_t current_period_q12,
    uint32_t crossing_period_q12,
    const classification_t *current_classification,
    bool fast)
{
    uint32_t candidates[12];
    uint16_t candidate_count = 0U;
    uint16_t index;
    uint32_t selected_period = current_period_q12;
    uint64_t selected_rank = classification_rank(current_classification);
    uint32_t current_margin = current_classification->best_score_ppm -
                              current_classification->second_score_ppm;
    bool weak_model =
        (current_classification->best_score_ppm < MIN_MODEL_SCORE_PPM) ||
        (current_margin < MIN_MODEL_MARGIN_PPM);

    /* 已经能通过原有硬门限的帧保持 2.0 路径不变；全局搜索只救援原本会
     * 安全拒识的倍频歧义帧，避免噪声穿越偶发偏差改变一个本来可靠的结果。 */
    if (!weak_model) {
        return current_period_q12;
    }

    append_period_candidate(candidates, &candidate_count,
                            current_period_q12, frame);
    append_period_candidate(candidates, &candidate_count,
                            current_period_q12 / 2U, frame);
    append_period_candidate(candidates, &candidate_count,
                            current_period_q12 / 3U, frame);
    append_period_candidate(candidates, &candidate_count,
                            current_period_q12 / 4U, frame);
    if (current_period_q12 <= (UINT32_MAX / 2U)) {
        append_period_candidate(candidates, &candidate_count,
                                current_period_q12 * 2U, frame);
    }
    if (crossing_period_q12 != 0U) {
        append_period_candidate(candidates, &candidate_count,
                                crossing_period_q12, frame);
        append_period_candidate(candidates, &candidate_count,
                                crossing_period_q12 / 2U, frame);
        if (crossing_period_q12 <= (UINT32_MAX / 2U)) {
            append_period_candidate(candidates, &candidate_count,
                                    crossing_period_q12 * 2U, frame);
        }
    }

    for (index = 0U; index < candidate_count; ++index) {
        uint32_t duty_ppm;
        classification_t candidate_classification;
        uint64_t rank;

        if (candidates[index] == current_period_q12) {
            continue;
        }
        if (fold_cycles(frame, candidates[index]) < MIN_VALID_CYCLES) {
            continue;
        }
        duty_ppm = estimate_duty_ppm();
        candidate_classification = fast ? classify_fold_fast(duty_ppm) :
                                          classify_fold(duty_ppm);
        rank = classification_rank(&candidate_classification);
        if (rank > selected_rank) {
            selected_rank = rank;
            selected_period = candidates[index];
        }
    }
    return selected_period;
}

/* 在最佳模板已确定后，用一元最小二乘求输入波形的峰峰 ADC 码，Q16。
 *
 * 模型是 y ≈ offset + scale*t。去均值后最佳 scale=cov(y,t)/energy(t)。
 * 模板峰峰值是 2048 个 Q10 单位，所以乘 2048 把 scale 换成完整 Vpp；
 * 再乘 65536 保存为 Q16。若协方差非正，说明方向不匹配，返回 0。 */
static uint32_t fitted_vpp_counts_q16(waveform_type_t type)
{
    int32_t sum_y = 0;
    int32_t sum_t = 0;
    int64_t covariance = 0;
    uint64_t template_energy = 0U;
    uint64_t positive_covariance;
    uint64_t multiplier = 2048ULL * 65536ULL;
    uint16_t bin;
    uint16_t used = 0U;

    /* 与相关计算一样，使用 N*y-sum 的等比例去均值形式，避免先做除法。 */
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        /* Interpolating across a saw reset rounds its discontinuity. Fit the
         * linear ramp only, then extrapolate its slope to a whole period. The
         * classification score still uses the complete waveform. */
        if ((type == WAVEFORM_TYPE_SAWTOOTH) &&
            (absolute_i32(g_template[bin]) > 800)) {
            continue;
        }
        sum_y += g_folded[bin];
        sum_t += g_template[bin];
        ++used;
    }
    for (bin = 0U; bin < PHASE_BINS; ++bin) {
        if ((type == WAVEFORM_TYPE_SAWTOOTH) &&
            (absolute_i32(g_template[bin]) > 800)) {
            continue;
        }
        int32_t y = ((int32_t)g_folded[bin] * (int32_t)used) -
                    sum_y;
        int32_t t = ((int32_t)g_template[bin] * (int32_t)used) -
                    sum_t;
        covariance += (int64_t)y * t;
        template_energy += (uint64_t)((int64_t)t * t);
    }
    if ((covariance <= 0) || (template_energy == 0U)) {
        return 0U;
    }
    positive_covariance = (uint64_t)covariance;
    /* 若 covariance*multiplier 可能溢出，则分子和分母同步除以 2，保持比值。 */
    while (positive_covariance > (UINT64_MAX / multiplier)) {
        positive_covariance >>= 1U;
        template_energy >>= 1U;
    }
    return (uint32_t)((positive_covariance * multiplier +
                       (template_energy / 2U)) / template_energy);
}

/* 检查一帧在进入大规模计算前是否满足接口契约，并把失败原因写入 result。
 * 范围检查的主要目的是捕捉单位写错和明显不可能的硬件参数，而不是限制
 * 算法只能在默认值运行。 */
static bool validate_frame(const waveform_frame_t *frame,
                           waveform_result_t *result)
{
    /* frame 和样本指针都必须存在。result 已由调用者先检查，不会为 NULL。 */
    if ((frame == NULL) || (frame->samples == NULL)) {
        result->status = WAVEFORM_STATUS_BAD_ARGUMENT;
        return false;
    }
    /* 过短不可靠，过长会写出静态工作区。 */
    if ((frame->sample_count < MIN_INPUT_SAMPLES) ||
        (frame->sample_count > WAVEFORM_MAX_SAMPLE_COUNT)) {
        result->status = WAVEFORM_STATUS_INSUFFICIENT_SAMPLES;
        return false;
    }
    /* 接受 8~14 位 ADC、10~50 kHz 采样、1~5.5 V 参考和 0.1~10 倍前端。
     * CONTIGUOUS 必须设置，因为所有周期公式都假设相邻数组元素等时间间隔。 */
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

/* 算法主入口。建议第一次阅读本文件时先只看本函数，再按调用顺序回看上方
 * 各辅助函数。每个失败分支都会设置明确 status 并立即返回 false。 */
bool waveform_analyze_frame_with_algorithm(const waveform_frame_t *frame,
                                           waveform_result_t *result,
                                           waveform_algorithm_t algorithm)
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

    /* result 自己为空时连错误状态都无处可写，只能直接返回。 */
    if (result == NULL) {
        return false;
    }
    /* 无论成功失败都先清零全部字段，保证调用者不会看到上一次分析残值。 */
    memset(result, 0, sizeof(*result));
    result->type = WAVEFORM_TYPE_UNKNOWN;
    result->status = WAVEFORM_STATUS_BAD_ARGUMENT;
    result->algorithm_version = WAVEFORM_ALGORITHM_VERSION;
    if (frame != NULL) {
        result->sequence = frame->sequence;
    }
    if (algorithm == WAVEFORM_ALGORITHM_CASCADE) {
        return waveform_analyze_cascade(frame, result, NULL);
    }
    if ((algorithm != WAVEFORM_ALGORITHM_LEGACY_AMDF_CROSSING) &&
        (algorithm != WAVEFORM_ALGORITHM_GLOBAL_HARMONIC_MODEL) &&
        (algorithm != WAVEFORM_ALGORITHM_FAST_PHASE_MODEL)) {
        return false;
    }
    if (!validate_frame(frame, result)) {
        return false;
    }

    /* 未校准不是算法硬失败，但必须在质量标志中如实提示。 */
    if ((frame->flags & WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED) == 0U) {
        result->quality_flags |= WAVEFORM_QUALITY_CLOCK_UNCALIBRATED;
    }
    if ((frame->flags & WAVEFORM_FRAME_FLAG_VREF_CALIBRATED) == 0U) {
        result->quality_flags |= WAVEFORM_QUALITY_VREF_UNCALIBRATED;
    }

    /* 阶段 1：输入健康检查、幅度直方图与轻度 FIR。 */
    filter_and_histogram(frame, result);
    if (result->invalid_samples != 0U) {
        result->status = WAVEFORM_STATUS_ADC_SAMPLE_INVALID;
        return false;
    }
    /* 5% 和 95% 分位对单个噪声尖峰不敏感。二者中点近似直流偏置，差值
     * robust_span 近似主体摆幅，仅用于门限/粗判断，最终 Vpp 由模板拟合。 */
    low_quantile = histogram_quantile(frame->sample_count / 20U,
                                      frame->adc_full_scale);
    high_quantile =
        histogram_quantile(((uint32_t)frame->sample_count * 19U) / 20U,
                           frame->adc_full_scale);
    result->dc_offset_counts =
        (uint16_t)(((uint32_t)low_quantile + high_quantile) / 2U);
    robust_span = (uint16_t)(high_quantile - low_quantile);
    /* 以 12 位 ADC 的 48 码为基准，按实际满量程等比例缩放，且不低于
     * 16 码。小于该跨度时噪声与量化足以主导形状，安全拒绝。 */
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
    /* 允许最多约 0.5% 的点进入上下削顶保护带；更多意味着波形形状/幅度
     * 已明显失真。高低端分别报告，便于检查直流偏置。 */
    max_clip_samples = (uint16_t)(frame->sample_count / 200U);
    if (result->high_clip_samples > max_clip_samples) {
        result->status = WAVEFORM_STATUS_ADC_HIGH_CLIPPED;
        return false;
    }
    if (result->low_clip_samples > max_clip_samples) {
        result->status = WAVEFORM_STATUS_ADC_LOW_CLIPPED;
        return false;
    }

    /* 阶段 2：AMDF 给出对任意四类形状都适用的粗周期。 */
    if (!estimate_period_amdf(frame, robust_span,
                              algorithm == WAVEFORM_ALGORITHM_FAST_PHASE_MODEL
                                  ? 8U : 2U, &period_q12)) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }
    /* 阶段 3：围绕稳健中心寻找上升穿越。滞回取主体幅度的 1/8，且至少
     * 4 个 ADC 码，避免量化噪声频繁触发。 */
    center = (int16_t)(((int32_t)low_quantile + high_quantile) / 2);
    hysteresis = (int16_t)(robust_span / 8U);
    if (hysteresis < 4) {
        hysteresis = 4;
    }
    /* 用最高允许频率算最短合法周期，供穿越检测的不应期使用。 */
    minimum_period_q12 =
        (uint32_t)(((uint64_t)frame->sample_rate_millihz * PHASE_SCALE) /
                   MAX_FREQUENCY_MILLIHZ);
    crossing_count = find_rising_crossings(frame->sample_count, center,
                                            hysteresis,
                                            minimum_period_q12);
    crossing_period_q12 = median_crossing_period_q12(crossing_count);
    regression_seed_q12 = period_q12;
    if (crossing_period_q12 != 0U) {
        /* AMDF 与中心穿越是两种独立线索：
         * - 相差不超过 20% 时，用穿越中位数作为回归种子；
         * - 若穿越周期约为 AMDF 的 1/2 或 1/3，可能 AMDF 选到了整数倍周期。
         *   只有较短候选的 AMDF 代价也足够好时才改用它，避免被噪声翻倍。 */
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
    /* 多穿越线性回归成功时，用亚采样精度周期覆盖粗估值；失败则保留 AMDF。 */
    refined_period_q12 = period_q12;
    if (refine_period_from_crossings(crossing_count, regression_seed_q12,
                                     &refined_period_q12)) {
        period_q12 = refined_period_q12;
    }

    /* f=fs/P。fs 是 mHz，P 是 Q12，因此分子乘 4096；加 P/2 后除是
     * 四舍五入。Q12 周期左移 4 位即得到对外约定的 Q16 周期。 */
    result->frequency_millihz =
        (uint32_t)((((uint64_t)frame->sample_rate_millihz * PHASE_SCALE) +
                    (period_q12 / 2U)) / period_q12);
    result->period_q16_samples = period_q12 << 4U;
    if ((result->frequency_millihz < MIN_FREQUENCY_MILLIHZ) ||
        (result->frequency_millihz > MAX_FREQUENCY_MILLIHZ)) {
        result->status = WAVEFORM_STATUS_FREQUENCY_OUT_OF_RANGE;
        return false;
    }

    /* 阶段 4：把所有完整周期折叠平均为单个 128 点代表周期。 */
    result->accepted_cycles = fold_cycles(frame, period_q12);
    if (result->accepted_cycles < MIN_VALID_CYCLES) {
        result->status = WAVEFORM_STATUS_PERIOD_NOT_FOUND;
        return false;
    }
    if (result->accepted_cycles < 8U) {
        /* 3~7 周期仍可分析，但平均降噪能力较弱，向调用方发出质量提示。 */
        result->quality_flags |= WAVEFORM_QUALITY_FEW_CYCLES;
    }

    /* 阶段 5：先估计潜在方波占空比，再让四类模板公平竞争。非方波虽然也
     * 会产生一个临时占空比，最终输出时会清零。 */
    result->duty_ppm = estimate_duty_ppm();
    classification = algorithm == WAVEFORM_ALGORITHM_FAST_PHASE_MODEL ?
                     classify_fold_fast(result->duty_ppm) :
                     classify_fold(result->duty_ppm);
    if ((algorithm == WAVEFORM_ALGORITHM_GLOBAL_HARMONIC_MODEL) ||
        (algorithm == WAVEFORM_ALGORITHM_FAST_PHASE_MODEL)) {
        bool fast_rescue = (classification.best_score_ppm < MIN_MODEL_SCORE_PPM) ||
            ((classification.best_score_ppm - classification.second_score_ppm)
             < MIN_MODEL_MARGIN_PPM);
        uint32_t selected_period_q12 = select_global_harmonic_period(
            frame, period_q12, crossing_period_q12, &classification,
            algorithm == WAVEFORM_ALGORITHM_FAST_PHASE_MODEL);
        if (selected_period_q12 != period_q12) {
            period_q12 = selected_period_q12;
            result->frequency_millihz =
                (uint32_t)((((uint64_t)frame->sample_rate_millihz *
                              PHASE_SCALE) + (period_q12 / 2U)) /
                           period_q12);
            result->period_q16_samples = period_q12 << 4U;
        }
        /* The fast path keeps the initial folded frame if the normal case
         * needs no rescue. Legacy algorithm 2 retains its original flow. */
        if ((algorithm != WAVEFORM_ALGORITHM_FAST_PHASE_MODEL) ||
            fast_rescue) {
            result->accepted_cycles = fold_cycles(frame, period_q12);
            result->duty_ppm = estimate_duty_ppm();
            classification = algorithm == WAVEFORM_ALGORITHM_FAST_PHASE_MODEL ?
                             classify_fold_fast(result->duty_ppm) :
                             classify_fold(result->duty_ppm);
        }
    }
    /* 内部 ppm 分数转换为对外较紧凑的 permille，并四舍五入。 */
    result->best_score_permille =
        (uint16_t)((classification.best_score_ppm + 500U) / 1000U);
    result->second_score_permille =
        (uint16_t)((classification.second_score_ppm + 500U) / 1000U);
    result->confidence_permille = result->best_score_permille;
    result->model_margin_permille =
        (uint16_t)((classification.best_score_ppm -
                    classification.second_score_ppm + 500U) / 1000U);
    /* 最佳与次佳接近时设置软质量标志；再用稍宽松的硬门限决定是否拒识。
     * “不知道”比自信地报错类别更安全。 */
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

    /* 阶段 6：以已经选中的最佳模板拟合峰峰幅度。 */
    vpp_counts_q16 = fitted_vpp_counts_q16(result->type);
    if (result->type == WAVEFORM_TYPE_SAWTOOTH) {
        result->quality_flags |= WAVEFORM_QUALITY_RAMP_AMPLITUDE;
    }
    result->vpp_counts_q16 = vpp_counts_q16;
    /* ADC 码转 ADC 引脚电压：
     * vpp_uv = vpp_counts_q16/65536/full_scale * reference_uv。
     * 分子分母都用 64 位，分母一半用于整数四舍五入。 */
    adc_vpp_microvolts =
        (((uint64_t)vpp_counts_q16 * frame->adc_reference_microvolts) +
         ((uint64_t)frame->adc_full_scale * 32768U)) /
        ((uint64_t)frame->adc_full_scale * 65536U);
    /* 再除以前端增益得到信号源端幅度。增益用 ppm，所以分子乘 1,000,000。 */
    input_vpp_microvolts =
        ((adc_vpp_microvolts * WAVEFORM_UNITY_GAIN_PPM) +
         (frame->frontend_gain_ppm / 2U)) /
        frame->frontend_gain_ppm;
    /* 对异常但仍通过元数据范围的组合做饱和，避免 64->32 位截断回绕。 */
    if (input_vpp_microvolts > UINT32_MAX) {
        input_vpp_microvolts = UINT32_MAX;
    }
    result->vpp_microvolts = (uint32_t)input_vpp_microvolts;
    result->vpp_millivolts =
        (uint32_t)((input_vpp_microvolts + 500U) / 1000U);
    /* 占空比只对方波有定义。其他类别明确清零，避免误读临时估计值。 */
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        result->duty_ppm = classification.duty_ppm;
        result->duty_permille =
            (uint16_t)((classification.duty_ppm + 500U) / 1000U);
    } else {
        result->duty_ppm = 0U;
        result->duty_permille = 0U;
    }
    /* 到这里所有硬门禁都已通过，最后才设置 OK。 */
    result->status = WAVEFORM_STATUS_OK;
    return true;
}

/* Production cascade; legacy algorithms remain explicitly callable for RT1-3. */
bool waveform_analyze_frame(const waveform_frame_t *frame,
                            waveform_result_t *result)
{
    return waveform_analyze_cascade(frame, result, NULL);
}

/* 旧接口包装器：把固定默认参数装成一帧，再调用唯一的新版实现。
 * 这样旧代码无需立刻迁移，同时避免维护两套算法。 */
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

/* 枚举到可读字符串的稳定映射。越界输入归为 unknown，而不是索引越界。 */
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

/* 状态枚举到串口文本的稳定映射。数组顺序必须与头文件枚举顺序相同。 */
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

const char *waveform_algorithm_name(waveform_algorithm_t algorithm)
{
    switch (algorithm) {
    case WAVEFORM_ALGORITHM_LEGACY_AMDF_CROSSING:
        return "legacy_amdf_crossing";
    case WAVEFORM_ALGORITHM_GLOBAL_HARMONIC_MODEL:
        return "global_harmonic_model";
    case WAVEFORM_ALGORITHM_FAST_PHASE_MODEL:
        return "fast_phase_model";
    case WAVEFORM_ALGORITHM_CASCADE:
        return "cascade_rt3_spectral_yin";
    default:
        return "unknown_algorithm";
    }
}
