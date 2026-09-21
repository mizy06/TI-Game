#ifndef TI_MSP_DL_CONFIG_HOST_STUB_H_
#define TI_MSP_DL_CONFIG_HOST_STUB_H_

#include <stdbool.h>
#include <stdint.h>

typedef struct { uint32_t unused; } UART_Regs;
typedef struct { uint32_t unused; } GPIO_Regs;
typedef struct { uint32_t unused; } GPTIMER_Regs;
typedef struct { uint32_t unused; } ADC12_Regs;
typedef struct { uint32_t unused; } DMA_Regs;

static UART_Regs g_stub_uart;
static GPIO_Regs g_stub_gpio;
static GPTIMER_Regs g_stub_timer;
static ADC12_Regs g_stub_adc;
static DMA_Regs g_stub_dma;

#define UART_0_INST               (&g_stub_uart)
#define GPIO_GRP_0_PORT           (&g_stub_gpio)
#define GPIO_GRP_0_PIN_0_PIN      (1U << 13U)
#define GPIO_GRP_0_PIN_0_IOMUX    (30U)
#define TIMER_0_INST              (&g_stub_timer)
#define ADC12_0_INST              (&g_stub_adc)
#define ADC12_0_ADCMEM_0          (0U)
#define ADC12_0_INST_INT_IRQN     (0U)
#define DMA                       (&g_stub_dma)
#define DMA_CH0_CHAN_ID           (0U)
#define DL_ADC12_INTERRUPT_DMA_DONE           (1U)
#define DL_ADC12_INTERRUPT_UNDERFLOW          (2U)
#define DL_ADC12_IIDX_DMA_DONE                (1U)
#define DL_ADC12_IIDX_UNDERFLOW                (2U)
#define DL_ADC12_DMA_MEM0_RESULT_LOADED       (1U)
#define DL_DMA_INTERRUPT_CHANNEL0             (1U)
#define DL_DMA_INTERRUPT_ADDR_ERROR           (2U)
#define DL_DMA_INTERRUPT_DATA_ERROR           (4U)
#define DL_DMA_SINGLE_TRANSFER_MODE           (0U)
#define DL_DMA_NORMAL_MODE                    (0U)
#define DL_DMA_WIDTH_HALF_WORD                (1U)
#define DL_DMA_ADDR_UNCHANGED                 (0U)
#define DL_DMA_ADDR_INCREMENT                 (1U)
#define DL_UART_MAIN_INTERRUPT_OVERRUN_ERROR  (1U)
#define DL_UART_MAIN_INTERRUPT_BREAK_ERROR    (2U)
#define DL_UART_MAIN_INTERRUPT_PARITY_ERROR   (4U)
#define DL_UART_MAIN_INTERRUPT_FRAMING_ERROR  (8U)
#define DL_UART_MAIN_INTERRUPT_NOISE_ERROR    (16U)
#define DL_GPIO_INVERSION_DISABLE             (0U)
#define DL_GPIO_RESISTOR_PULL_UP               (1U)
#define DL_GPIO_HYSTERESIS_ENABLE              (1U)
#define DL_GPIO_WAKEUP_DISABLE                 (0U)
#define CPUCLK_FREQ                            (32000000U)

#define ADC12_0_INST_IRQHandler ADC0_IRQHandler

