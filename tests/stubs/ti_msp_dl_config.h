#ifndef TI_MSP_DL_CONFIG_HOST_STUB_H_
#define TI_MSP_DL_CONFIG_HOST_STUB_H_

#include <stdbool.h>
#include <stdint.h>

typedef struct { uint32_t unused; } UART_Regs;
typedef struct { uint32_t unused; } GPIO_Regs;
typedef struct { uint32_t unused; } GPTIMER_Regs;
typedef struct { uint32_t unused; } ADC12_Regs;

static UART_Regs g_stub_uart;
static GPIO_Regs g_stub_gpio;
static GPTIMER_Regs g_stub_timer;
static ADC12_Regs g_stub_adc;

#define UART_0_INST               (&g_stub_uart)
#define GPIO_GRP_0_PORT           (&g_stub_gpio)
#define GPIO_GRP_0_PIN_0_PIN      (1U << 13U)
#define GPIO_GRP_0_PIN_0_IOMUX    (30U)
#define TIMER_0_INST              (&g_stub_timer)
#define ADC12_0_INST              (&g_stub_adc)
#define ADC12_0_ADCMEM_0          (0U)
#define ADC12_0_INST_INT_IRQN     (0U)
#define DL_ADC12_INTERRUPT_MEM0_RESULT_LOADED (1U)
#define DL_ADC12_IIDX_MEM0_RESULT_LOADED      (1U)
#define DL_GPIO_INVERSION_DISABLE             (0U)
#define DL_GPIO_RESISTOR_PULL_UP               (1U)
#define DL_GPIO_HYSTERESIS_ENABLE              (1U)
#define DL_GPIO_WAKEUP_DISABLE                 (0U)
#define IOMUX_PINCM21                          (21U)
#define IOMUX_PINCM22                          (22U)
#define IOMUX_PINCM21_PF_UART0_TX              (1U)
#define IOMUX_PINCM22_PF_UART0_RX              (1U)
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
static inline void DL_GPIO_initPeripheralOutputFunction(uint32_t iomux,
                                                        uint32_t function)
{ (void)iomux; (void)function; }
static inline void DL_GPIO_initPeripheralInputFunction(uint32_t iomux,
                                                       uint32_t function)
{ (void)iomux; (void)function; }
static inline void DL_TimerA_stopCounter(GPTIMER_Regs *timer)
{ (void)timer; }
static inline void DL_TimerA_startCounter(GPTIMER_Regs *timer)
{ (void)timer; }
static inline void DL_ADC12_clearInterruptStatus(ADC12_Regs *adc, uint32_t mask)
{ (void)adc; (void)mask; }
static inline uint32_t DL_ADC12_getPendingInterrupt(ADC12_Regs *adc)
{ (void)adc; return DL_ADC12_IIDX_MEM0_RESULT_LOADED; }
static inline uint16_t DL_ADC12_getMemResult(ADC12_Regs *adc, uint32_t index)
{ (void)adc; (void)index; return 0U; }
static inline void NVIC_ClearPendingIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_EnableIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_DisableIRQ(uint32_t irq) { (void)irq; }
static inline void delay_cycles(uint32_t cycles) { (void)cycles; }
static inline void __disable_irq(void) {}
static inline void __enable_irq(void) {}
static inline void __WFI(void) {}

#endif /* TI_MSP_DL_CONFIG_HOST_STUB_H_ */
