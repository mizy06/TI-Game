#ifndef WAVEFORM_ANALYZER_H_
#define WAVEFORM_ANALYZER_H_

/*
 * 本文件是“算法使用说明书”，而不是算法实现。
 *
 * 采集程序只要按 waveform_frame_t 填好 4096 个 ADC 点及其元数据，再调用
 * waveform_analyze_frame()，就能从 waveform_result_t 取得分类和测量结果。
 * 字段名故意包含物理单位，避免把 Hz/mHz、mV/uV、百分比/ppm 混在一起。
 */

#include <stdbool.h>
#include <stdint.h>

/* 如果头文件被 C++ 工程包含，extern "C" 可关闭 C++ 的函数名改编，保证
 * 链接器仍能找到由 C 编译器产生的 waveform_analyze_frame 等符号。 */
#ifdef __cplusplus
extern "C" {
#endif

/* 算法接口版本 2.1.0。高 16 位可理解为主版本；主机工具会检查这个值，
 * 防止“Python 按新版结构体读，但 DLL 仍是旧版”造成静默错位。 */
#define WAVEFORM_ALGORITHM_VERSION       (0x00020100UL)

/* 默认采集参数。WAVEFORM_SAMPLE_RATE_HZ=20000 表示每秒取 20000 点；
 * 4096 点覆盖 204.8 ms；12 位 ADC 的最大合法码为 2^12-1=4095。 */
#define WAVEFORM_SAMPLE_RATE_HZ          (20000U)
#define WAVEFORM_SAMPLE_COUNT            (4096U)
#define WAVEFORM_MAX_SAMPLE_COUNT        (4096U)
#define WAVEFORM_ADC_FULL_SCALE          (4095U)
#define WAVEFORM_ADC_VDDA_MV             (3300U)

/* 以 ppm 表示增益时，1000000 ppm 就是 1.000000 倍。整数单位可避免目标
 * 单片机依赖浮点运算。 */
#define WAVEFORM_UNITY_GAIN_PPM          (1000000UL)

/* 下面三个宏描述“默认采集侧事实”，允许真实硬件工程在编译参数中覆盖。
 * 采样率用 mHz（千分之一 Hz），参考电压用 uV（微伏），这样只用整数也
 * 能保留足够精度。例如 20000000 mHz = 20000 Hz。 */
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

/* 采集帧事实标志；可用按位或“|”同时设置多个标志。
 *
 * CONTIGUOUS：samples 中每一点在时间上连续，没有丢点或拼帧。
 * CLOCK_CALIBRATED：采样时钟经过校准；否则频率只具有标称时钟精度。
 * VREF_CALIBRATED：ADC 参考电压经过校准；否则 Vpp 继承参考电压误差。
 * HARDWARE_AVERAGED：ADC/外设已经做过硬件平均，供追踪采集链路使用。
 *
 * 算法可以降低随机噪声，却不可能凭数据自动消除采样时钟或参考电压的
 * 整体比例误差，所以校准状态必须由采集层如实说明。 */
enum {
    WAVEFORM_FRAME_FLAG_CONTIGUOUS          = (1UL << 0),
    WAVEFORM_FRAME_FLAG_CLOCK_CALIBRATED    = (1UL << 1),
    WAVEFORM_FRAME_FLAG_VREF_CALIBRATED     = (1UL << 2),
    WAVEFORM_FRAME_FLAG_HARDWARE_AVERAGED   = (1UL << 3)
};

#ifndef WAVEFORM_ACQUISITION_FLAGS
#define WAVEFORM_ACQUISITION_FLAGS (WAVEFORM_FRAME_FLAG_CONTIGUOUS)
#endif

/* 输出质量标志。它们是“结果仍可用，但请留意”的提示，不等同于 status
 * 里的硬失败。调用方可用 result.quality_flags & 某标志判断它是否存在。 */
enum {
    WAVEFORM_QUALITY_CLOCK_UNCALIBRATED = (1U << 0),
    WAVEFORM_QUALITY_VREF_UNCALIBRATED  = (1U << 1),
    WAVEFORM_QUALITY_FEW_CYCLES         = (1U << 2),
    WAVEFORM_QUALITY_LOW_MODEL_MARGIN   = (1U << 3)
};

/* 四种可识别波形及兜底 UNKNOWN。枚举从 0 连续排列，C 实现和 Python
 * ctypes 中的类型名称数组依赖这个顺序，修改时必须同步主机工具。 */
typedef enum {
    WAVEFORM_TYPE_SINE = 0,
    WAVEFORM_TYPE_SQUARE,
    WAVEFORM_TYPE_TRIANGLE,
    WAVEFORM_TYPE_SAWTOOTH,
    WAVEFORM_TYPE_UNKNOWN
} waveform_type_t;

/* 三条历史路径及正式三级级联入口。
 *
 * LEGACY_AMDF_CROSSING 保留原 2.0 的 AMDF + 中心穿越周期选择；
 * GLOBAL_HARMONIC_MODEL 在同一基线之上，对存在倍频歧义的帧额外比较
 * 1/2、1/3、1/4 周期候选的全局形状证据，避免相干干扰把基波拉到倍周期。
 * FAST_PHASE_MODEL 保留算法 2 的救援，减少 AMDF 与循环模板的重复运算。
 * 数值从 1 开始，便于串口命令 RT1/RT2/RT3/RT4 与枚举直接对应。 */
typedef enum {
    WAVEFORM_ALGORITHM_LEGACY_AMDF_CROSSING = 1,
    WAVEFORM_ALGORITHM_GLOBAL_HARMONIC_MODEL = 2,
    WAVEFORM_ALGORITHM_FAST_PHASE_MODEL = 3,
    WAVEFORM_ALGORITHM_CASCADE = 4
} waveform_algorithm_t;

/* 分析状态。只有 OK 才能把测得的频率、Vpp、占空比当作有效结果。
 *
 * TOO_SMALL：有效摆幅太小；CLIPPED：靠近 ADC 某一端的点过多；
 * PERIOD_NOT_FOUND：没有可靠周期；OUT_OF_RANGE：不在 100~400 Hz 附近；
 * SAMPLE_INVALID：出现大于 adc_full_scale 的码；AMBIGUOUS：模板分不清；
 * BAD_ARGUMENT/METADATA：指针或元数据错误；INSUFFICIENT：样本数不合格。 */
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

/* 采集层交给算法的“一帧数据”。
 *
 * samples 在调用期间仍归采集层所有，算法只读、不释放，也不会长期保存
 * 指针。ADC 码应是无符号、右对齐并按时间连续排列的原始整数。
 * frontend_gain_ppm = ADC 引脚幅度 / 信号源输入幅度；例如前端放大 2 倍
 * 填 2000000，衰减到一半填 500000。 */
typedef struct {
    /* 第一个 ADC 点的地址。数组至少包含 sample_count 个 uint16_t。 */
    const uint16_t *samples;
    /* 本帧实际点数；当前核心允许 1024~4096，默认固定为 4096。 */
    uint16_t sample_count;
    /* ADC 最大合法码；12 位右对齐 ADC 通常为 4095。 */
    uint16_t adc_full_scale;
    /* 实际采样率，单位 0.001 Hz；20 kHz 写作 20000000。 */
    uint32_t sample_rate_millihz;
    /* ADC 参考电压，单位 uV；3.3 V 写作 3300000。 */
    uint32_t adc_reference_microvolts;
    /* 模拟前端总增益，单位 ppm；1 倍写作 1000000。 */
    uint32_t frontend_gain_ppm;
    /* 用户自增的帧编号。算法原样回传，方便对应请求和结果。 */
    uint32_t sequence;
    /* 上述 WAVEFORM_FRAME_FLAG_* 的按位组合。 */
    uint32_t flags;
} waveform_frame_t;

/* 算法输出。为兼容旧接口，早期字段顺序保持不变；Python ctypes 结构体
 * 必须与这里在类型、顺序和对齐上完全相同。 */
typedef struct {
    /* 最佳波形类别；失败时保持 UNKNOWN。 */
    waveform_type_t type;
    /* 详细成功/失败原因；这是判断字段是否有效的第一依据。 */
    waveform_status_t status;
    /* 频率，单位 0.001 Hz；250000 表示 250.000 Hz。 */
    uint32_t frequency_millihz;
    /* 输入端峰峰值，单位 mV；保留给旧调用方。 */
    uint32_t vpp_millivolts;
    /* 方波占空比，单位千分比；300 表示 30.0%。非方波为 0。 */
    uint16_t duty_permille;
    /* 兼容字段，当前等于最佳模板分数。 */
    uint16_t confidence_permille;
    /* 最佳模板相关分数，0~1000；1000 表示非常吻合。 */
    uint16_t best_score_permille;
    /* 次佳模板相关分数；与最佳分数之差反映分类余量。 */
    uint16_t second_score_permille;
    /* 实际参与折叠平均的完整周期数。 */
    uint16_t accepted_cycles;
    /* 靠近 ADC 满量程的采样点数。 */
    uint16_t high_clip_samples;
    /* 由稳健高低分位中点估计的直流中心，单位 ADC 码。 */
    uint16_t dc_offset_counts;
    /* 靠近 ADC 零端的采样点数。 */
    uint16_t low_clip_samples;
    /* 超过 adc_full_scale 的非法采样点数。 */
    uint16_t invalid_samples;

    /* 以下为版本 2 的高精度与可追踪字段。 */
    /* 实际执行的算法版本，应等于 WAVEFORM_ALGORITHM_VERSION。 */
    uint32_t algorithm_version;
    /* 从输入帧原样复制的编号。 */
    uint32_t sequence;
    /* 输入端峰峰值，单位 uV；比旧 mV 字段保留更多小数。 */
    uint32_t vpp_microvolts;
    /* 方波占空比，单位 ppm；300000 表示 30%。非方波为 0。 */
    uint32_t duty_ppm;
    /* 每周期采样点数的 Q16 值；除以 65536 得到普通小数。 */
    uint32_t period_q16_samples;
    /* 拟合峰峰 ADC 码的 Q16 值；除以 65536 得到码值。 */
    uint32_t vpp_counts_q16;
    /* 上述 WAVEFORM_QUALITY_* 的按位组合。 */
    uint16_t quality_flags;
    /* 最佳分数减次佳分数，单位千分比；越大越容易区分。 */
    uint16_t model_margin_permille;
} waveform_result_t;

/* 推荐的版本 2 入口。同步调用期间不要改写 frame 或 samples；函数返回后
 * 即不再引用它们。成功返回 true，失败返回 false，并尽量在 result->status
 * 中给出原因。由于内部使用全局工作区，本函数不可重入。 */
bool waveform_analyze_frame(const waveform_frame_t *frame,
                            waveform_result_t *result);

/* 显式选择算法。1/2/3 保留历史对照；4 为生产级联。
 * waveform_analyze_frame() 默认运行级联。 */
bool waveform_analyze_frame_with_algorithm(const waveform_frame_t *frame,
                                           waveform_result_t *result,
                                           waveform_algorithm_t algorithm);

/* Independent backstops and the production cascade. L1 is the established
 * fast phase model, L2 is direct spectral harmonic regression, L3 is a
 * normalized difference period search with robust time-domain shape fitting.
 * Every call is synchronous and non-reentrant. */
typedef struct {
    waveform_status_t l1_status, l2_status, l3_status;
    waveform_type_t l1_type, l2_type, l3_type;
    uint8_t selected_level; /* 1 on agreement, 3 after arbitration */
    bool l3_ran;
} waveform_cascade_trace_t;
bool waveform_analyze_frequency(const waveform_frame_t *, waveform_result_t *);
bool waveform_analyze_robust(const waveform_frame_t *, waveform_result_t *);
bool waveform_analyze_cascade(const waveform_frame_t *, waveform_result_t *,
                              waveform_cascade_trace_t *);

/* 旧版兼容入口：自动套用本头文件上方的默认参数。新代码优先使用 frame
 * 入口，以免真实采样率、参考电压或增益与默认值不同。 */
bool waveform_analyze(const uint16_t *samples, waveform_result_t *result);

/* 把枚举转换成稳定、便于串口和日志读取的英文字符串。返回的是静态只读
 * 字符串，调用方不要修改或 free。 */
const char *waveform_type_name(waveform_type_t type);
const char *waveform_status_name(waveform_status_t status);
const char *waveform_algorithm_name(waveform_algorithm_t algorithm);

#ifdef __cplusplus
}
#endif

#endif /* WAVEFORM_ANALYZER_H_ */