static inline void SYSCFG_DL_init(void) {}
static inline bool DL_UART_Main_isTXFIFOFull(UART_Regs *uart)
{ (void)uart; return false; }
static inline void DL_UART_Main_transmitData(UART_Regs *uart, uint8_t value)
{ (void)uart; (void)value; }
static inline bool DL_UART_Main_isRXFIFOEmpty(UART_Regs *uart)
{ (void)uart; return true; }
static inline uint8_t DL_UART_Main_receiveData(UART_Regs *uart)
{ (void)uart; return 0U; }
static inline uint32_t DL_UART_Main_getRawInterruptStatus(
    UART_Regs *uart, uint32_t mask)
{ (void)uart; (void)mask; return 0U; }
static inline void DL_UART_Main_clearInterruptStatus(
    UART_Regs *uart, uint32_t mask)
{ (void)uart; (void)mask; }
static inline void DL_UART_Main_disableLoopbackMode(UART_Regs *uart)
{ (void)uart; }
static inline uint32_t DL_GPIO_readPins(GPIO_Regs *gpio, uint32_t pins)
{ (void)gpio; return pins; }
static inline void DL_GPIO_initDigitalInputFeatures(uint32_t iomux,
                                                    uint32_t inversion,
                                                    uint32_t resistor,
                                                    uint32_t hysteresis,
                                                    uint32_t wakeup)
{ (void)iomux; (void)inversion; (void)resistor; (void)hysteresis; (void)wakeup; }
static inline void DL_TimerA_stopCounter(GPTIMER_Regs *timer)
{ (void)timer; }
static inline void DL_TimerA_startCounter(GPTIMER_Regs *timer)
{ (void)timer; }
static inline void DL_ADC12_clearInterruptStatus(ADC12_Regs *adc, uint32_t mask)
{ (void)adc; (void)mask; }
static inline uint32_t DL_ADC12_getPendingInterrupt(ADC12_Regs *adc)
{ (void)adc; return DL_ADC12_IIDX_DMA_DONE; }
static inline uint32_t DL_ADC12_getMemResultAddress(
    ADC12_Regs *adc, uint32_t index)
{ (void)adc; (void)index; return 0U; }
static inline void DL_ADC12_enableDMA(ADC12_Regs *adc) { (void)adc; }
static inline void DL_ADC12_disableDMA(ADC12_Regs *adc) { (void)adc; }
static inline void DL_ADC12_enableDMATrigger(ADC12_Regs *adc, uint32_t mask)
{ (void)adc; (void)mask; }
static inline void DL_ADC12_disableDMATrigger(ADC12_Regs *adc, uint32_t mask)
{ (void)adc; (void)mask; }
static inline void DL_ADC12_clearDMATriggerStatus(ADC12_Regs *adc,
                                                  uint32_t mask)
{ (void)adc; (void)mask; }
static inline void DL_DMA_disableChannel(DMA_Regs *dma, uint8_t channel)
{ (void)dma; (void)channel; }
static inline void DL_DMA_enableChannel(DMA_Regs *dma, uint8_t channel)
{ (void)dma; (void)channel; }
static inline void DL_DMA_configTransfer(DMA_Regs *dma, uint8_t channel,
                                         uint32_t transfer_mode,
                                         uint32_t extended_mode,
                                         uint32_t src_width,
                                         uint32_t dest_width,
                                         uint32_t src_increment,
                                         uint32_t dest_increment)
{ (void)dma; (void)channel; (void)transfer_mode; (void)extended_mode;
  (void)src_width; (void)dest_width; (void)src_increment; (void)dest_increment; }
static inline void DL_DMA_setSrcAddr(
    DMA_Regs *dma, uint8_t channel, uint32_t address)
{ (void)dma; (void)channel; (void)address; }
static inline void DL_DMA_setDestAddr(
    DMA_Regs *dma, uint8_t channel, uint32_t address)
{ (void)dma; (void)channel; (void)address; }
static inline void DL_DMA_setTransferSize(
    DMA_Regs *dma, uint8_t channel, uint16_t size)
{ (void)dma; (void)channel; (void)size; }
static inline void DL_DMA_clearInterruptStatus(DMA_Regs *dma, uint32_t mask)
{ (void)dma; (void)mask; }
static inline uint32_t DL_DMA_getRawInterruptStatus(
    DMA_Regs *dma, uint32_t mask)
{ (void)dma; (void)mask; return 0U; }
static inline void NVIC_ClearPendingIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_EnableIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_DisableIRQ(uint32_t irq) { (void)irq; }
static inline void delay_cycles(uint32_t cycles) { (void)cycles; }
static inline void __disable_irq(void) {}
static inline void __enable_irq(void) {}
static inline void __WFI(void) {}

#endif /* TI_MSP_DL_CONFIG_HOST_STUB_H_ */
