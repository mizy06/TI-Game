#ifndef BOARD_CLOCK_H_
#define BOARD_CLOCK_H_

/* Run after SYSCFG_DL_init(), before any acquisition. Keep the generated
 * 32 MHz BUSCLK configuration, but replace its reference with external Y2.
 * The .syscfg and generated files remain owned by SysConfig.
 */
#if CPUCLK_FREQ != 32000000U
#error "External clock setup requires a 32 MHz generated peripheral configuration"
#endif
#if ACQ_HFXT_FREQ_HZ == 40000000UL
#define BOARD_PLL_CLK2X_DIV (4U)  /* 40 MHz * 2 (VCO) * 2 / 5 = 32 MHz */
#define BOARD_ADC_CLOCK_RANGE DL_ADC12_CLOCK_FREQ_RANGE_32_TO_40
#define BOARD_CLOCK_DESCRIPTION "HFXT=40MHz, PLL=32MHz, CLOCK=EXTERNAL"
#elif ACQ_HFXT_FREQ_HZ == 48000000UL
#define BOARD_PLL_CLK2X_DIV (5U)  /* 48 MHz * 2 (VCO) * 2 / 6 = 32 MHz */
#define BOARD_ADC_CLOCK_RANGE DL_ADC12_CLOCK_FREQ_RANGE_40_TO_48
#define BOARD_CLOCK_DESCRIPTION "HFXT=48MHz, PLL=32MHz, CLOCK=EXTERNAL"
#else
#error "Supported external crystals are 40 MHz and 48 MHz"
#endif

/* SYSOSC is still driving the CPU during all startup checks. */
static bool board_clock_wait(uint32_t mask, uint32_t expected)
{
    uint32_t attempt;
    for (attempt = 0U; attempt < 1000U; ++attempt) {
        if ((DL_SYSCTL_getClockStatus() & mask) == expected) {
            return true;
        }
        delay_cycles(CPUCLK_FREQ / 10000U);
    }
    return false;
}

static bool board_clock_fcc_count(DL_SYSCTL_FCC_CLOCK_SOURCE source,
                                 uint32_t *count)
{
    uint32_t attempt;
    DL_SYSCTL_configFCC(DL_SYSCTL_FCC_TRIG_TYPE_RISE_RISE,
                        DL_SYSCTL_FCC_TRIG_SOURCE_LFCLK, source);
    DL_SYSCTL_startFCC();
    for (attempt = 0U; attempt < 66U; ++attempt) {
        if (DL_SYSCTL_isFCCDone()) {
            *count = DL_SYSCTL_readFCC();
            return *count != 0U;
        }
        delay_cycles(977U);
    }
    return false;
}

static bool board_clock_pll_ratio_valid(void)
{
    uint32_t pll_count, reference_count;
    uint64_t measured, expected;
    /* SYSPLL_ERR_01: verify lock against its HFCLK reference, as in the
     * SDK's generated workaround. LFOSC is only the common gate time. */
    DL_SYSCTL_setFCCPeriods(DL_SYSCTL_FCC_TRIG_CNT_01);
    if (!board_clock_fcc_count(DL_SYSCTL_FCC_CLOCK_SOURCE_SYSPLLCLK2X,
                               &pll_count) ||
        !board_clock_fcc_count(DL_SYSCTL_FCC_CLOCK_SOURCE_HFCLK,
                               &reference_count)) {
        return false;
    }
    measured = (uint64_t)pll_count * ACQ_HFXT_FREQ_HZ * 1000U;
    expected = (uint64_t)reference_count * CPUCLK_FREQ;
    return measured > expected * 997U && measured < expected * 1003U;
}

