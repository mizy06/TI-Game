/*
 * TI Cup waveform classifier application for MSPM0G3507.
 *
 * This is an application-owned source file. SysConfig-generated files under
 * the active build directory remain untouched and may be regenerated safely.
 *
 * 零基础阅读提示：
 * 1. 定时器每 50 us 触发一次 ADC，因此采样率为 20 kHz；
 * 2. ADC 中断把 4096 个点依次放进 g_adc_samples；
 * 3. 主循环把这帧数据交给 waveform_analyze_frame；
 * 4. 结果经 UART 打印给电脑。
 *
 * R/W/T/D/RT 串口命令分别选择真实 ADC、电脑注入、片上模拟、样点导出和
 * 自激环路。算法本身不关心数据来自哪里，只要最终形成符合接口约定的
 * waveform_frame_t。
 *
 * 自激（RT）让板子不再依赖外部信号源：DAC 在 PA15 上产生已知波形，用
 * 一根杜邦线短接到 ADC 输入 PA25，同一帧数据再走完整的采集+识别链路，
 * 并把"标准答案"与"测量结果"逐项对比后一起上报。详见文件后半部分
 * "自激环路（RT）测试波形发生器"一节。
 */

#include "ti_msp_dl_config.h"
#include "waveform_analyzer.h"
#include "rt_question_set.h"

#include <stdbool.h>
#include <stdint.h>

/* ADC 中断和主循环都会访问前三个变量，因此必须用 volatile 告诉编译器：
 * 它们可能在当前代码看不到的地方（中断）发生变化，不能缓存或省略读写。
 * 8192 字节的样本数组放在静态区而不是函数栈，适合栈空间较小的单片机。
 * g_frame_sequence 只由主循环更新，不需要 volatile。 */
static volatile uint16_t g_adc_samples[WAVEFORM_SAMPLE_COUNT];/* 4096 个 12 位 ADC 样本。 */
static volatile uint32_t g_sample_index;/* 0~4096，表示下一点写入位置。 */
static volatile bool g_capture_done;/* 采集完成标志，主循环在 __WFI 等待时由中断置位。 */
static uint32_t g_frame_sequence;
/* 是否已经有一帧可导出的样点。上电后为 false，避免在还没采集时把未初始化
 * 的数组当成真实数据发给上位机。任何一条填充 g_adc_samples 的路径完成后
 * 都会置 true。 */
static bool g_has_samples;
/* 阻塞发送一个字符。FIFO 满时原地等待，直到 UART 硬件腾出空间。
 * 这种简单写法适合少量诊断文本；它不会丢字，但发送时 CPU 不能做别的事。 */
static void uart_put_char(char value)
{
    while (DL_UART_Main_isTXFIFOFull(UART_0_INST)) {
    }
    DL_UART_Main_transmitData(UART_0_INST, (uint8_t)value);
}
/* 发送以 '\0' 结尾的 C 字符串。text++ 先取当前字符，再把指针移到下一位。 */
static void uart_put_string(const char *text)
{
    while (*text != '\0') {
        uart_put_char(*text++);
    }
}
/* 不依赖 printf，把 32 位无符号整数写成十进制。
 * 除以 10 时余数是最低位，所以数字会先反向存入 digits，再倒序发送。
 * 32 位最大值 4294967295 正好 10 位，digits[10] 足够。do-while 保证数值
 * 为 0 时也会输出一个字符 '0'。*/
static void uart_put_u32(uint32_t value)
{
    char digits[10];
    uint32_t count = 0U;

    do {
        digits[count++] = (char)('0' + (value % 10U));
        value /= 10U;
    } while ((value != 0U) && (count < sizeof(digits)));

    while (count > 0U) {
        uart_put_char(digits[--count]);
    }
}
/* 把“千分之一单位”的整数打印为固定三位小数。例如 250000 mHz 输出
 * 250.000，2000 mV 输出 2.000；函数本身不关心物理单位。 */
static void uart_put_milli(uint32_t value)
{
    uart_put_u32(value / 1000U);
    uart_put_char('.');
    uart_put_char((char)('0' + ((value / 100U) % 10U)));
    uart_put_char((char)('0' + ((value / 10U) % 10U)));
    uart_put_char((char)('0' + (value % 10U)));
}
/* 把千分比打印为百分数的一位小数：999 permille -> 99.9%。 */
static void uart_put_percent_permille(uint16_t permille)
{
    uart_put_u32(permille / 10U);
    uart_put_char('.');
    uart_put_char((char)('0' + (permille % 10U)));
}

#include <math.h>

/* 一次分析的数据来源。枚举比裸数字更容易读，也能让编译器检查类型。 */
typedef enum {
    TRIGGER_ADC_CAPTURE = 0,
    TRIGGER_UART_INJECT,
    TRIGGER_ONCHIP_SELFTEST,
    /* 调试专用：把最近一帧的 4096 个样点原样上行，供上位机落盘与离线归因。
     * 它不参与识别流程，因此不会影响比赛路径的耗时与输出格式。 */
    TRIGGER_SAMPLE_DUMP,
    /* 自激环路：板载 DAC 自己产生已知波形，经杜邦线回到 ADC 输入，跑完
     * 整个模拟测量矩阵。它走的是真实模拟链路，只是激励来自板子自己。 */
    TRIGGER_DAC_LOOPBACK
} trigger_mode_t;
/* 片上自测使用的线性同余伪随机数状态。固定初值意味着每次上电产生同一
 * 噪声序列，便于复现实验。它不是密码学随机数，只用于测试。 */
static uint32_t g_sim_rng = 0x13579BDFU;
/* 线性同余发生器：新状态 = a*旧状态+c；uint32_t 溢出会按模 2^32 回绕。 */
static uint32_t sim_rand(void)
{
    g_sim_rng = (1664525U * g_sim_rng) + 1013904223U;
    return g_sim_rng;
}

/* 每触发一次 T 就切换到下一个波形，0~3 循环。 */
/*0~3循环？循环啥，等我看看*/
static uint8_t g_selftest_case = 0U;
/* 在单片机内存中生成一帧已知波形，用于在没有信号源和电脑注入工具时
 * 快速自检。这里使用浮点 sinf 只发生在自测数据生成阶段，不属于核心
 * waveform_analyzer.c 的定点算法。*/
