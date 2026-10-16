# 外部晶振固定时钟配置与烧录验证

2026-10-16：在 main 分支同步此前外部时钟工作，并将时钟配置统一交给 SysConfig。

## 硬件与时钟路径

原装 LP-MSPM0G3507 的 Y2 是 **40 MHz**，连接 PA5/HFXIN、PA6/HFXOUT（TI 用户指南 SLAU873，§2.7、原理图及物料表）。48 MHz 是 HFXT 支持的上限，不代表原装板晶振频率。Y2 已连接，无需加线；PA5/PA6 不得再接其他信号。

| 项目 | 当前配置 |
| --- | --- |
| HFCLK | HFXT，40 MHz |
| SYSPLL 参考 | HFCLK，不使用 SYSOSC |
| PLL PDIV / QDIV | /1、×2，VCO=80 MHz |
| SYSPLLCLK2X | VCO×2 /5 = 32 MHz（rDivClk2x 编码为 4） |
| MCLK / BUSCLK / CPUCLK | 32 MHz |
| TIMA0 | BUSCLK /1，prescale=0，LOAD=1599 |
| ADC 触发率 | 32000000 /1600 = 20000 Hz |
| ADC 转换时钟 | HFCLK /2 = 20 MHz |
| UART0 | BUSCLK，115200，8N1 |

`empty.syscfg` 直接配置 HFXT → SYSPLL → MCLK，启用 HFXT 启动监视及 SYSPLL_ERR_01 错误锁定检查；FCC 仅在启动时用于 PLL 比例校验。SysConfig 重新生成后，外部晶振路径仍保留。`board_clock.h` 只在启动后核验锁定状态、MCLK/HSCLK 选择及 PLL 外部参考，不再修改时钟。R、D 和采集循环都不进行时钟切换或 SYSOSC trim。

SYSOSC 按芯片 PLL 电路要求保持开启，但采样定时器和 ADC 均不使用它作为参考。配置显式设置 `enableSYSOSCFCL=false`，不主动开启 FCL。SDK 说明 FCL 一旦开启不能直接用软件关闭，必须 BOOTRST；旧调试状态可能保留 FCL。故启动横幅报告实际 FCL 状态，而不把配置值当作寄存器实测值。即使显示保留的 FCL=ON，当前采样路径仍是 HFXT 固定参考。

4096 点分类窗口、8192 点采集长度、16 KiB ADC 缓冲区及 DUMP v3 协议保持原配置。若实物改装了 48 MHz 晶振，必须同时修改 SysConfig 的 HFXT 频率、PLL 分频、`acquisition_config.h` 和启动说明，再生成并验证；不能只改一个宏。

## 烧录与操作

1. CCS 构建当前工程，烧录 `D:\github\TI-Game\Debug\TI-game.out`。Git 不跟踪 Debug 产物；队友拉取 main 后需在自己的 CCS 中重新构建。
2. 烧录后关闭调试会话，断开所有板卡供电（USB 和任何外部供电），再接通 USB，以清除旧 FCL 状态。无需重新插网线。
3. 关闭占用串口的上位机，串口设为 115200、8N1，复位并运行。正常横幅包含：

   ```text
   TI-CUP ADC DMA HOST V3
   HFXT=40MHz, PLL=32MHz, CLOCK=EXTERNAL
   SYSOSC_FCL=OFF; SAMPLE_CLOCK=HFXT_FIXED
   ADC_CLOCK=HFCLK/2; recalibrate host with RO after clock change
   ADC=PA25, UART=PA10/PA11-XDS110, FS=20000Hz, N=8192, MCLK=32MHz, SCOMP=32
   ```

4. 如果没有横幅，用 CCS 暂停查看是否停在 HFXT/PLL 初始化等待中。TI 生成的驱动在晶振不能稳定或 PLL 反复错误锁定时可能一直等待；本版不再采用此前手动切换函数的有限重试。若已进入应用但时钟核验失败，则输出 `ERR_EXTERNAL_CLOCK` 并禁止采集。检查 Y2、PA5/PA6 和供电；不要把缺横幅直接当成串口故障。
5. 初始化完成后，可在 CCS 核对 HFCLKGOOD、SYSPLLGOOD、HSCLKGOOD；MCLK 应选择 HSCLK，HSCLK 应选择 SYSPLL，SYSPLLREF 应选择 HFCLK。TIMA0 LOAD=1599，ADC SAMPCLK=HFCLK、/2。启动信息是软件核验，不替代独立时基测量。
6. 关闭串口工具，在工程根目录运行 `python server.py --port COM实际编号`。先发送 R 确认正常收帧，再执行 **RO 重新校准**，最后执行 RT 比较频率误差及分布。算法和识别仍在上位机运行。

## 校准迁移

rebase 后远端 main 的内部时钟校准（2026-10-15，约 20245.505423 Hz）已备份为 `algorithm_parameters.sysosc-backup.json`。当前 `algorithm_parameters.json` 清空采集校准，避免把内部时钟约 1.2% 的偏差补偿套到外部时钟上。

退出并重新启动上位机，防止沿用内存中的旧校准。RO 成功后按现有机制保存新参数。旧备份只能用于对应的内部时钟固件并重新验证，不能用于本版 HFXT 固件。

外部参考消除了 SYSOSC/FCL 对采样时基的直接依赖，但晶振温漂、初始误差、信号源参考、ADC 和模拟前端误差仍需实测。本修改未宣称达到固定 ppm 精度，也未用软件测试证明图中的全部误差必然来自 FCL。

## 软件验证记录

- TI SysConfig 1.28.1 / MSPM0 SDK 2.11.00.07 生成通过，核对 PA5/PA6、PLL 外部参考、20 kS/s 定时器及 HFCLK ADC 配置。
- TI Arm Clang 5.1.1.LTS 编译、链接通过；map 为 Flash **9280 / 131072 B**，SRAM **17090 / 32768 B**（包括 512 B 栈预留）。
- `tests/board_clock_test.c` 覆盖 64 种时钟状态/源组合，检查未锁定、MCLK/HSCLK 选错或 PLL 使用 SYSOSC 参考时禁止采集。此前手动切换方案的重试测试已随实现更新。
- 重建上位机 DLL 后，串口收帧集成测试 4 项、采集校准回归测试 5 项通过。
- 软件验证不包含实体板烧录、示波器时基测量或 RO 实机校准。

本机复现启动门控测试（PowerShell，工程根目录）：

```powershell
& 'C:\Program Files\LLVM\bin\clang.exe' -std=c11 -Wall -Wextra -Werror -I . tests\board_clock_test.c -o Debug\board_clock_test.exe
& .\Debug\board_clock_test.exe
```

参考：[TI LP-MSPM0G3507 用户指南](https://www.ti.com/lit/ug/slau873a/slau873a.pdf)；SDK `dl_sysctl_mspm0g1x0x_g3x0x.h` 中 FCL 的 BOOTRST 说明。
