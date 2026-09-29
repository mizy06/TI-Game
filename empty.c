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
static volatile bool g_capture_done;
static volatile bool g_capture_failed;
static uint32_t g_frame_sequence;

/* MSPM0G3507 has 32 KB SRAM.  Keep one 8 KB sample buffer and let the
 * analyzer operate in place; a second DMA buffer would unnecessarily consume
 * the stack/runtime headroom needed by waveform_analyze(). */
#if WAVEFORM_SAMPLE_COUNT > 65535
#error "DMA transfer size must fit the MSPM0 DMA 16-bit counter"
#endif

#define UART_RX_ERROR_MASK (DL_UART_MAIN_INTERRUPT_OVERRUN_ERROR | \
                            DL_UART_MAIN_INTERRUPT_BREAK_ERROR |   \
                            DL_UART_MAIN_INTERRUPT_PARITY_ERROR |  \
                            DL_UART_MAIN_INTERRUPT_FRAMING_ERROR | \
                            DL_UART_MAIN_INTERRUPT_NOISE_ERROR)
#define CAPTURE_TIMEOUT_MS (500U)

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

/* Convert an ADC-code DC estimate to volts with three decimal places. */
static void uart_put_offset_voltage(uint16_t counts)
{
    uint32_t millivolts = ((uint32_t)counts * 3300U +
                           (WAVEFORM_ADC_FULL_SCALE / 2U)) /
                          WAVEFORM_ADC_FULL_SCALE;
    uart_put_milli(millivolts);
}