static void generate_simulated_samples(void)
{
    /* 4 standard noisy test cases:
     * 0: Sine 200 Hz, Vpp ~2.0V, 15% Vpp uniform noise
     * 1: Square 250 Hz, 30% duty, Vpp ~2.0V, 15% Vpp uniform noise
     * 2: Triangle 150 Hz, Vpp ~2.0V, 15% Vpp uniform noise
     * 3: Sawtooth 300 Hz, Vpp ~2.0V, 15% Vpp uniform noise
     */
    uint32_t freq_hz = (g_selftest_case == 0U) ? 200U :
                       (g_selftest_case == 1U) ? 250U :
                       (g_selftest_case == 2U) ? 150U : 300U;
    uint32_t wave_type = g_selftest_case;
    g_selftest_case = (g_selftest_case + 1U) % 4U;

    /* 用 0~65535 表示一整周相位。每采一个点前进 freq/fs 周，因此步长为
     * freq_hz * 65536 / sample_rate。整数相位自然在 16 位处循环。 */
    uint32_t phase_inc = (freq_hz * 65536U) / WAVEFORM_SAMPLE_RATE_HZ;
    uint32_t phase = 0U;
    int32_t vpp_counts = 2482; /* 约 2.0 V：2.0 * 4095 / 3.3。 */
    int32_t center = 2048;     /* 约 1.65 V 的直流中心。 */
    /* 噪声范围为 [-noise_amp,+noise_amp]，总峰峰值约占信号 Vpp 的 15%。 */
    int32_t noise_amp = vpp_counts * 15 / 200;

    for (uint32_t i = 0U; i < WAVEFORM_SAMPLE_COUNT; ++i) {
        int32_t val = 0;
        uint16_t p16 = (uint16_t)phase;

        /* 四种公式都先生成以 0 为中心、约在 ±Vpp/2 之间的形状。 */
        switch (wave_type) {
        case 0: /* 正弦波。 */
            val = (int32_t)(sinf(2.0f * 3.14159265f * (float)phase / 65536.0f) * (float)(vpp_counts / 2));
            break;
        case 1: /* 30% 占空比方波：一周前 30% 为高，其余为低。 */
            val = (p16 < (65536U * 30U / 100U)) ? (vpp_counts / 2) : -(vpp_counts / 2);
            break;
        case 2: /* 三角波分三段线性表达，保证首尾周期连续。 */
            if (p16 < 16384U) {
                val = (int32_t)(((int64_t)p16 * vpp_counts) / 32768U);
            } else if (p16 < 49152U) {
                val = (vpp_counts / 2) - (int32_t)(((int64_t)(p16 - 16384U) * vpp_counts) / 32768U);
            } else {
                val = -(vpp_counts / 2) + (int32_t)(((int64_t)(p16 - 49152U) * vpp_counts) / 32768U);
            }
            break;
        case 3: /* 下降锯齿波：一周内从 +Vpp/2 线性降到 -Vpp/2。 */
        default:
            val = (vpp_counts / 2) - (int32_t)(((int64_t)p16 * vpp_counts) / 65536U);
            break;
        }

        /* 加伪随机均匀噪声，再叠加直流中心。极端值夹在 ADC 合法范围内，
         * 模拟真实 ADC 无法输出负数或大于 4095 的事实。 */
        int32_t noise = (int32_t)(sim_rand() % (uint32_t)(noise_amp * 2 + 1)) - noise_amp;
        int32_t sample = center + val + noise;
        if (sample < 0) {
            sample = 0;
        }
        if (sample > 4095) {
            sample = 4095;
        }
        g_adc_samples[i] = (uint16_t)sample;

        /* & 0xFFFF 等价于对 65536 取模，让相位走完一周后回到开头。 */
        phase = (phase + phase_inc) & 0xFFFFU;
    }
    g_has_samples = true;
}
/*所以以上都是在搞自测？感觉后续可删*/
/* 从 UART 接收一帧电脑注入的 ADC 数据。
 * 线上格式固定为 4096 个 little-endian uint16_t，共 8192 字节。MSPM0
 * 本身也是小端，因此可直接把样本数组看作字节数组依次填充。函数同时
 * 计算FNV-1a回执，让电脑确认固件实际收到的内容与发送内容完全一致。 */
static bool receive_uart_samples(void)
{
    volatile uint8_t *dest = (volatile uint8_t *)g_adc_samples;
    uint32_t total = WAVEFORM_SAMPLE_COUNT * sizeof(uint16_t);
    uint32_t count = 0U;
    uint32_t timeout_loops = 0U;
    uint32_t checksum = 2166136261U;

    /* 只有电脑收到握手后才应发送大块二进制，避免把固件启动文字误当数据。 */
    uart_put_string("READY_FOR_DATA\r\n");

    while (count < total) {
        if (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            uint8_t value = DL_UART_Main_receiveData(UART_0_INST);
            dest[count++] = value;
            /* FNV-1a：先异或当前字节，再乘固定素数；32 位溢出是算法的一部分。 */
            checksum = (checksum ^ value) * 16777619U;
            timeout_loops = 0U;
        } else {
            delay_cycles(CPUCLK_FREQ / 100000U); /* 大约等待 10 us。 */
            if (++timeout_loops > 250000U) {     /* 总计约 2.5 s 后超时。 */
                uart_put_string("ERR_TIMEOUT\r\n");
                return false;
            }
        }
    }
    uart_put_string("DATA,bytes=");
    uart_put_u32(total);
    uart_put_string(",fnv1a=");
    uart_put_u32(checksum);
    uart_put_string("\r\n");
    /* uint16_t 可到 65535，因此即使字节传输完整，样本也可能超过 12 位 ADC
     * 的 4095。尽早拒绝可让上位机得到明确的协议错误。 */
    for (count = 0U; count < WAVEFORM_SAMPLE_COUNT; ++count) {
        if (g_adc_samples[count] > WAVEFORM_ADC_FULL_SCALE) {
            uart_put_string("ERR_SAMPLE_RANGE\r\n");
            return false;
        }
    }
    g_has_samples = true;
    return true;
}
/*为什么是65535，没有说明*/

/* 按键使用上拉输入：未按下读到 1，按下接地读到 0，所以与 0 比较。 */
static bool button_is_pressed(void)
{
    return (DL_GPIO_readPins(GPIO_GRP_0_PORT, GPIO_GRP_0_PIN_0_PIN) &
            GPIO_GRP_0_PIN_0_PIN) == 0U;
}

/* 非阻塞地取一个串口字节；没有数据时返回 -1。
 * 与 uart_put_char 相对：这里绝不等待，因为调用方要在"等下一个字符"
 * 和"做别的事"之间自己安排时间。 */
static int uart_try_get_char(void)
{
    if (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
        return (int)DL_UART_Main_receiveData(UART_0_INST);
    }
    return -1;
}

/* 收到 'R' 之后，还要判断它是不是 "RT" 的第一个字符。
 *
 * 为什么需要这个等待窗口：串口是逐字节到达的，"RT" 会先送进 'R' 再送进
 * 'T'，固件在只看到 'R' 的时候无法知道后面还有没有 'T'。因此先按 R 处理
 * 会让 RT 永远收不到。这里在收到 'R' 后最多等 RT_PREFIX_WAIT_US 微秒：
 * - 期间收到 'T' -> 判定为 RT（自激环路）；
 * - 期间收到任何别的字节 -> 判定为单发 R（对外测量），并顺手丢弃该字节
 *   （与原有"忽略无关字符"的行为一致）；
 * - 一直没有新字节 -> 超时后判定为单发 R。
 *
 * 等待窗口取 120 ms：手打或脚本一次写出的 "RT" 间隔远小于它，而单发 R
 * 只多花 120 ms，相对比赛 10 s 的单帧限值完全可以忽略。 */
#define RT_PREFIX_WAIT_US (120000U)

static trigger_mode_t resolve_r_command(void)
{
    uint32_t waited_us = 0U;

    while (waited_us < RT_PREFIX_WAIT_US) {
        int next = uart_try_get_char();
        if (next >= 0) {
            if ((next == 't') || (next == 'T')) {
                return TRIGGER_DAC_LOOPBACK;
            }
            return TRIGGER_ADC_CAPTURE;
        }
        delay_cycles(CPUCLK_FREQ / 1000000U); /* 约 1 us。 */
        ++waited_us;
    }
    return TRIGGER_ADC_CAPTURE;
}

/* 一直等待用户选择下一帧数据来源。串口命令不区分大小写；板上按键等价
 * 于 R。按键再次读取前延时约 10 ms 是最简单的机械抖动过滤。 */
static trigger_mode_t wait_for_trigger(void)
{
    for (;;) {
        while (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            uint8_t value = DL_UART_Main_receiveData(UART_0_INST);
            if ((value == 'r') || (value == 'R')) {
                return resolve_r_command();
            }
            if ((value == 'w') || (value == 'W')) {
                return TRIGGER_UART_INJECT;
            }
            if ((value == 't') || (value == 'T')) {
                return TRIGGER_ONCHIP_SELFTEST;
            }
            if ((value == 'd') || (value == 'D')) {
                return TRIGGER_SAMPLE_DUMP;
            }
        }
        if (button_is_pressed()) {
            delay_cycles(CPUCLK_FREQ / 100U);
            if (button_is_pressed()) {
                return TRIGGER_ADC_CAPTURE;
            }
        }
        delay_cycles(CPUCLK_FREQ / 1000U);
    }
}