static bool board_clock_init(void)
{
    static const DL_ADC12_ClockConfig adc_clock = {
        .clockSel = DL_ADC12_CLOCK_HFCLK,
        .divideRatio = DL_ADC12_CLOCK_DIVIDE_2,
        .freqRange = BOARD_ADC_CLOCK_RANGE,
    };
    uint32_t instruction_config;
    uint32_t attempt;

    /* PA5/HFXIN and PA6/HFXOUT are dedicated to Y2. Analog mode disconnects
     * the digital pad. SYSOSC remains on as required for PLL operation. */
    DL_GPIO_initPeripheralAnalogFunction(IOMUX_PINCM10);
    DL_GPIO_initPeripheralAnalogFunction(IOMUX_PINCM11);
    DL_SYSCTL_setHFCLKSourceHFXTParams(DL_SYSCTL_HFXT_RANGE_32_48_MHZ,
                                      10U, false);
    DL_SYSCTL_enableHFCLKStartupMonitor();
    if (!board_clock_wait(DL_SYSCTL_CLK_STATUS_HFCLK_GOOD,
                           DL_SYSCTL_CLK_STATUS_HFCLK_GOOD)) {
        DL_SYSCTL_disableHFXT();
        return false;
    }

    /* Same register sequence and factory tuning as DL_SYSCTL_configSYSPLL,
     * with bounded waits so an absent crystal or failed PLL cannot hang boot.
     * PDIV=1, QDIV=2 (encoded 1); VCO is 80/96 MHz, within 80..160 MHz. */
    DL_SYSCTL_disableSYSPLL();
    if (!board_clock_wait(DL_SYSCTL_CLK_STATUS_SYSPLL_OFF,
                           DL_SYSCTL_CLK_STATUS_SYSPLL_OFF)) {
        DL_SYSCTL_disableHFXT();
        return false;
    }
    DL_Common_updateReg(&SYSCTL->SOCLOCK.SYSPLLCFG0,
        DL_SYSCTL_SYSPLL_REF_HFCLK, SYSCTL_SYSPLLCFG0_SYSPLLREF_MASK);
    DL_Common_updateReg(&SYSCTL->SOCLOCK.SYSPLLCFG1,
        DL_SYSCTL_SYSPLL_PDIV_1 | (1U << SYSCTL_SYSPLLCFG1_QDIV_OFS),
        SYSCTL_SYSPLLCFG1_PDIV_MASK | SYSCTL_SYSPLLCFG1_QDIV_MASK);

    instruction_config = DL_CORE_getInstructionConfig();
    DL_CORE_configInstruction(DL_CORE_PREFETCH_ENABLED, DL_CORE_CACHE_DISABLED,
                              DL_CORE_LITERAL_CACHE_ENABLED);
    SYSCTL->SOCLOCK.SYSPLLPARAM0 = *(volatile const uint32_t *)
        DL_SYSCTL_SYSPLL_INPUT_FREQ_32_48_MHZ;
    SYSCTL->SOCLOCK.SYSPLLPARAM1 = *(volatile const uint32_t *)
        (DL_SYSCTL_SYSPLL_INPUT_FREQ_32_48_MHZ + 4U);
    CPUSS->CTL = instruction_config;

    DL_Common_updateReg(&SYSCTL->SOCLOCK.SYSPLLCFG0,
        (BOARD_PLL_CLK2X_DIV << SYSCTL_SYSPLLCFG0_RDIVCLK2X_OFS) |
            DL_SYSCTL_SYSPLL_CLK2X_ENABLE | DL_SYSCTL_SYSPLL_MCLK_CLK2X,
        SYSCTL_SYSPLLCFG0_RDIVCLK2X_MASK | SYSCTL_SYSPLLCFG0_RDIVCLK1_MASK |
            SYSCTL_SYSPLLCFG0_RDIVCLK0_MASK | SYSCTL_SYSPLLCFG0_ENABLECLK2X_MASK |
            SYSCTL_SYSPLLCFG0_ENABLECLK1_MASK | SYSCTL_SYSPLLCFG0_ENABLECLK0_MASK |
            SYSCTL_SYSPLLCFG0_MCLK2XVCO_MASK);

    for (attempt = 0U; attempt < 3U; ++attempt) {
        DL_SYSCTL_enableSYSPLL();
        if (board_clock_wait(DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD,
                              DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD) &&
            board_clock_pll_ratio_valid()) {
            /* Select and validate HSCLK while the CPU still uses SYSOSC. */
            DL_SYSCTL_setHSCLKSource(DL_SYSCTL_HSCLK_SOURCE_SYSPLL);
            if (!board_clock_wait(DL_SYSCTL_CLK_STATUS_HSCLK_GOOD,
                                   DL_SYSCTL_CLK_STATUS_HSCLK_GOOD)) {
                break;
            }
            DL_SYSCTL_setMCLKSource(SYSOSC, HSCLK, DL_SYSCTL_HSCLK_SOURCE_SYSPLL);
            /* Conversions are disabled by the caller; use the same crystal
             * for ADC conversion/hold timing as for the sample trigger. */
            DL_ADC12_setClockConfig(ADC12_0_INST,
                                    (DL_ADC12_ClockConfig *)&adc_clock);
            return true;
        }
        DL_SYSCTL_disableSYSPLL();
        if (!board_clock_wait(DL_SYSCTL_CLK_STATUS_SYSPLL_OFF,
                               DL_SYSCTL_CLK_STATUS_SYSPLL_OFF)) {
            break;
        }
    }
    DL_SYSCTL_disableSYSPLL();
    DL_SYSCTL_disableHFXT();
    return false;
}

#endif
