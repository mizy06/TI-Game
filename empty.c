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
 * R/W/T 三个串口命令分别选择真实 ADC、电脑注入和片上模拟。算法本身
 * 不关心数据来自哪里，只要最终形成符合接口约定的 waveform_frame_t。
 */

#include "ti_msp_dl_config.h"
#include "waveform_analyzer.h"

#include <stdbool.h>
#include <stdint.h>

/* ADC 中断和主循环都会访问前三个变量，因此必须用 volatile 告诉编译器：
 * 它们可能在当前代码看不到的地方（中断）发生变化，不能缓存或省略读写。
 *
 * 8192 字节的样本数组放在静态区而不是函数栈，适合栈空间较小的单片机。
 * g_frame_sequence 只由主循环更新，不需要 volatile。 */
static volatile uint16_t g_adc_samples[WAVEFORM_SAMPLE_COUNT];
static volatile uint32_t g_sample_index;
static volatile bool g_capture_done;
static uint32_t g_frame_sequence;

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
 *
 * 除以 10 时余数是最低位，所以数字会先反向存入 digits，再倒序发送。
 * 32 位最大值 4294967295 正好 10 位，digits[10] 足够。do-while 保证数值
 * 为 0 时也会输出一个字符 '0'。 */
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
    TRIGGER_ONCHIP_SELFTEST
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
static uint8_t g_selftest_case = 0U;

/* 在单片机内存中生成一帧已知波形，用于在没有信号源和电脑注入工具时
 * 快速自检。这里使用浮点 sinf 只发生在自测数据生成阶段，不属于核心
 * waveform_analyzer.c 的定点算法。 */
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
}

/* 从 UART 接收一帧电脑注入的 ADC 数据。
 *
 * 线上格式固定为 4096 个 little-endian uint16_t，共 8192 字节。MSPM0
 * 本身也是小端，因此可直接把样本数组看作字节数组依次填充。函数同时
 * 计算 FNV-1a 回执，让电脑确认固件实际收到的内容与发送内容完全一致。 */
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
    return true;
}

/* 按键使用上拉输入：未按下读到 1，按下接地读到 0，所以与 0 比较。 */
static bool button_is_pressed(void)
{
    return (DL_GPIO_readPins(GPIO_GRP_0_PORT, GPIO_GRP_0_PIN_0_PIN) &
            GPIO_GRP_0_PIN_0_PIN) == 0U;
}

/* 一直等待用户选择下一帧数据来源。串口命令不区分大小写；板上按键等价
 * 于 R。按键再次读取前延时约 10 ms 是最简单的机械抖动过滤。 */
static trigger_mode_t wait_for_trigger(void)
{
    for (;;) {
        while (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            uint8_t value = DL_UART_Main_receiveData(UART_0_INST);
            if ((value == 'r') || (value == 'R')) {
                return TRIGGER_ADC_CAPTURE;
            }
            if ((value == 'w') || (value == 'W')) {
                return TRIGGER_UART_INJECT;
            }
            if ((value == 't') || (value == 'T')) {
                return TRIGGER_ONCHIP_SELFTEST;
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

    uart_put_string("\r\nTI-CUP WAVEFORM CLASSIFIER\r\n");
    uart_put_string("FW=algorithm-v2,API=0x00020000,INJECT=LE16-4096-FNV1A\r\n");
    uart_put_string("ADC=PA25, FS_HZ=");
    uart_put_milli(WAVEFORM_ACQUISITION_SAMPLE_RATE_MILLIHZ);
    uart_put_string(", N=");
    uart_put_u32(WAVEFORM_SAMPLE_COUNT);
    uart_put_string("\r\n");
    uart_put_string("READY: send R=ADC, W=Inject, T=Simulate\r\n");

    /* 每轮循环只处理一帧：等待触发 -> 准备数据 -> 分析 -> 输出 -> READY。 */
    for (;;) {
        waveform_result_t result;
        waveform_frame_t frame;
        bool valid;
        trigger_mode_t mode = wait_for_trigger();

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