/* 等到按键松开并再延时约 10 ms，防止一次长按触发多帧。 */
static void wait_for_button_release(void)
{
    while (button_is_pressed()) {
        delay_cycles(CPUCLK_FREQ / 1000U);
    }
    delay_cycles(CPUCLK_FREQ / 100U);
}

/* 启动一次真实 ADC 采集并等待 4096 点完成。
 *
 * 关键顺序是：关中断 -> 清软件状态和硬件挂起状态 -> 开 ADC 中断并启动
 * 定时器 -> 开中断。这样旧的挂起中断不会被误算作新帧的第一个点。
 * __WFI() 让 CPU 在等待期间休眠，任何中断都会唤醒它，比空转省电。 */
static void capture_samples(void)
{
    __disable_irq();
    g_sample_index = 0U;
    g_capture_done = false;
    DL_TimerA_stopCounter(TIMER_0_INST);
    DL_ADC12_clearInterruptStatus(ADC12_0_INST,
                                 DL_ADC12_INTERRUPT_MEM0_RESULT_LOADED);
    NVIC_ClearPendingIRQ(ADC12_0_INST_INT_IRQN);
    NVIC_EnableIRQ(ADC12_0_INST_INT_IRQN);
    DL_TimerA_startCounter(TIMER_0_INST);
    __enable_irq();

    while (!g_capture_done) {
        __WFI();
    }
    NVIC_DisableIRQ(ADC12_0_INST_INT_IRQN);
    g_has_samples = true;
}

/* 把最近一帧的 4096 个样点原样上行，供上位机落盘和离线归因。
 *
 * 存在的理由：R 触发只上行一行 RESULT，4096 个模拟采样点留在 g_adc_samples
 * 里，上位机看不到波形本身，也就无法回答"这一帧为什么被判拒识"。失败分支
 * 又只报告状态码，于是拒识原因在链路上完全不可见。
 *
 * 帧格式刻意与 W 注入完全一致（8192 字节小端 uint16，外加上行前先给一行
 * 带字节数与 FNV-1a 的回执），这样上位机可以复用同一套校验与离线回放代码，
 * 并且把收到的字节直接当成一帧喂给算法库。 */
static void dump_samples(void)
{
    uint32_t checksum = 2166136261U;
    uint32_t index;

    if (!g_has_samples) {
        uart_put_string("ERR_NO_SAMPLES\r\n");
        return;
    }

    /* 先算校验再发送：校验的字节顺序必须与下面的发送顺序完全一致，
     * 否则上位机复算 FNV-1a 会对不上。 */
    for (index = 0U; index < WAVEFORM_SAMPLE_COUNT; ++index) {
        uint16_t value = g_adc_samples[index];
        checksum = (checksum ^ (uint32_t)(value & 0xFFU)) * 16777619U;
        checksum = (checksum ^ (uint32_t)((value >> 8) & 0xFFU)) * 16777619U;
    }
    uart_put_string("SAMPLES,bytes=");
    uart_put_u32(WAVEFORM_SAMPLE_COUNT * 2U);
    uart_put_string(",fnv1a=");
    uart_put_u32(checksum);
    uart_put_string("\r\n");

    /* MSPM0 是小端器件，按低字节在前发送即可让上位机用 <4096H 直接解包。 */
    for (index = 0U; index < WAVEFORM_SAMPLE_COUNT; ++index) {
        uint16_t value = g_adc_samples[index];
        uart_put_char((char)(value & 0xFFU));
        uart_put_char((char)((value >> 8) & 0xFFU));
    }
}

/* 把结构化结果编码为一行稳定的键值文本。
 *
 * 失败结果只报告诊断字段，避免误用无效测量值；成功结果同时保留便于人
 * 阅读的小数单位和便于程序精确处理的整数单位。\r\n 是常见串口换行。 */
static void print_result(const waveform_result_t *result, bool valid)
{
    if (!valid) {
        uart_put_string("RESULT,status=");
        uart_put_string(waveform_status_name(result->status));
        uart_put_string(",type=unknown,high_clip_samples=");
        uart_put_u32(result->high_clip_samples);
        uart_put_string(",low_clip_samples=");
        uart_put_u32(result->low_clip_samples);
        uart_put_string(",invalid_samples=");
        uart_put_u32(result->invalid_samples);
        uart_put_string(",sequence=");
        uart_put_u32(result->sequence);
        uart_put_string(",quality_flags=");
        uart_put_u32(result->quality_flags);
        uart_put_string("\r\n");
        return;
    }

    uart_put_string("RESULT,status=OK,type=");
    uart_put_string(waveform_type_name(result->type));
    uart_put_string(",freq_hz=");
    uart_put_milli(result->frequency_millihz);
    uart_put_string(",vpp_v=");
    uart_put_milli(result->vpp_millivolts);
    /* 只有方波定义占空比；其他波形明确发送 NA，而不是让 0 看似测量值。 */
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_string(",duty_pct=");
        uart_put_percent_permille(result->duty_permille);
    } else {
        uart_put_string(",duty_pct=NA");
    }
    uart_put_string(",score_pct=");
    uart_put_percent_permille(result->best_score_permille);
    uart_put_string(",margin_pct=");
    /* 理论上最佳分数不会小于次佳分数；防御性判断可避免无符号减法下溢。 */
    if (result->best_score_permille >= result->second_score_permille) {
        uart_put_percent_permille((uint16_t)(result->best_score_permille -
                                             result->second_score_permille));
    } else {
        uart_put_string("0.0");
    }
    uart_put_string(",cycles=");
    uart_put_u32(result->accepted_cycles);
    uart_put_string(",freq_millihz=");
    uart_put_u32(result->frequency_millihz);
    uart_put_string(",vpp_microvolts=");
    uart_put_u32(result->vpp_microvolts);
    uart_put_string(",duty_ppm=");
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_u32(result->duty_ppm);
    } else {
        uart_put_string("NA");
    }
    uart_put_string(",sequence=");
    uart_put_u32(result->sequence);
    uart_put_string(",algorithm_version=");
    uart_put_u32(result->algorithm_version);
    uart_put_string(",quality_flags=");
    uart_put_u32(result->quality_flags);
    uart_put_string("\r\n");
}

/* ================= 自激环路（RT）测试波形发生器 =================
 *
 * 目的：在没有信号发生器、甚至没有电脑的情况下，让板子自己产生"标准
 * 答案已知"的波形，从 DAC 脚 PA15 输出，用一根杜邦线短接到 ADC 输入
 * PA25，再走完整的采集 + 识别链路，最后把标称值和测量值逐项对比。
 * 这样一条命令就能验证"模拟输出 -> 引脚 -> 连线 -> ADC -> 算法 -> 串口"
 * 整条通路，而不是只验证算法本身。
 *
 * 为什么对得很准（三条设计约束）：
 * 1. 产生和采集共用 TIMER_0 的同一个 20 kHz 节拍。每个节拍上硬件先触发
 *    ADC 采样保持，之后中断里才把 DAC 更新到下一点，因此第 k 个 ADC 点
 *    采到的正是第 k 个 DAC 码，帧内不需要任何对齐或插值。
 * 2. 用例频率取 f = cycles * 20000 / 4096（cycles 为整数），保证一帧 4096
 *    点里正好放下整数个周期，首尾严格连续，换帧不会产生毛刺。
 * 3. DAC 和 ADC 都以 VDDA 为参考，参考电压的绝对误差在码域两端互相
 *    抵消，所以 Vpp 的对比可以卡得很紧。
 *
 * 安全约束：非自激状态下 DAC 输出缓冲被置为高阻。这样即使杜邦线一直
 * 挂在 PA15 和 PA25 之间，板载发生器也不会去拽动外部信号源，对外测量
 * 的 R 路径完全不受影响。
 */

