/*
 * TI Cup waveform classifier application for MSPM0G3507.
 *
 * This is an application-owned source file. SysConfig-generated files under
 * the active build directory remain untouched and may be regenerated safely.
 */

#include "ti_msp_dl_config.h"
#include "waveform_analyzer.h"

#include <stdbool.h>
#include <stdint.h>

static volatile uint16_t g_adc_samples[WAVEFORM_SAMPLE_COUNT];
static volatile uint32_t g_sample_index;
static volatile bool g_capture_done;

static void uart_put_char(char value)
{
    while (DL_UART_Main_isTXFIFOFull(UART_0_INST)) {
    }
    DL_UART_Main_transmitData(UART_0_INST, (uint8_t)value);
}

static void uart_put_string(const char *text)
{
    while (*text != '\0') {
        uart_put_char(*text++);
    }
}

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

static void uart_put_milli(uint32_t value)
{
    uart_put_u32(value / 1000U);
    uart_put_char('.');
    uart_put_char((char)('0' + ((value / 100U) % 10U)));
    uart_put_char((char)('0' + ((value / 10U) % 10U)));
    uart_put_char((char)('0' + (value % 10U)));
}

static void uart_put_percent_permille(uint16_t permille)
{
    uart_put_u32(permille / 10U);
    uart_put_char('.');
    uart_put_char((char)('0' + (permille % 10U)));
}

#include <math.h>

typedef enum {
    TRIGGER_ADC_CAPTURE = 0,
    TRIGGER_UART_INJECT,
    TRIGGER_ONCHIP_SELFTEST
} trigger_mode_t;

static uint32_t g_sim_rng = 0x13579BDFU;

static uint32_t sim_rand(void)
{
    g_sim_rng = (1664525U * g_sim_rng) + 1013904223U;
    return g_sim_rng;
}

static uint8_t g_selftest_case = 0U;

static void generate_simulated_samples(void)
{
    /* 4 standard noisy test cases:
     * 0: Sine 200 Hz, Vpp ~2.0V, 15% noise
     * 1: Square 250 Hz, 30% duty, Vpp ~2.0V, 15% noise
     * 2: Triangle 150 Hz, Vpp ~2.0V, 15% noise
     * 3: Sawtooth 300 Hz, Vpp ~2.0V, 15% noise
     */
    uint32_t freq_hz = (g_selftest_case == 0U) ? 200U :
                       (g_selftest_case == 1U) ? 250U :
                       (g_selftest_case == 2U) ? 150U : 300U;
    uint32_t wave_type = g_selftest_case;
    g_selftest_case = (g_selftest_case + 1U) % 4U;

    uint32_t phase_inc = (freq_hz * 65536U) / WAVEFORM_SAMPLE_RATE_HZ;
    uint32_t phase = 0U;
    int32_t vpp_counts = 2482; // ~2.0V (2.0 * 4095 / 3.3)
    int32_t center = 2048;     // 1.65V DC offset
    int32_t noise_amp = vpp_counts * 15 / 100; // 15% noise

    for (uint32_t i = 0U; i < WAVEFORM_SAMPLE_COUNT; ++i) {
        int32_t val = 0;
        uint16_t p16 = (uint16_t)phase;

        switch (wave_type) {
        case 0: // Sine
            val = (int32_t)(sinf(2.0f * 3.14159265f * (float)phase / 65536.0f) * (float)(vpp_counts / 2));
            break;
        case 1: // Square (30% duty)
            val = (p16 < (65536U * 30U / 100U)) ? (vpp_counts / 2) : -(vpp_counts / 2);
            break;
        case 2: // Triangle
            if (p16 < 16384U) {
                val = (int32_t)(((int64_t)p16 * vpp_counts) / 16384U) - (vpp_counts / 2);
            } else if (p16 < 49152U) {
                val = (vpp_counts / 2) - (int32_t)(((int64_t)(p16 - 16384U) * vpp_counts) / 16384U);
            } else {
                val = -(vpp_counts / 2) + (int32_t)(((int64_t)(p16 - 49152U) * vpp_counts) / 16384U);
            }
            break;
        case 3: // Falling Sawtooth
        default:
            val = (vpp_counts / 2) - (int32_t)(((int64_t)p16 * vpp_counts) / 65536U);
            break;
        }

        // Add pseudo-random noise (max 15% Vpp)
        int32_t noise = (int32_t)(sim_rand() % (uint32_t)(noise_amp * 2 + 1)) - noise_amp;
        int32_t sample = center + val + noise;
        if (sample < 0) sample = 0;
        if (sample > 4095) sample = 4095;
        g_adc_samples[i] = (uint16_t)sample;

        phase = (phase + phase_inc) & 0xFFFFU;
    }
}