static bool uart_rx_error_pending(void)
{
    uint32_t status = DL_UART_Main_getRawInterruptStatus(UART_0_INST,
                                                         UART_RX_ERROR_MASK);

    if (status == 0U) {
        return false;
    }
    DL_UART_Main_clearInterruptStatus(UART_0_INST, status);
    return true;
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

    uint32_t phase_inc = (freq_hz * 65536U) / WAVEFORM_SAMPLE_RATE_HZ;
    uint32_t phase = 0U;
    int32_t vpp_counts = 2482; // ~2.0V (2.0 * 4095 / 3.3)
    int32_t center = 2048;     // 1.65V DC offset
    int32_t noise_amp = vpp_counts * 15 / 200;

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
                val = (int32_t)(((int64_t)p16 * vpp_counts) / 32768U);
            } else if (p16 < 49152U) {
                val = (vpp_counts / 2) - (int32_t)(((int64_t)(p16 - 16384U) * vpp_counts) / 32768U);
            } else {
                val = -(vpp_counts / 2) + (int32_t)(((int64_t)(p16 - 49152U) * vpp_counts) / 32768U);
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
    volatile uint8_t *dest = (volatile uint8_t *)g_adc_samples;
    uint32_t total = WAVEFORM_SAMPLE_COUNT * sizeof(uint16_t);
    uint32_t count = 0U;
    uint32_t timeout_loops = 0U;
    uint32_t checksum = 2166136261U;

    uart_put_string("READY_FOR_DATA\r\n");

    while (count < total) {
        if (uart_rx_error_pending()) {
            uart_put_string("ERR_UART_RX\r\n");
            return false;
        }
        if (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            uint8_t value = DL_UART_Main_receiveData(UART_0_INST);
            dest[count++] = value;
            checksum = (checksum ^ value) * 16777619U;
            timeout_loops = 0U;
        } else {
            delay_cycles(CPUCLK_FREQ / 100000U); // ~10 us
            if (++timeout_loops > 250000U) {     // ~2.5s timeout
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
    for (count = 0U; count < WAVEFORM_SAMPLE_COUNT; ++count) {
        if (g_adc_samples[count] > WAVEFORM_ADC_FULL_SCALE) {
            uart_put_string("ERR_SAMPLE_RANGE\r\n");
            return false;
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
        (void)uart_rx_error_pending();
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

static void configure_adc_dma_capture(void)
{
    /* SysConfig creates DMA_CH0 and connects ADC0's event trigger.  Addresses
     * and size must be restored for every capture because a completed block
     * leaves the DMA destination at the end of the sample buffer. */
    DL_DMA_disableChannel(DMA, DMA_CH0_CHAN_ID);
    DL_DMA_configTransfer(DMA, DMA_CH0_CHAN_ID,
                          DL_DMA_SINGLE_TRANSFER_MODE,
                          DL_DMA_NORMAL_MODE,
                          DL_DMA_WIDTH_HALF_WORD,
                          DL_DMA_WIDTH_HALF_WORD,
                          DL_DMA_ADDR_UNCHANGED,
                          DL_DMA_ADDR_INCREMENT);
    DL_DMA_setSrcAddr(DMA, DMA_CH0_CHAN_ID,
                      DL_ADC12_getMemResultAddress(ADC12_0_INST,
                                                   ADC12_0_ADCMEM_0));
    DL_DMA_setDestAddr(DMA, DMA_CH0_CHAN_ID,
                       (uint32_t)(uintptr_t)g_adc_samples);
    DL_DMA_setTransferSize(DMA, DMA_CH0_CHAN_ID,
                           (uint16_t)WAVEFORM_SAMPLE_COUNT);
    DL_DMA_clearInterruptStatus(DMA, DL_DMA_INTERRUPT_CHANNEL0 |
                                     DL_DMA_INTERRUPT_ADDR_ERROR |
                                     DL_DMA_INTERRUPT_DATA_ERROR);

    /* DMA_DONE is reported by ADC0 when the configured DMA block completes.
     * Clear both terminal and fault status before re-arming.  Otherwise an
     * underflow from the previous frame can make the next frame fail before
     * its first conversion. */
    DL_ADC12_clearInterruptStatus(ADC12_0_INST,
                                  DL_ADC12_INTERRUPT_DMA_DONE |
                                      DL_ADC12_INTERRUPT_UNDERFLOW);
    DL_ADC12_clearDMATriggerStatus(ADC12_0_INST,
                                   DL_ADC12_DMA_MEM0_RESULT_LOADED);
    DL_ADC12_enableDMA(ADC12_0_INST);
    DL_ADC12_enableDMATrigger(ADC12_0_INST,
                              DL_ADC12_DMA_MEM0_RESULT_LOADED);
    DL_DMA_enableChannel(DMA, DMA_CH0_CHAN_ID);
}

static void stop_adc_dma_capture(bool failed)
{
    DL_TimerA_stopCounter(TIMER_0_INST);
    DL_ADC12_disableDMATrigger(ADC12_0_INST,
                               DL_ADC12_DMA_MEM0_RESULT_LOADED);
    DL_ADC12_disableDMA(ADC12_0_INST);
    DL_DMA_disableChannel(DMA, DMA_CH0_CHAN_ID);
    g_capture_failed = failed;
    g_capture_done = true;
}

static bool capture_samples(void)
{
    uint32_t elapsed_ms;

    __disable_irq();
    g_capture_done = false;
    g_capture_failed = false;
    DL_TimerA_stopCounter(TIMER_0_INST);
    configure_adc_dma_capture();
    NVIC_ClearPendingIRQ(ADC12_0_INST_INT_IRQN);
    NVIC_EnableIRQ(ADC12_0_INST_INT_IRQN);
    DL_TimerA_startCounter(TIMER_0_INST);
    __enable_irq();

    for (elapsed_ms = 0U;
         (!g_capture_done) && (elapsed_ms < CAPTURE_TIMEOUT_MS);
         ++elapsed_ms) {
        uint32_t dma_errors = DL_DMA_getRawInterruptStatus(
            DMA, DL_DMA_INTERRUPT_ADDR_ERROR | DL_DMA_INTERRUPT_DATA_ERROR);
        if (dma_errors != 0U) {
            DL_DMA_clearInterruptStatus(DMA, dma_errors);
            stop_adc_dma_capture(true);
            break;
        }
        delay_cycles(CPUCLK_FREQ / 1000U);
    }
    if (!g_capture_done) {
        stop_adc_dma_capture(true);
    }
    NVIC_DisableIRQ(ADC12_0_INST_INT_IRQN);
    return !g_capture_failed;
}

static void print_result(const waveform_result_t *result, bool valid)
{
    /* Competition CSV: type,frequency_hz,vpp_v,duty_pct,dc_offset_v.
     * Non-square waveforms use duty_pct=0.  Keep this line free of labels so
     * a host can parse it with a plain comma split. */
    if (!valid) {
        uart_put_string("unknown,0,0,0,0\r\n");
        return;
    }

    uart_put_string(waveform_type_name(result->type));
    uart_put_char(',');
    uart_put_milli(result->frequency_millihz);
    uart_put_char(',');
    uart_put_milli(result->vpp_millivolts);
    uart_put_char(',');
    if (result->type == WAVEFORM_TYPE_SQUARE) {
        uart_put_percent_permille(result->duty_permille);
    } else {
        uart_put_char('0');
    }
    uart_put_char(',');
    uart_put_offset_voltage(result->dc_offset_counts);
    uart_put_string("\r\n");
}

int main(void)
{
    SYSCFG_DL_init();

    /* Keep these runtime settings explicit so a regenerated SysConfig project
     * cannot silently restore loopback or a floating trigger input. */
    DL_UART_Main_disableLoopbackMode(UART_0_INST);
    DL_GPIO_initDigitalInputFeatures(GPIO_GRP_0_PIN_0_IOMUX,
                                     DL_GPIO_INVERSION_DISABLE,
                                     DL_GPIO_RESISTOR_PULL_UP,
                                     DL_GPIO_HYSTERESIS_ENABLE,
                                     DL_GPIO_WAKEUP_DISABLE);

    uart_put_string("\r\nTI-CUP WAVEFORM CLASSIFIER\r\n");
    uart_put_string("FW=hardware-dma-cascade-v2.1,API=0x00020100\r\n");
    uart_put_string("ALGO=cascade_rt3_spectral_yin,OUTPUT=CSV5\r\n");
    uart_put_string("INJECT=LE16-4096-FNV1A\r\n");
    uart_put_string("ADC=PA25, UART=PA10/PA11-XDS110, FS=20000Hz, N=4096\r\n");
    uart_put_string("READY: send R=ADC, W=Inject, T=Simulate\r\n");

    for (;;) {
        waveform_frame_t frame;
        waveform_result_t result;
        bool valid;
        trigger_mode_t mode = wait_for_trigger();

        if (mode == TRIGGER_ADC_CAPTURE) {
            uart_put_string("CAPTURING\r\n");
            if (!capture_samples()) {
                uart_put_string("ERR_ADC_DMA\r\n");
                wait_for_button_release();
                uart_put_string("READY\r\n");
                continue;
            }
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
        frame.samples = (const uint16_t *)g_adc_samples;
        frame.sample_count = WAVEFORM_SAMPLE_COUNT;
        frame.adc_full_scale = WAVEFORM_ADC_FULL_SCALE;
        frame.sample_rate_millihz = WAVEFORM_SAMPLE_RATE_HZ * 1000UL;
        frame.adc_reference_microvolts = 3300000UL;
        frame.frontend_gain_ppm = 1000000UL;
        frame.sequence = ++g_frame_sequence;
        frame.flags = WAVEFORM_FRAME_FLAG_CONTIGUOUS;
        valid = waveform_analyze_frame(&frame, &result);
        print_result(&result, valid);
        wait_for_button_release();
        uart_put_string("READY\r\n");
    }
}

void ADC12_0_INST_IRQHandler(void)
{
    switch (DL_ADC12_getPendingInterrupt(ADC12_0_INST)) {
    case DL_ADC12_IIDX_DMA_DONE:
        stop_adc_dma_capture(false);
        break;
    case DL_ADC12_IIDX_UNDERFLOW:
        stop_adc_dma_capture(true);
        break;
    default:
        break;
    }
}