/* 题集定义在 rt_question_set.h：前 200 题是自编门禁，后 96 题是
 * challenge。参数由原 competition_stress_suite.py 固定 seed 题库映射而来；
 * 硬件采样率固定为 20 kHz，所以频率量化到一帧内的整数周期。 */

/* 256 点整周期正弦表，幅值 ±2047（约等于 12 位满量程的一半）。
 * 放在 flash 里（const），不占 SRAM。相位取高 8 位直接查表，相位量化
 * 误差小于 0.8 度，对幅度的影响小于 0.01%，可以忽略。 */
static const int16_t g_rt_sine_table[256] = {
        0,    50,   100,   151,   201,   251,   300,   350,
      399,   449,   497,   546,   594,   642,   690,   737,
      783,   830,   875,   920,   965,  1009,  1052,  1095,
     1137,  1179,  1219,  1259,  1299,  1337,  1375,  1411,
     1447,  1483,  1517,  1550,  1582,  1614,  1644,  1674,
     1702,  1729,  1756,  1781,  1805,  1828,  1850,  1871,
     1891,  1910,  1927,  1944,  1959,  1973,  1986,  1997,
     2008,  2017,  2025,  2032,  2037,  2041,  2045,  2046,
     2047,  2046,  2045,  2041,  2037,  2032,  2025,  2017,
     2008,  1997,  1986,  1973,  1959,  1944,  1927,  1910,
     1891,  1871,  1850,  1828,  1805,  1781,  1756,  1729,
     1702,  1674,  1644,  1614,  1582,  1550,  1517,  1483,
     1447,  1411,  1375,  1337,  1299,  1259,  1219,  1179,
     1137,  1095,  1052,  1009,   965,   920,   875,   830,
      783,   737,   690,   642,   594,   546,   497,   449,
      399,   350,   300,   251,   201,   151,   100,    50,
        0,   -50,  -100,  -151,  -201,  -251,  -300,  -350,
     -399,  -449,  -497,  -546,  -594,  -642,  -690,  -737,
     -783,  -830,  -875,  -920,  -965, -1009, -1052, -1095,
    -1137, -1179, -1219, -1259, -1299, -1337, -1375, -1411,
    -1447, -1483, -1517, -1550, -1582, -1614, -1644, -1674,
    -1702, -1729, -1756, -1781, -1805, -1828, -1850, -1871,
    -1891, -1910, -1927, -1944, -1959, -1973, -1986, -1997,
    -2008, -2017, -2025, -2032, -2037, -2041, -2045, -2046,
    -2047, -2046, -2045, -2041, -2037, -2032, -2025, -2017,
    -2008, -1997, -1986, -1973, -1959, -1944, -1927, -1910,
    -1891, -1871, -1850, -1828, -1805, -1781, -1756, -1729,
    -1702, -1674, -1644, -1614, -1582, -1550, -1517, -1483,
    -1447, -1411, -1375, -1337, -1299, -1259, -1219, -1179,
    -1137, -1095, -1052, -1009,  -965,  -920,  -875,  -830,
     -783,  -737,  -690,  -642,  -594,  -546,  -497,  -449,
     -399,  -350,  -300,  -251,  -201,  -151,  -100,   -50,
};

/* 回放期间会被 TIMA0 中断读写的量，必须 volatile：编译器不能假设它们在
 * 当前代码看不到的地方（中断）保持不变。其余配置量只在开始播放之前由
 * 主循环写一次，中断只读，因此不需要 volatile。 */
static volatile bool     g_dac_playing;   /* 中断据此决定是否驱动 DAC。 */
static volatile uint16_t g_dac_phase;     /* 中断内推进的 16 位相位。 */
static uint16_t g_rt_phase_inc;           /* 每个采样点的相位增量。 */
static uint16_t g_rt_duty_thresh;         /* 方波高电平的相位门限。 */
static uint16_t g_rt_rise_width;          /* 方波上升沿宽度，16 位相位单位。 */
static uint16_t g_rt_fall_width;          /* 方波下降沿宽度，16 位相位单位。 */
static uint16_t g_rt_center;              /* DAC 直流中心码。 */
static uint8_t  g_rt_type;                /* 当前用例的波形类别。 */
static uint8_t  g_rt_impairment;          /* challenge 失真/干扰类型。 */
static uint8_t  g_rt_noise_permille;      /* 噪声 Vpp / 信号 Vpp，千分数。 */
static uint8_t  g_rt_noise_mode;          /* 白噪声、低通噪声或高频噪声。 */
static int32_t  g_rt_half;                /* 半峰峰值，单位 ADC 码。 */
static int32_t  g_rt_noise_state;         /* 一阶噪声滤波器状态。 */
static uint32_t g_rt_rng;                 /* 固定种子伪随机噪声。 */
static uint16_t g_rt_am_phase;            /* 13 Hz 慢调幅相位。 */

/* 用例的标称频率，单位 mHz：f = cycles * fs / N。
 * 用整数运算保留到 mHz，避免为了打印一个频率引入浮点。 */
static uint32_t rt_case_frequency_millihz(const rt_case_t *item)
{
    return ((uint32_t)item->cycles * WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ) /
           WAVEFORM_SAMPLE_COUNT;
}

/* 由 16 位相位算出当前点应写进 DAC 的 12 位码值。
 * 先算以 0 为中心、幅值约 ±2047 的"单位波形"，再按用例幅度缩放并叠加
 * 直流中心。全程只有整数乘法和移位，在 20 kHz 中断里也足够快。 */
static int32_t rt_square_unit(uint16_t phase)
{
    int32_t rise_distance = (int32_t)(int16_t)phase;
    int32_t fall_distance = (int32_t)(int16_t)(phase - g_rt_duty_thresh);
    int32_t rise_half = (int32_t)g_rt_rise_width / 2;
    int32_t fall_half = (int32_t)g_rt_fall_width / 2;

    if ((g_rt_rise_width > 0U) &&
        (rise_distance >= -rise_half) && (rise_distance <= rise_half)) {
        return (rise_distance * 4094) / (int32_t)g_rt_rise_width;
    }
    if ((g_rt_fall_width > 0U) &&
        (fall_distance >= -fall_half) && (fall_distance <= fall_half)) {
        return -(fall_distance * 4094) / (int32_t)g_rt_fall_width;
    }
    if ((phase > (uint16_t)rise_half) &&
        (phase < (uint16_t)(g_rt_duty_thresh - (uint16_t)fall_half))) {
        return 2047;
    }
    return -2047;
}

static int32_t rt_base_unit(uint8_t type, uint16_t phase)
{
    int32_t unit;

    switch (type) {
    case WAVEFORM_TYPE_SINE:
        unit = (int32_t)g_rt_sine_table[phase >> 8];
        break;
    case WAVEFORM_TYPE_SQUARE:
        unit = rt_square_unit(phase);
        break;
    case WAVEFORM_TYPE_TRIANGLE:
        /* 两段线性：前半周从 -2047 升到 +2047，后半周再降回来。
         * 段内用 >>15 代替除以 32768，边界处只差 1 个码值。 */
        if (phase < 32768U) {
            unit = -2047 + (((int32_t)phase * 4094) >> 15);
        } else {
            unit = 2047 - (((int32_t)(phase - 32768U) * 4094) >> 15);
        }
        break;
    case WAVEFORM_TYPE_SAWTOOTH:
    default:
        /* 下降锯齿：一周内从 +2047 线性降到 -2047。 */
        unit = 2047 - (((int32_t)phase * 4094) >> 16);
        break;
    }
    return unit;
}