static bool receive_uart_samples(void)
{
    uint8_t *dest = (uint8_t *)(uintptr_t)g_adc_samples;
    uint32_t total = WAVEFORM_SAMPLE_COUNT * sizeof(uint16_t);
    uint32_t count = 0U;
    uint32_t timeout_loops = 0U;

    uart_put_string("READY_FOR_DATA\r\n");

    while (count < total) {
        if (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            dest[count++] = DL_UART_Main_receiveData(UART_0_INST);
            timeout_loops = 0U;
        } else {
            delay_cycles(CPUCLK_FREQ / 100000U); // ~10 us
            if (++timeout_loops > 250000U) {     // ~2.5s timeout
                uart_put_string("ERR_TIMEOUT\r\n");
                return false;
            }
        }
    }
    return true;
}

static bool button_is_pressed(void)
{
    return (DL_GPIO_readPins(GPIO_GRP_0_PORT, GPIO_GRP_0_PIN_0_PIN) &
            GPIO_GRP_0_PIN_0_PIN) == 0U;
}

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

static void wait_for_button_release(void)
{
    while (button_is_pressed()) {
        delay_cycles(CPUCLK_FREQ / 1000U);
    }
    delay_cycles(CPUCLK_FREQ / 100U);
}

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

static void print_result(const waveform_result_t *result, bool valid)
{
    if (!valid) {
        uart_put_string("RESULT,status=");
        uart_put_string(waveform_status_name(result->status));
        uart_put_string(",type=unknown,high_clip_samples=");
        uart_put_u32(result->high_clip_samples);
        uart_put_string("\r\n");
        return;
    }

    uart_put_string("RESULT,status=OK,type=");
    uart_put_string(waveform_type_name(result->type));
    uart_put_string(",freq_hz=");
    uart_put_milli(result->frequency_millihz);
    uart_put_string(",vpp_v=");
    uart_put_milli(result->vpp_millivolts);
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_string(",duty_pct=");
        uart_put_percent_permille(result->duty_permille);
    } else {
        uart_put_string(",duty_pct=NA");
    }
    uart_put_string(",score_pct=");
    uart_put_percent_permille(result->best_score_permille);
    uart_put_string(",margin_pct=");
    if (result->best_score_permille >= result->second_score_permille) {
        uart_put_percent_permille((uint16_t)(result->best_score_permille -
                                             result->second_score_permille));
    } else {
        uart_put_string("0.0");
    }
    uart_put_string(",cycles=");
    uart_put_u32(result->accepted_cycles);
    uart_put_string("\r\n");
}

int main(void)
{
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
    uart_put_string("ADC=PA25, FS=20000Hz, N=4096\r\n");
    uart_put_string("READY: send R=ADC, W=Inject, T=Simulate\r\n");

    for (;;) {
        waveform_result_t result;
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
        valid = waveform_analyze((const uint16_t *)g_adc_samples, &result);
        print_result(&result, valid);
        wait_for_button_release();
        uart_put_string("READY\r\n");
    }
}

void ADC12_0_INST_IRQHandler(void)
{
    switch (DL_ADC12_getPendingInterrupt(ADC12_0_INST)) {
    case DL_ADC12_IIDX_MEM0_RESULT_LOADED:
        if (g_sample_index < WAVEFORM_SAMPLE_COUNT) {
            g_adc_samples[g_sample_index++] =
                DL_ADC12_getMemResult(ADC12_0_INST, ADC12_0_ADCMEM_0);
            if (g_sample_index >= WAVEFORM_SAMPLE_COUNT) {
                DL_TimerA_stopCounter(TIMER_0_INST);
                g_capture_done = true;
            }
        } else {
            DL_TimerA_stopCounter(TIMER_0_INST);
            g_capture_done = true;
        }
        break;
    default:
        break;
    }
}
