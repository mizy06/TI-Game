#ifndef TI_MSP_DL_CONFIG_HOST_STUB_H_
#define TI_MSP_DL_CONFIG_HOST_STUB_H_

/*
 * 主机语法检查用的最小 TI DriverLib 替身（stub）。
 *
 * 电脑上没有 MSPM0 寄存器和 TI SDK，但仍希望 cc -Wall -Werror 检查
 * empty.c 的普通 C 语法。这里声明 empty.c 用到的类型、宏和函数，并让
 * 所有硬件函数成为“什么也不做”的 static inline。它不能模拟真实硬件，
 * 也不参与开发板固件构建，只用于 tests/run_all_checks.sh。
 */

#include <stdbool.h>
#include <stdint.h>

/* 占位寄存器类型至少含一个成员，避免某些 C 编译器不接受空结构体。 */
typedef struct { uint32_t unused; } UART_Regs;
typedef struct { uint32_t unused; } GPIO_Regs;
typedef struct { uint32_t unused; } GPTIMER_Regs;
typedef struct { uint32_t unused; } ADC12_Regs;
typedef struct { uint32_t unused; } DAC12_Regs;

/* 给实例宏一个类型正确且非空的地址。 */
static UART_Regs g_stub_uart;
static GPIO_Regs g_stub_gpio;
static GPTIMER_Regs g_stub_timer;
static ADC12_Regs g_stub_adc;
static DAC12_Regs g_stub_dac;

/* 名称与 SysConfig 生成的 ti_msp_dl_config.h 保持一致，具体数值只需让
 * empty.c 编译，绝不代表真实寄存器地址或引脚复用编码。 */
#define UART_0_INST               (&g_stub_uart)
#define GPIO_GRP_0_PORT           (&g_stub_gpio)
#define GPIO_GRP_0_PIN_0_PIN      (1U << 13U)
#define GPIO_GRP_0_PIN_0_IOMUX    (30U)
#define TIMER_0_INST              (&g_stub_timer)
#define ADC12_0_INST              (&g_stub_adc)
#define DAC0                      (&g_stub_dac)
#define ADC12_0_ADCMEM_0          (0U)
#define ADC12_0_INST_INT_IRQN     (0U)
#define TIMER_0_INST_INT_IRQN     (1U)
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

/* 模拟生成头文件对中断函数名的别名，避免 empty.c 重复定义实际向量名。 */
#define ADC12_0_INST_IRQHandler ADC0_IRQHandler
#define TIMER_0_INST_IRQHandler TIMA0_IRQHandler

/* DAC12 与定时器中断在真实 SDK 里是枚举类型；主机语法检查只需要能编译
 * 通过的整数常量，取值本身没有硬件含义。 */
typedef uint32_t DL_DAC12_AMP;
#define DL_DAC12_AMP_ON           (1U)
#define DL_DAC12_AMP_OFF_TRISTATE (2U)
typedef uint32_t DL_TIMER_IIDX;
#define DL_TIMER_IIDX_ZERO        (1U)

/* 以下 inline 函数有与 DriverLib 调用相容的签名。把未使用参数显式转为
 * void 可在 -Wextra -Werror 下消除告警。返回值只选择能让语法测试终止或
 * 编译的固定值；这些函数不会被 run_all_checks.sh 真正执行。 */
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
static inline void DL_DAC12_output12(DAC12_Regs *dac, uint32_t value)
{ (void)dac; (void)value; }
static inline void DL_DAC12_setAmplifier(DAC12_Regs *dac, DL_DAC12_AMP amp)
{ (void)dac; (void)amp; }
static inline DL_TIMER_IIDX DL_Timer_getPendingInterrupt(GPTIMER_Regs *timer)
{ (void)timer; return DL_TIMER_IIDX_ZERO; }
static inline void NVIC_ClearPendingIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_EnableIRQ(uint32_t irq) { (void)irq; }
static inline void NVIC_DisableIRQ(uint32_t irq) { (void)irq; }
static inline void delay_cycles(uint32_t cycles) { (void)cycles; }
static inline void __disable_irq(void) {}
static inline void __enable_irq(void) {}
static inline void __WFI(void) {}

#endif /* TI_MSP_DL_CONFIG_HOST_STUB_H_ */