static int32_t rt_noise_unit(void)
{
    int32_t white;
    int32_t selected;

    g_rt_rng = (1664525U * g_rt_rng) + 1013904223U;
    white = (int32_t)((g_rt_rng >> 20) & 0x0FFFU) - 2048;
    g_rt_noise_state += (white - g_rt_noise_state) >> 2;
    if (g_rt_noise_mode == RT_NOISE_LOWPASS) {
        selected = g_rt_noise_state;
    } else if (g_rt_noise_mode == RT_NOISE_HIGHBAND) {
        selected = (white - g_rt_noise_state) / 2;
    } else {
        selected = white;
    }
    return (selected * (int32_t)g_rt_noise_permille) / 1000;
}

/* 由 16 位相位算出当前点应写进 DAC 的 12 位码值。除基础四类波形外，
 * challenge 还在这里加入削顶、交越、谐波、慢调幅和相干干扰。全程使用
 * 整数运算，保证 20 kHz 定时器中断内可以完成。 */
static uint16_t rt_wave_code(uint16_t phase)
{
    int32_t unit = rt_base_unit(g_rt_type, phase);
    int32_t limit;

    switch (g_rt_impairment) {
    case RT_IMPAIRMENT_TOP_CLIP:
        limit = (2047 * 92) / 100;
        if (unit > limit) unit = limit;
        break;
    case RT_IMPAIRMENT_BOTTOM_CLIP:
        limit = (2047 * 92) / 100;
        if (unit < -limit) unit = -limit;
        break;
    case RT_IMPAIRMENT_TWO_SIDED_CLIP:
        limit = (2047 * 94) / 100;
        if (unit > limit) unit = limit;
        if (unit < -limit) unit = -limit;
        break;
    case RT_IMPAIRMENT_CROSSOVER:
        limit = (2047 * 5) / 100;
        if ((unit >= -limit) && (unit <= limit)) {
            unit = 0;
        } else if (unit > 0) {
            unit = ((unit - limit) * 100) / 95;
        } else {
            unit = ((unit + limit) * 100) / 95;
        }
        break;
    case RT_IMPAIRMENT_HARMONIC_2_3:
        unit += ((int32_t)g_rt_sine_table[((uint16_t)(phase * 2U)) >> 8] * 4) / 100;
        unit += ((int32_t)g_rt_sine_table[((uint16_t)(phase * 3U)) >> 8] * 6) / 100;
        break;
    case RT_IMPAIRMENT_SLOW_AM:
        unit = (unit * (1000 +
                ((int32_t)g_rt_sine_table[g_rt_am_phase >> 8] * 60) / 2047)) / 1000;
        g_rt_am_phase = (uint16_t)(g_rt_am_phase + 43U); /* 约 13.1 Hz。 */
        break;
    case RT_IMPAIRMENT_COHERENT_INTERFERER:
        unit += ((int32_t)g_rt_sine_table[
                    ((uint16_t)(((uint32_t)phase * 173U) / 100U)) >> 8] * 8) / 100;
        break;
    case RT_IMPAIRMENT_FIFTH_HARMONIC:
        unit += ((int32_t)g_rt_sine_table[((uint16_t)(phase * 5U)) >> 8] * 8) / 100;
        break;
    default:
        break;
    }

    unit += rt_noise_unit();

    /* >>11 相当于除以 2048，于是 unit 的满幅 2047 正好对应半个峰峰值。
     * 极端值夹到 ADC/DAC 合法的 0~4095，模拟真实器件的边界行为。 */
    {
        int32_t code = (int32_t)g_rt_center + ((unit * g_rt_half) >> 11);
        if (code < 0) {
            code = 0;
        } else if (code > (int32_t)WAVEFORM_ADC_FULL_SCALE) {
            code = (int32_t)WAVEFORM_ADC_FULL_SCALE;
        }
        return (uint16_t)code;
    }
}

/* 装载第 index 个用例的所有回放参数。必须在开始播放之前调用。 */
static void rt_load_case(const rt_case_t *item, uint16_t index)
{
    uint32_t vpp_counts;
    uint32_t occupied_half;

    g_rt_type = item->type;
    /* 一帧 4096 点正好对应 65536 个相位单位，所以每点相位增量是
     * cycles * (65536 / 4096) = cycles * 16，整数运算无截断误差。 */
    g_rt_phase_inc = (uint16_t)(item->cycles * (65536U / WAVEFORM_SAMPLE_COUNT));
    /* 占空比千分数换算成相位门限：permille/1000 * 65536。 */
    g_rt_duty_thresh =
        (uint16_t)(((uint32_t)item->duty_permille * 65536U) / 1000U);
    g_rt_rise_width =
        (uint16_t)(((uint32_t)item->rise_permille * 65536U) / 1000U);
    g_rt_fall_width =
        (uint16_t)(((uint32_t)item->fall_permille * 65536U) / 1000U);
    /* 峰峰值按 ADC/DAC 共同的 12 位满量程换算成码值。 */
    vpp_counts = ((uint32_t)item->vpp_millivolts * WAVEFORM_ADC_FULL_SCALE) /
                 WAVEFORM_ADC_VDDA_MV;
    g_rt_half = (int32_t)(vpp_counts / 2U);
    occupied_half = ((uint32_t)g_rt_half *
                    (1000U + (uint32_t)item->noise_permille)) / 1000U + 16U;
    if (item->center_mode == 0U) {
        g_rt_center = (uint16_t)occupied_half;
    } else if (item->center_mode == 2U) {
        g_rt_center = (uint16_t)(WAVEFORM_ADC_FULL_SCALE - occupied_half);
    } else if (item->center_mode == 3U) {
        uint32_t span = WAVEFORM_ADC_FULL_SCALE - (2U * occupied_half);
        g_rt_center = (uint16_t)(occupied_half +
            (((uint32_t)index * 2654435761UL) % (span + 1U)));
    } else {
        g_rt_center = 2048U;
    }
    g_rt_impairment = item->impairment;
    g_rt_noise_permille = item->noise_permille;
    g_rt_noise_mode = item->noise_mode;
    g_rt_noise_state = 0;
    g_rt_rng = 0x6D2B79F5U ^ ((uint32_t)index * 7919U);
    g_rt_am_phase = (uint16_t)(index * 977U);
    g_dac_phase = (uint16_t)(index * 40503U);
}

/* 播放当前用例并同步采回一帧。返回后 g_adc_samples 里就是这一帧。 */
static void rt_capture_case(void)
{
    /* 先把第 0 点写进 DAC。这样第一个节拍上 ADC 采到的就是第 0 点，而
     * 中断随后写入第 1 点，依此类推，第 k 个采样点恒等于第 k 个 DAC 码。 */
    uint16_t first_phase = g_dac_phase;
    DL_DAC12_output12(DAC0, rt_wave_code(first_phase));
    g_dac_phase = (uint16_t)(first_phase + g_rt_phase_inc);
    g_dac_playing = true;

    /* TIMA0 的 ZERO 中断平时不挂到 NVIC 上，只有自激播放期间才打开：
     * 对外测量的 R 路径因此不会多背一个 20 kHz 中断的固定开销。 */
    NVIC_ClearPendingIRQ(TIMER_0_INST_INT_IRQN);
    NVIC_EnableIRQ(TIMER_0_INST_INT_IRQN);
    capture_samples(); /* 内部自己关/开总中断、启动 TIMA0、采满 4096 点后停表。 */
    NVIC_DisableIRQ(TIMER_0_INST_INT_IRQN);
    g_dac_playing = false;
}

