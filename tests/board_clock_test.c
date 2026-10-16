/* Native regression for the boot gate after SysConfig initialization.
 * A locked PLL alone must not allow capture with SYSOSC as its reference. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#define CPUCLK_FREQ 32000000U
#define ACQ_HFXT_FREQ_HZ 40000000UL
#define DL_SYSCTL_CLK_STATUS_HFCLK_GOOD 1U
#define DL_SYSCTL_CLK_STATUS_SYSPLL_GOOD 2U
#define DL_SYSCTL_CLK_STATUS_HSCLK_GOOD 4U
#define DL_SYSCTL_MCLK_SOURCE_HSCLK 1U
#define DL_SYSCTL_HSCLK_SOURCE_SYSPLL 1U
#define SYSCTL_SYSPLLCFG0_SYSPLLREF_MASK 1U
#define DL_SYSCTL_SYSPLL_REF_HFCLK 1U

static struct { struct { uint32_t SYSPLLCFG0; } SOCLOCK; } fake_sysctl;
#define SYSCTL (&fake_sysctl)
static uint32_t status, mclk_source, hsclk_source;
static uint32_t DL_SYSCTL_getClockStatus(void) { return status; }
static uint32_t DL_SYSCTL_getMCLKSource(void) { return mclk_source; }
static uint32_t DL_SYSCTL_getHSCLKSource(void) { return hsclk_source; }

#include "board_clock.h"

int main(void)
{
    unsigned cases = 0U;
    for (status = 0; status < 8; ++status) {
        for (mclk_source = 0; mclk_source < 2; ++mclk_source) {
            for (hsclk_source = 0; hsclk_source < 2; ++hsclk_source) {
                for (uint32_t ref = 0; ref < 2; ++ref) {
                    fake_sysctl.SOCLOCK.SYSPLLCFG0 = ref;
                    assert(board_clock_is_ready() ==
                           (status == 7 && mclk_source == 1 &&
                            hsclk_source == 1 && ref == 1));
                    ++cases;
                }
            }
        }
    }
    printf("External clock boot gate: %u source/lock combinations passed\n", cases);
    return 0;
}
