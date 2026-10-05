/* MSPM0G3507 acquisition firmware: 4096 ADC samples via TIMA0 + DMA_CH0.
 * Classification and all physical-unit calculations run on the host. */
#include "ti_msp_dl_config.h"
#include "acquisition_config.h"
#include <stdbool.h>
#include <stdint.h>

static volatile uint16_t g_adc_samples[ACQ_SAMPLE_COUNT];
static volatile bool g_capture_done;
static volatile bool g_capture_failed;
static uint32_t g_frame_sequence;

#if ACQ_SAMPLE_COUNT > 65535
#error "DMA transfer size must fit the 16-bit transfer counter"
#endif
#define CAPTURE_TIMEOUT_MS (500U)
#define UART_RX_ERROR_MASK (DL_UART_MAIN_INTERRUPT_OVERRUN_ERROR | \
                            DL_UART_MAIN_INTERRUPT_BREAK_ERROR | \
                            DL_UART_MAIN_INTERRUPT_PARITY_ERROR | \
                            DL_UART_MAIN_INTERRUPT_FRAMING_ERROR | \
                            DL_UART_MAIN_INTERRUPT_NOISE_ERROR)

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

/* DUMP v2: metadata, LE16 payload, FNV-1a over the exact transmitted bytes. */
static uint32_t frame_fnv1a(void)
{
    uint32_t hash = 2166136261U;
    uint32_t i;
    for (i = 0U; i < ACQ_SAMPLE_COUNT; ++i) {
        uint16_t sample = g_adc_samples[i];
        hash = (hash ^ (uint8_t)sample) * 16777619U;
        hash = (hash ^ (uint8_t)(sample >> 8)) * 16777619U;
    }
    return hash;
}

static void dump_adc_frame(void)
{
    uint32_t i;
    uart_put_string("DUMP,proto=2,bytes=");
    uart_put_u32(ACQ_SAMPLE_COUNT * 2U);
    uart_put_string(",fnv1a=");
    uart_put_u32(frame_fnv1a());
    uart_put_string(",seq=");
    uart_put_u32(++g_frame_sequence);
    uart_put_string(",fs_millihz=");
    uart_put_u32(ACQ_SAMPLE_RATE_MILLIHZ);
    uart_put_string(",vref_uv=");
    uart_put_u32(ACQ_REFERENCE_MICROVOLTS);
    uart_put_string(",gain_ppm=");
    uart_put_u32(ACQ_FRONTEND_GAIN_PPM);
    uart_put_string(",adc_max=");
    uart_put_u32(ACQ_ADC_FULL_SCALE);
    uart_put_string(",flags=");
    uart_put_u32(ACQ_FLAGS);
    uart_put_string("\r\n");
    for (i = 0U; i < ACQ_SAMPLE_COUNT; ++i) {
        uint16_t sample = g_adc_samples[i];
        uart_put_char((char)(uint8_t)sample);
        uart_put_char((char)(uint8_t)(sample >> 8));
    }
    uart_put_string("DUMP_END\r\n");
}

static bool button_is_pressed(void)
{
    return (DL_GPIO_readPins(GPIO_GRP_0_PORT, GPIO_GRP_0_PIN_0_PIN) &
            GPIO_GRP_0_PIN_0_PIN) == 0U;
}

static bool wait_for_trigger(void)
{
    for (;;) {
        (void)uart_rx_error_pending();
        while (!DL_UART_Main_isRXFIFOEmpty(UART_0_INST)) {
            uint8_t value = DL_UART_Main_receiveData(UART_0_INST);
            if ((value == 'r') || (value == 'R')) {
                return false;
            }
            if ((value == 'd') || (value == 'D')) {
                return false;
            }
        }
        if (button_is_pressed()) {
            delay_cycles(CPUCLK_FREQ / 100U);
            if (button_is_pressed()) {
                return true;
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
                           (uint16_t)ACQ_SAMPLE_COUNT);
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

int main(void)
{
    SYSCFG_DL_init();
    DL_UART_Main_disableLoopbackMode(UART_0_INST);
    DL_GPIO_initDigitalInputFeatures(GPIO_GRP_0_PIN_0_IOMUX,
                                     DL_GPIO_INVERSION_DISABLE,
                                     DL_GPIO_RESISTOR_PULL_UP,
                                     DL_GPIO_HYSTERESIS_ENABLE,
                                     DL_GPIO_WAKEUP_DISABLE);
    uart_put_string("\r\nTI-CUP ADC DMA HOST V2\r\n");
    uart_put_string("ADC=PA25, UART=PA10/PA11-XDS110, FS=20000Hz, N=4096\r\n");
    uart_put_string("READY: server R -> raw ADC frame; RT runs on host\r\n");
    for (;;) {
        bool from_button = wait_for_trigger();
        uart_put_string("CAPTURING\r\n");
        if (capture_samples()) {
            dump_adc_frame();
        } else {
            uart_put_string("ERR_ADC_DMA\r\n");
        }
        if (from_button) {
            wait_for_button_release();
        }
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