static uint32_t rt_capture_adc_mean(void)
{
    uint32_t sum = 0U;
    uint32_t index;

    capture_samples();
    for (index = 0U; index < WAVEFORM_SAMPLE_COUNT; ++index) {
        sum += g_adc_samples[index];
    }
    return sum / WAVEFORM_SAMPLE_COUNT;
}

static uint32_t rt_capture_dac_level_mean(uint16_t dac_code)
{
    DL_DAC12_output12(DAC0, dac_code);
    delay_cycles(CPUCLK_FREQ / 1000U);
    return rt_capture_adc_mean();
}

static void rt_report_gpio_preflight(void)
{
    const uint32_t pin_config = IOMUX->SECCFG.PINCM[GPIO_DAC12_IOMUX_OUT];
    const uint32_t adc_pin_config = IOMUX->SECCFG.PINCM[GPIO_ADC12_0_IOMUX_C2];
    uint32_t low_adc;
    uint32_t high_adc;
    uint32_t low_logic;
    uint32_t high_logic;

    DL_DAC12_setAmplifier(DAC0, DL_DAC12_AMP_OFF_TRISTATE);
    DL_DAC12_disableOutputPin(DAC0);
    DL_GPIO_initDigitalOutput(GPIO_DAC12_IOMUX_OUT);
    DL_GPIO_initDigitalInputFeatures(GPIO_ADC12_0_IOMUX_C2,
        DL_GPIO_INVERSION_DISABLE, DL_GPIO_RESISTOR_NONE,
        DL_GPIO_HYSTERESIS_DISABLE, DL_GPIO_WAKEUP_DISABLE);
    DL_GPIO_clearPins(GPIO_DAC12_OUT_PORT, GPIO_DAC12_OUT_PIN);
    DL_GPIO_enableOutput(GPIO_DAC12_OUT_PORT, GPIO_DAC12_OUT_PIN);
    delay_cycles(CPUCLK_FREQ / 1000U);
    low_adc = rt_capture_adc_mean();
    low_logic = (DL_GPIO_readPins(GPIO_ADC12_0_C2_PORT,
        GPIO_ADC12_0_C2_PIN) != 0U) ? 1U : 0U;

    DL_GPIO_setPins(GPIO_DAC12_OUT_PORT, GPIO_DAC12_OUT_PIN);
    delay_cycles(CPUCLK_FREQ / 1000U);
    high_adc = rt_capture_adc_mean();
    high_logic = (DL_GPIO_readPins(GPIO_ADC12_0_C2_PORT,
        GPIO_ADC12_0_C2_PIN) != 0U) ? 1U : 0U;

    DL_GPIO_clearPins(GPIO_DAC12_OUT_PORT, GPIO_DAC12_OUT_PIN);
    DL_GPIO_disableOutput(GPIO_DAC12_OUT_PORT, GPIO_DAC12_OUT_PIN);
    IOMUX->SECCFG.PINCM[GPIO_DAC12_IOMUX_OUT] = pin_config;
    IOMUX->SECCFG.PINCM[GPIO_ADC12_0_IOMUX_C2] = adc_pin_config;
    DL_DAC12_enableOutputPin(DAC0);
    DL_DAC12_setAmplifier(DAC0, DL_DAC12_AMP_ON);
    delay_cycles(CPUCLK_FREQ / 1000U);

    uart_put_string("RT-GPIO-PREFLIGHT,low_adc=");
    uart_put_u32(low_adc);
    uart_put_string(",high_adc=");
    uart_put_u32(high_adc);
    uart_put_string(",delta=");
    uart_put_u32((high_adc >= low_adc) ? (high_adc - low_adc) : 0U);
    uart_put_string(",low_gpio_in=");
    uart_put_u32(low_logic);
    uart_put_string(",high_gpio_in=");
    uart_put_u32(high_logic);
    uart_put_string("\r\n");
}

static void rt_report_dac_preflight(void)
{
    const uint32_t low_adc = rt_capture_dac_level_mean(1024U);
    const uint32_t high_adc = rt_capture_dac_level_mean(3072U);

    uart_put_string("RT-DAC-PREFLIGHT,low_code=1024,low_adc=");
    uart_put_u32(low_adc);
    uart_put_string(",high_code=3072,high_adc=");
    uart_put_u32(high_adc);
    uart_put_string(",delta=");
    uart_put_u32((high_adc >= low_adc) ? (high_adc - low_adc) : 0U);
    uart_put_string(",dac_enabled=");
    uart_put_u32(DL_DAC12_isEnabled(DAC0) ? 1U : 0U);
    uart_put_string(",pin_enabled=");
    uart_put_u32(DL_DAC12_isOutputPinEnabled(DAC0) ? 1U : 0U);
    uart_put_string(",amplifier=");
    uart_put_u32((uint32_t)DL_DAC12_getAmplifier(DAC0));
    uart_put_string("\r\n");
}

/* 自激矩阵运行期间，任何串口输入或按键都表示"停下来"。
 * 连续读两次按键是为了滤掉机械抖动，避免误判中止。 */
static bool rt_abort_requested(void)
{
    if (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
        (void)DL_UART_Main_receiveData(UART_0_INST);
        return true;
    }
    if (button_is_pressed()) {
        delay_cycles(CPUCLK_FREQ / 1000U);
        return button_is_pressed();
    }
    return false;
}

/* 进入自激前先清空接收 FIFO。
 *
 * 串口助手习惯把命令连同回车一起发出，于是 "RT\r\n" 里的 '\r' '\n' 会在
 * 'T' 之后继续到达。而 rt_abort_requested() 把"收到任何字节"当成中止请求，
 * 那个回车就会让矩阵还没开始就退出。这里先花约 10 ms 把 FIFO 读空，
 * 之后到达的字节才算真正的用户输入。 */
static void rt_drain_rx(void)
{
    uint32_t settle;

    for (settle = 0U; settle < 10000U; ++settle) {
        (void)uart_try_get_char();
        delay_cycles(CPUCLK_FREQ / 1000000U); /* 约 1 us。 */
    }
}

/* 自编题沿用软件题集门限：频率 0.5%、Vpp 8%、占空比 2 个百分点；
 * challenge 为 1%、12%、3 个百分点。 */

/* 把"标准答案"和算法测出来的值逐项比较，输出一行 RT-CHECK。
 * 返回 true 表示该用例通过。失败时仍然打印各项误差，便于现场定位是
 * 分类错、频率错还是幅度错。 */
