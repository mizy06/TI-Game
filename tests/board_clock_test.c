/* Native fault-injection harness; no SDK or connected board is required.
 * Checks boot gating, PLL retry and the clock ratio erratum workaround. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CPUCLK_FREQ 32000000U
#define DL_SYSCTL_CLK_STATUS_HFCLK_GOOD 1U
#define DL_SYSCTL_CLK_STATUS_SYSPLL_OFF 2U
#define DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD 4U
#define DL_SYSCTL_CLK_STATUS_HSCLK_GOOD 8U
#define DL_SYSCTL_HFXT_RANGE_32_48_MHZ 0U
#define DL_SYSCTL_SYSPLL_REF_HFCLK 1U
#define SYSCTL_SYSPLLCFG0_SYSPLLREF_MASK 1U
#define DL_SYSCTL_SYSPLL_PDIV_1 0U
#define SYSCTL_SYSPLLCFG1_QDIV_OFS 4U
#define SYSCTL_SYSPLLCFG1_PDIV_MASK 3U
#define SYSCTL_SYSPLLCFG1_QDIV_MASK 0x7f0U
#define SYSCTL_SYSPLLCFG0_RDIVCLK2X_OFS 4U
#define SYSCTL_SYSPLLCFG0_RDIVCLK2X_MASK 0xf0U
#define SYSCTL_SYSPLLCFG0_RDIVCLK1_MASK 0xf00U
#define SYSCTL_SYSPLLCFG0_RDIVCLK0_MASK 0xf000U
#define DL_SYSCTL_SYSPLL_CLK2X_ENABLE 0x10000U
#define SYSCTL_SYSPLLCFG0_ENABLECLK2X_MASK 0x10000U
#define SYSCTL_SYSPLLCFG0_ENABLECLK1_MASK 0x20000U
#define SYSCTL_SYSPLLCFG0_ENABLECLK0_MASK 0x40000U
#define DL_SYSCTL_SYSPLL_MCLK_CLK2X 0x80000U
#define SYSCTL_SYSPLLCFG0_MCLK2XVCO_MASK 0x80000U
#define DL_CORE_PREFETCH_ENABLED 1U
#define DL_CORE_CACHE_DISABLED 0U
#define DL_CORE_LITERAL_CACHE_ENABLED 1U
#define DL_SYSCTL_FCC_TRIG_TYPE_RISE_RISE 0U
#define DL_SYSCTL_FCC_TRIG_SOURCE_LFCLK 0U
#define DL_SYSCTL_FCC_TRIG_CNT_01 0U
#define DL_SYSCTL_HSCLK_SOURCE_SYSPLL 0U
#define IOMUX_PINCM10 9U
#define IOMUX_PINCM11 10U
#define DL_ADC12_CLOCK_HFCLK 1U
#define DL_ADC12_CLOCK_DIVIDE_2 2U
#define DL_ADC12_CLOCK_FREQ_RANGE_32_TO_40 40U
#define DL_ADC12_CLOCK_FREQ_RANGE_40_TO_48 48U
#define ADC12_0_INST 0U
typedef enum { DL_SYSCTL_FCC_CLOCK_SOURCE_SYSPLLCLK2X,
               DL_SYSCTL_FCC_CLOCK_SOURCE_HFCLK } DL_SYSCTL_FCC_CLOCK_SOURCE;
typedef struct { uint32_t clockSel, divideRatio, freqRange; } DL_ADC12_ClockConfig;
static struct { struct { uint32_t SYSPLLCFG0, SYSPLLCFG1,
    SYSPLLPARAM0, SYSPLLPARAM1; } SOCLOCK; } fake_sysctl;
static struct { uint32_t CTL; } fake_cpuss;
#define SYSCTL (&fake_sysctl)
#define CPUSS (&fake_cpuss)
static uint32_t factory_parameters[] = {0x1234U, 0x5678U};
#define DL_SYSCTL_SYSPLL_INPUT_FREQ_32_48_MHZ ((uintptr_t)factory_parameters)

enum fault { NONE, NO_CRYSTAL, NO_PLL_LOCK, WRONG_PLL_RATIO, FCC_TIMEOUT,
             ZERO_FCC, HSCLK_FAILURE, RETRY_LOCK, PLL_STUCK_ON };
static enum fault fault;
static bool hfclk_on, pll_on, hsclk_selected, mclk_switched, adc_configured;
static uint32_t enables, delays, pins;
static DL_SYSCTL_FCC_CLOCK_SOURCE fcc_source;

static void delay_cycles(uint32_t cycles) { assert(cycles != 0); ++delays; }
static void DL_GPIO_initPeripheralAnalogFunction(uint32_t pin)
{ assert(pin == 9U || pin == 10U); ++pins; }
static void DL_SYSCTL_setHFCLKSourceHFXTParams(uint32_t range, uint32_t startup, bool monitor)
{ (void)range; assert(startup == 10U && !monitor); hfclk_on = true; }
static void DL_SYSCTL_enableHFCLKStartupMonitor(void) {}
static void DL_SYSCTL_disableHFXT(void) { hfclk_on = false; }
static void DL_SYSCTL_disableSYSPLL(void) { pll_on = false; }
static void DL_SYSCTL_enableSYSPLL(void) { pll_on = true; ++enables; }
static uint32_t DL_SYSCTL_getClockStatus(void)
{
    uint32_t status = 0U;
    if (hfclk_on && fault != NO_CRYSTAL) status |= DL_SYSCTL_CLK_STATUS_HFCLK_GOOD;
    if (!pll_on && fault != PLL_STUCK_ON) status |= DL_SYSCTL_CLK_STATUS_SYSPLL_OFF;
    if (pll_on && fault != NO_PLL_LOCK) status |= DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD;
    if (hsclk_selected && fault != HSCLK_FAILURE) status |= DL_SYSCTL_CLK_STATUS_HSCLK_GOOD;
    return status;
}
static void DL_Common_updateReg(uint32_t *reg, uint32_t value, uint32_t mask)
{ *reg = (*reg & ~mask) | (value & mask); }
static uint32_t DL_CORE_getInstructionConfig(void) { return 0xabcU; }
static void DL_CORE_configInstruction(uint32_t a, uint32_t b, uint32_t c)
{ (void)a; (void)b; (void)c; }
static void DL_SYSCTL_setFCCPeriods(uint32_t period) { (void)period; }
static void DL_SYSCTL_configFCC(uint32_t a, uint32_t b, DL_SYSCTL_FCC_CLOCK_SOURCE source)
{ (void)a; (void)b; fcc_source = source; }
static void DL_SYSCTL_startFCC(void) {}
static bool DL_SYSCTL_isFCCDone(void) { return fault != FCC_TIMEOUT; }
static uint32_t DL_SYSCTL_readFCC(void)
{
    if (fault == ZERO_FCC) return 0U;
    if (fcc_source == DL_SYSCTL_FCC_CLOCK_SOURCE_HFCLK) return ACQ_HFXT_FREQ_HZ / 32000U;
    if (fault == WRONG_PLL_RATIO || (fault == RETRY_LOCK && enables == 1U)) return 2000U;
    return 1000U;
}
static void DL_SYSCTL_setHSCLKSource(uint32_t source)
{ (void)source; hsclk_selected = true; }
#define DL_SYSCTL_setMCLKSource(current, next, source) \
    do { (void)(source); assert(hsclk_selected); mclk_switched = true; } while (0)
static void DL_ADC12_setClockConfig(uint32_t instance, DL_ADC12_ClockConfig *cfg)
{
    (void)instance;
    assert(mclk_switched && cfg->clockSel == DL_ADC12_CLOCK_HFCLK);
    assert(cfg->divideRatio == 2U && cfg->freqRange == ACQ_HFXT_FREQ_HZ / 1000000U);
    adc_configured = true;
}

#include "board_clock.h"

int main(void)
{
    for (fault = NONE; fault <= PLL_STUCK_ON; ++fault) {
        bool expected = fault == NONE || fault == RETRY_LOCK;
        hfclk_on = pll_on = hsclk_selected = mclk_switched = adc_configured = false;
        enables = delays = pins = 0U;
        memset(&fake_sysctl, 0, sizeof(fake_sysctl));
        assert(board_clock_init() == expected);
        assert(mclk_switched == expected && adc_configured == expected);
        assert(pins == 2U && delays < 5000U);
        if (expected) {
            assert(enables == (fault == RETRY_LOCK ? 2U : 1U));
            assert(fake_sysctl.SOCLOCK.SYSPLLPARAM0 == factory_parameters[0]);
            assert(fake_sysctl.SOCLOCK.SYSPLLPARAM1 == factory_parameters[1]);
            assert(fake_cpuss.CTL == 0xabcU);
            assert((fake_sysctl.SOCLOCK.SYSPLLCFG1 & SYSCTL_SYSPLLCFG1_QDIV_MASK) == 16U);
            assert((fake_sysctl.SOCLOCK.SYSPLLCFG0 & SYSCTL_SYSPLLCFG0_RDIVCLK2X_MASK) ==
                   BOARD_PLL_CLK2X_DIV << SYSCTL_SYSPLLCFG0_RDIVCLK2X_OFS);
        } else {
            assert(!hfclk_on && !pll_on);
        }
    }
    puts("External clock: success, retry and 7 failure paths passed");
    return 0;
}
