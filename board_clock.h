#ifndef BOARD_CLOCK_H_
#define BOARD_CLOCK_H_

/* 时钟由 empty.syscfg 在启动时配置；此处只核验，不重新切换时钟。
 * 原装 LP-MSPM0G3507 Y2=40 MHz。SYSOSC 保持开启供 PLL 电路使用，
 * 但 PLL 参考为 HFXT；采样定时器和 ADC 均不再依赖 SYSOSC 的 FCL。 */
#if CPUCLK_FREQ != 32000000U
#error "External clock setup requires a 32 MHz generated peripheral configuration"
#endif
#if ACQ_HFXT_FREQ_HZ != 40000000UL
#error "Update empty.syscfg and board_clock.h together for a different physical crystal"
#endif

#define BOARD_CLOCK_DESCRIPTION "HFXT=40MHz, PLL=32MHz, CLOCK=EXTERNAL"

static bool board_clock_is_ready(void)
{
    const uint32_t required = DL_SYSCTL_CLK_STATUS_HFCLK_GOOD |
                              DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD |
                              DL_SYSCTL_CLK_STATUS_HSCLK_GOOD;
    return (DL_SYSCTL_getClockStatus() & required) == required &&
           DL_SYSCTL_getMCLKSource() == DL_SYSCTL_MCLK_SOURCE_HSCLK &&
           DL_SYSCTL_getHSCLKSource() == DL_SYSCTL_HSCLK_SOURCE_SYSPLL &&
           (SYSCTL->SOCLOCK.SYSPLLCFG0 & SYSCTL_SYSPLLCFG0_SYSPLLREF_MASK) ==
               DL_SYSCTL_SYSPLL_REF_HFCLK;
}

#endif