static bool rt_check_case(const rt_case_t *item,
                          const waveform_result_t *result,
                          bool valid)
{
    uint32_t nominal_millihz = rt_case_frequency_millihz(item);
    uint32_t nominal_uv = (uint32_t)item->vpp_millivolts * 1000U;
    uint32_t nominal_duty_ppm = (uint32_t)item->duty_permille * 1000U;
    bool type_ok = valid && (result->type == (waveform_type_t)item->type);
    bool freq_ok = false;
    bool vpp_ok = false;
    bool duty_ok = true;
    uint32_t freq_err = 0U;
    uint32_t vpp_err = 0U;
    uint32_t duty_err = 0U;
    uint32_t freq_tolerance = (item->group == RT_GROUP_CHALLENGE) ? 10U : 5U;
    uint32_t vpp_tolerance = (item->group == RT_GROUP_CHALLENGE) ? 120U : 80U;
    uint32_t duty_tolerance =
        (item->group == RT_GROUP_CHALLENGE) ? 30000U : 20000U;

    /* 类型判错时后面的数值没有意义，直接保留 0 误差并置 FAIL。 */
    if (type_ok) {
        freq_err = (result->frequency_millihz > nominal_millihz)
                       ? (result->frequency_millihz - nominal_millihz)
                       : (nominal_millihz - result->frequency_millihz);
        freq_ok = (freq_err * 1000U) <= (nominal_millihz * freq_tolerance);

        vpp_err = (result->vpp_microvolts > nominal_uv)
                      ? (result->vpp_microvolts - nominal_uv)
                      : (nominal_uv - result->vpp_microvolts);
        vpp_ok = (vpp_err * 1000U) <= (nominal_uv * vpp_tolerance);

        if (item->type == WAVEFORM_TYPE_SQUARE) {
            duty_err = (result->duty_ppm > nominal_duty_ppm)
                           ? (result->duty_ppm - nominal_duty_ppm)
                           : (nominal_duty_ppm - result->duty_ppm);
            duty_ok = duty_err <= duty_tolerance;
        }
    }

    uart_put_string("RT-CHECK,verdict=");
    uart_put_string((type_ok && freq_ok && vpp_ok && duty_ok) ? "PASS" : "FAIL");
    uart_put_string(",type_ok=");
    uart_put_u32(type_ok ? 1U : 0U);
    uart_put_string(",freq_ok=");
    uart_put_u32(freq_ok ? 1U : 0U);
    uart_put_string(",vpp_ok=");
    uart_put_u32(vpp_ok ? 1U : 0U);
    uart_put_string(",duty_ok=");
    uart_put_u32(duty_ok ? 1U : 0U);
    uart_put_string(",freq_err_millihz=");
    uart_put_u32(freq_err);
    uart_put_string(",vpp_err_microvolts=");
    uart_put_u32(vpp_err);
    uart_put_string(",duty_err_ppm=");
    if (item->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_u32(duty_err);
    } else {
        uart_put_string("NA");
    }
    uart_put_string("\r\n");

    return type_ok && freq_ok && vpp_ok && duty_ok;
}

/* 跑完一个用例：报标称值 -> 播放并采样 -> 分析 -> 报结果 -> 报对比 ->
 * 回传原始样点。返回 true 表示这个用例通过。 */
static const char *rt_impairment_name(uint8_t impairment)
{
    static const char *const names[] = {
        "boundary", "top_clip", "bottom_clip", "two_sided_clip",
        "crossover", "harmonic_2_3", "slow_am", "coherent_interferer",
        "fifth_harmonic"
    };
    return (impairment < (sizeof(names) / sizeof(names[0]))) ?
           names[impairment] : "unknown";
}

static bool rt_measure_case(uint16_t index)
{
    const rt_case_t *item = &g_rt_cases[index];
    waveform_frame_t frame;
    waveform_result_t result;
    bool valid;
    bool passed;

    rt_load_case(item, index);

    uart_put_string("RT-CASE,index=");
    uart_put_u32((uint32_t)index + 1U);
    uart_put_string("/");
    uart_put_u32(RT_CASE_COUNT);
    uart_put_string(",group=");
    uart_put_string((item->group == RT_GROUP_CHALLENGE) ? "challenge" : "custom");
    uart_put_string(",profile=");
    uart_put_string(rt_impairment_name(item->impairment));
    uart_put_string(",type=");
    uart_put_string(waveform_type_name((waveform_type_t)item->type));
    uart_put_string(",freq_millihz=");
    uart_put_u32(rt_case_frequency_millihz(item));
    uart_put_string(",vpp_millivolts=");
    uart_put_u32(item->vpp_millivolts);
    uart_put_string(",duty_ppm=");
    if (item->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_u32((uint32_t)item->duty_permille * 1000U);
    } else {
        uart_put_string("NA");
    }
    uart_put_string(",cycles=");
    uart_put_u32(item->cycles);
    uart_put_string(",noise_permille=");
    uart_put_u32(item->noise_permille);
    uart_put_string(",edge_permille=");
    uart_put_u32(item->rise_permille);
    uart_put_char('/');
    uart_put_u32(item->fall_permille);
    uart_put_string("\r\n");

    uart_put_string("CAPTURING\r\n");
    rt_capture_case();
    uart_put_string("PROCESSING\r\n");

    /* 与 R 路径完全相同的帧组装：算法看不到数据是自激产生的。 */
    frame.samples = (const uint16_t *)g_adc_samples;
    frame.sample_count = WAVEFORM_SAMPLE_COUNT;
    frame.adc_full_scale = WAVEFORM_ADC_FULL_SCALE;
    frame.sample_rate_millihz = WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ;
    frame.adc_reference_microvolts = WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS;
    frame.frontend_gain_ppm = WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM;
    frame.sequence = ++g_frame_sequence;
    frame.flags = WAVEFORM_ACQUISITION_FLAGS;
    valid = waveform_analyze_frame(&frame, &result);
    print_result(&result, valid);

    passed = rt_check_case(item, &result, valid);

    /* 296 题默认只回传文本，避免向普通串口终端倾倒约 2.4 MiB 二进制。
     * 如需检查某一帧，矩阵中止后发送 D 可导出最近一帧。 */
    return passed;
}

/* 依次跑完整个 200+96 自激矩阵。 */
static void rt_run_matrix(void)
{
    uint16_t index;
    uint16_t custom_passed = 0U;
    uint16_t custom_failed = 0U;
    uint16_t challenge_passed = 0U;
    uint16_t challenge_failed = 0U;

    /* 先吃掉命令行尾的回车，否则它会被当成中止请求。 */
    rt_drain_rx();

    uart_put_string("RT-START,cases=");
    uart_put_u32(RT_CASE_COUNT);
    uart_put_string(",custom=");
    uart_put_u32(RT_CUSTOM_CASE_COUNT);
    uart_put_string(",challenge=");
    uart_put_u32(RT_CHALLENGE_CASE_COUNT);
    uart_put_string(",fs_hz=");
    uart_put_u32(WAVEFORM_SAMPLE_RATE_HZ);
    uart_put_string(",n=");
    uart_put_u32(WAVEFORM_SAMPLE_COUNT);
    uart_put_string(",dac=PA15,adc=PA25,dump=0\r\n");

    /* Explicitly enable the DAC module, output pin and amplifier before testing. */
    DL_DAC12_enable(DAC0);
    DL_DAC12_enableOutputPin(DAC0);
    DL_DAC12_setAmplifier(DAC0, DL_DAC12_AMP_ON);
    delay_cycles(CPUCLK_FREQ / 1000U); /* 约 1 ms。 */
    rt_report_gpio_preflight();
    rt_report_dac_preflight();

    for (index = 0U; index < RT_CASE_COUNT; ++index) {
        if (rt_abort_requested()) {
            uart_put_string("RT-ABORT,index=");
            uart_put_u32((uint32_t)index + 1U);
            uart_put_string("\r\n");
            break;
        }
        if (rt_measure_case(index)) {
            if (g_rt_cases[index].group == RT_GROUP_CHALLENGE) {
                ++challenge_passed;
            } else {
                ++custom_passed;
            }
        } else {
            if (g_rt_cases[index].group == RT_GROUP_CHALLENGE) {
                ++challenge_failed;
            } else {
                ++custom_failed;
            }
        }
    }

    /* 退出前一定把输出缓冲放回高阻，避免自激结束后 DAC 继续往 PA25 灌
     * 直流而污染后续的对外测量。 */
    DL_DAC12_setAmplifier(DAC0, DL_DAC12_AMP_OFF_TRISTATE);

    uart_put_string("RT-DONE,pass=");
    uart_put_u32((uint32_t)custom_passed + challenge_passed);
    uart_put_string(",fail=");
    uart_put_u32((uint32_t)custom_failed + challenge_failed);
    uart_put_string(",custom_pass=");
    uart_put_u32(custom_passed);
    uart_put_string(",custom_fail=");
    uart_put_u32(custom_failed);
    uart_put_string(",challenge_pass=");
    uart_put_u32(challenge_passed);
    uart_put_string(",challenge_fail=");
    uart_put_u32(challenge_failed);
    uart_put_string("\r\n");
    uart_put_string("READY\r\n");
}

/* 固件入口。嵌入式程序通常不会从 main 返回，因此末尾是无限循环。 */
int main(void)
{
    /* 初始化由 SysConfig 生成的时钟、ADC、定时器、UART 和 GPIO 寄存器。 */
    SYSCFG_DL_init();

    /* The starter project selected UART loopback and a floating input. These
     * application overrides avoid editing the protected .syscfg source. */
    DL_UART_Main_disableLoopbackMode(UART_0_INST);
    DL_GPIO_initDigitalInputFeatures(GPIO_GRP_0_PIN_0_IOMUX,
                                     DL_GPIO_INVERSION_DISABLE,
                                     DL_GPIO_RESISTOR_PULL_UP,
                                     DL_GPIO_HYSTERESIS_ENABLE,
                                     DL_GPIO_WAKEUP_DISABLE);

    /* Also map UART0 to PA10 (TX) and PA11 (RX) so the onboard LaunchPad XDS110
     * backchannel virtual COM port works directly without external jumper wires. */
    DL_GPIO_initPeripheralOutputFunction(IOMUX_PINCM21, IOMUX_PINCM21_PF_UART0_TX);
    DL_GPIO_initPeripheralInputFunction(IOMUX_PINCM22, IOMUX_PINCM22_PF_UART0_RX);

    /* 上电默认把 DAC 输出缓冲置成高阻。SysConfig 生成的初始化会打开输出，
     * 如果自激用的杜邦线一直挂在 PA15 和 PA25 之间，一个低阻的 DAC 输出
     * 会直接和外部信号源对抗，把对外测量的结果带偏。置成高阻后 PA15 等
     * 效于悬空，只有 RT 期间才真正驱动。 */
    DL_DAC12_setAmplifier(DAC0, DL_DAC12_AMP_OFF_TRISTATE);

    uart_put_string("\r\nTI-CUP WAVEFORM CLASSIFIER\r\n");
    uart_put_string("FW=algorithm-v2,API=0x00020000,INJECT=LE16-4096-FNV1A\r\n");
    uart_put_string("ADC=PA25, FS_HZ=");
    uart_put_milli(WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ);
    uart_put_string(", N=");
    uart_put_u32(WAVEFORM_SAMPLE_COUNT);
    uart_put_string("\r\n");
    /* 自激接线提示随横幅一起打印：现场最常犯的错就是忘了把 PA15 接到
     * PA25，或者接到了别的脚上，导致 RT 全是 SIGNAL_TOO_SMALL。 */
    uart_put_string("DAC=PA15 -> jumper to PA25 (ADC in) for RT self-test\r\n");
    uart_put_string("READY: R=ADC, RT=Self-test, W=Inject, T=Simulate, D=Dump\r\n");

    /* 每轮循环只处理一帧：等待触发 -> 准备数据 -> 分析 -> 输出 -> READY。 */
    for (;;) {
        waveform_result_t result;
        waveform_frame_t frame;
        bool valid;
        trigger_mode_t mode = wait_for_trigger();

        /* 自激环路自带完整的"播放-采集-分析-对比-上行"流程，并且会自己
         * 打印 READY，因此这里直接继续，不再走下面单帧分析的公共路径。 */
        if (mode == TRIGGER_DAC_LOOPBACK) {
            rt_run_matrix();
            wait_for_button_release();
            continue;
        }

        /* 样点导出走独立分支：不做分析、不动帧号，也不影响比赛路径的输出
         * 格式。上位机可以用它在任意一帧之后取回原始波形。 */
        if (mode == TRIGGER_SAMPLE_DUMP) {
            dump_samples();
            uart_put_string("READY\r\n");
            continue;
        }

        if (mode == TRIGGER_ADC_CAPTURE) {
            uart_put_string("CAPTURING\r\n");
            capture_samples();
        } else if (mode == TRIGGER_UART_INJECT) {
            if (!receive_uart_samples()) {
                uart_put_string("READY\r\n");
                continue;
            }
        } else if (mode == TRIGGER_ONCHIP_SELFTEST) {
            uart_put_string("GENERATING\r\n");
            generate_simulated_samples();
        }

        uart_put_string("PROCESSING\r\n");
        /* volatile 只与采集中断共享有关；分析开始时采集已经停止，因此可以
         * 作为只读普通数组交给同步算法。 */
        frame.samples = (const uint16_t *)g_adc_samples;
        frame.sample_count = WAVEFORM_SAMPLE_COUNT;
        frame.adc_full_scale = WAVEFORM_ADC_FULL_SCALE;
        frame.sample_rate_millihz =
            WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ;
        frame.adc_reference_microvolts =
            WAVEFORM_ACQUISITION_REFERENCE_MICROVOLTS;
        frame.frontend_gain_ppm =
            WAVEFORM_ACQUISITION_FRONTEND_GAIN_PPM;
        /* 前置 ++ 使第一帧编号为 1。帧号会原样出现在结果中。 */
        frame.sequence = ++g_frame_sequence;
        frame.flags = WAVEFORM_ACQUISITION_FLAGS;
        valid = waveform_analyze_frame(&frame, &result);
        print_result(&result, valid);
        wait_for_button_release();
        uart_put_string("READY\r\n");
    }
}

/* ADC“存储器 0 结果已装载”中断服务程序（ISR）。
 *
 * ISR 必须尽量短：读一个点、写入数组、必要时停止定时器并置完成标志。
 * 复杂分析放在主循环，避免处理时间超过 50 us 而错过下一个采样。 */
void ADC12_0_INST_IRQHandler(void)
{
    switch (DL_ADC12_getPendingInterrupt(ADC12_0_INST)) {
    case DL_ADC12_IIDX_MEM0_RESULT_LOADED:
        if (g_sample_index < WAVEFORM_SAMPLE_COUNT) {
            g_adc_samples[g_sample_index++] =
                DL_ADC12_getMemResult(ADC12_0_INST, ADC12_0_ADCMEM_0);
            /* 写入第 4096 点后立刻停触发源，主循环随后退出 __WFI 等待。 */
            if (g_sample_index >= WAVEFORM_SAMPLE_COUNT) {
                DL_TimerA_stopCounter(TIMER_0_INST);
                g_capture_done = true;
            }
        } else {
            /* 防御分支：即使出现意外的额外中断也绝不写出数组边界。 */
            DL_TimerA_stopCounter(TIMER_0_INST);
            g_capture_done = true;
        }
        break;
    default:
        break;
    }
}

/* TIMA0 的 ZERO 中断服务程序：自激播放期间，每个采样节拍把下一个 DAC 码
 * 写进 DATA0。
 *
 * 它与 ADC 的触发来自同一个硬件事件，所以"产生"和"采集"严格同源：
 * 第 k 个节拍上硬件先让 ADC 采样保持，随后本中断才更新 DAC，于是第 k 个
 * ADC 点采到的正好是第 k 个 DAC 码。这条时序也是"非播放时立即返回"的
 * 原因——对外测量的 R 路径只用到 ADC 侧，不需要为 DAC 付任何开销。
 *
 * 注意：无论是否在播放，都必须先调用 DL_Timer_getPendingInterrupt()，
 * 因为它会读取并清除中断标志。漏掉这一步中断标志会一直挂起，程序会
 * 反复重入本函数而卡死。 */
void TIMER_0_INST_IRQHandler(void)
{
    if (DL_Timer_getPendingInterrupt(TIMER_0_INST) == DL_TIMER_IIDX_ZERO) {
        if (g_dac_playing) {
            uint16_t phase = g_dac_phase;
            g_dac_phase = (uint16_t)(phase + g_rt_phase_inc);
            DL_DAC12_output12(DAC0, rt_wave_code(phase));
        }
    }
}
