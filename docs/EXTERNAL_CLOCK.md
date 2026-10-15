# 板载外部晶振配置

2026-10-15：单片机主时钟和 ADC 时钟改用板载 Y2 晶振。

## 确认晶振频率

TI 的 LP-MSPM0G3507 用户指南 SLAU873E §2.7、原理图和物料表均标注 **Y2 = 40 MHz**，连接 PA5/HFXIN、PA6/HFXOUT。原装板采用 40 MHz 配置。48 MHz 是芯片支持的 HFXT 上限，不能据此认定板载晶振是 48 MHz。

如果实物确实更换为 48 MHz 晶振，把 `acquisition_config.h` 的 `ACQ_HFXT_FREQ_HZ` 改为 `48000000UL` 再构建。参数必须与实物匹配；启动横幅显示的是软件配置，不能自动识别晶振频率。两种配置都经过 SysConfig 时钟计算核对。

## 运行中的时钟路径

| 项目 | 原装 40 MHz 板卡 | 更换为 48 MHz 晶振的板卡 |
| --- | --- | --- |
| HFCLK | Y2，40 MHz | Y2，48 MHz |
| PLL PDIV / QDIV | /1、×2 | /1、×2 |
| PLL VCO | 80 MHz | 96 MHz |
| SYSPLLCLK2X | VCO ×2 /5 = 32 MHz | VCO ×2 /6 = 32 MHz |
| MCLK / BUSCLK / CPUCLK | 32 MHz | 32 MHz |
| TIMA0 | BUSCLK，/1，prescale=0，LOAD=1599 | 同左 |
| ADC 触发率 | 32000000 /1600 = 20000 Hz | 同左 |
| ADC 转换时钟 | HFCLK /2 = 20 MHz | HFCLK /2 = 24 MHz |
| UART0 | BUSCLK，115200，8N1 | 同左 |

SYSOSC 按芯片 PLL 的要求继续保持开启，但采样定时器和 ADC 转换时钟均已使用外部晶振作为参考。4096 点分类窗口、8192 点采集长度、16 KiB ADC 缓冲区和 DUMP v3 协议保持原配置。外部晶振本身不等于经过 ppm 精度标定，固件不会自动置“已校准”标志。

`empty.syscfg` 生成 32 MHz 外设初始化。`main()` 随后停止 TIMA0、关闭 ADC 转换，调用 `board_clock.h` 中的启动函数：将 PA5/PA6 配成晶振模拟引脚，启动 HFXT，配置并验证 PLL，切换 MCLK，然后切换 ADC 时钟。这样重新生成 SysConfig 文件不会覆盖外部时钟代码，也无需手工修改工具管理的 `.syscfg` 或生成文件。检查运行状态时必须同时检查此启动函数。

代码使用 SDK 的寄存器配置顺序和工厂 PLL 调谐值，包含 SYSPLL_ERR_01 的 FCC 比例校验；错误锁定最多重试三次。晶振、PLL、FCC 等启动检查使用有限等待。切换前任一检查失败，程序保持内部启动时钟供串口报错，并禁止采集，不会把内部时钟采样伪装成外部时钟采样。

## 烧录与核验

1. 在 CCS 构建当前 `D:\github\TI-Game` 工程，烧录 `D:\github\TI-Game\Debug\TI-game.out`。
2. 单片机复位前关闭占用串口的上位机。串口设为 115200、8N1，复位并运行程序。正常启动信息包括：

   ```text
   TI-CUP ADC DMA HOST V3
   HFXT=40MHz, PLL=32MHz, CLOCK=EXTERNAL
   ADC_CLOCK=HFCLK/2; recalibrate host with RO after clock change
   ADC=PA25, UART=PA10/PA11-XDS110, FS=20000Hz, N=8192, MCLK=32MHz, SCOMP=32
   ```

3. 若看到 `ERR_EXTERNAL_CLOCK: check Y2 / PA5 / PA6; capture disabled`，检查 Y2 是否焊接、频率是否正确、PA5/PA6 是否被其他电路占用。原装板 Y2 已连接，无需添加时钟输入线；不要向晶振引脚直接接入信号发生器。
4. 在 CCS 暂停到 main 初始化完成后，可核对 `SYSCTL->SOCLOCK.CLKSTATUS` 的 HFCLKGOOD、SYSPLLGOOD、HSCLKGOOD 及 MCLK/HSCLK 选择状态。TIMA0 的 LOAD 应为 1599，ADC 的 SAMPCLK 应选择 HFCLK。启动横幅证明配置和启动检查完成，独立精度仍需要实测。
5. 关闭串口工具，在本工程根目录运行 `python server.py --port COM实际编号`。先用 R 验证收帧，再执行 RO 重新校准和验证。采样源变化后，旧校准不能继续使用。

## 校准迁移

旧 `algorithm_parameters.json` 已保留为 `algorithm_parameters.sysosc-backup.json`，当前默认参数文件置为未校准。旧参数测得约 20244.764 Hz，套在外部时钟采样上会引入约 1.2% 的频率误差。上位机进程若仍在运行，应退出并重新启动，避免沿用内存中的旧参数。

成功执行 RO 后，上位机会按现有机制写入新参数。换回内部时钟固件时才考虑恢复旧参数，并重新验证。其他工作树（例如 `D:\github\TI-Game-latest`）有独立的程序和参数，不会自动随本工程修改；本固件应搭配本工程的上位机进行首次校准。

## 软件验证与实板边界

- TI SysConfig 1.28.1、MSPM0 SDK 2.11.00.07 对 MSPM0G3507 的 40/48 MHz → 32 MHz 时钟组合进行验证。
- TI Arm Clang 5.1.1.LTS 构建和链接当前 40 MHz 固件，核对 TIMA0/UART 配置及 SRAM map。
- 本次默认固件 map：Flash 10872 / 131072 字节，SRAM 17090 / 32768 字节（包含 512 字节栈预留），未增加 ADC 缓冲区。48 MHz 分支也通过 TI 编译器的 `-Wall -Wextra -Werror` 编译。
- `tests/board_clock_test.c` 使用模拟寄存器和故障注入验证 40/48 MHz 的正常启动、首次错误锁定后重试，以及缺晶振、PLL 不锁定、比例错误、FCC 超时、FCC 零计数、HSCLK 故障、PLL 不能关闭七种失败路径；失败时不切 MCLK、不启用外部 ADC 配置。

本次软件验证不包含对实体开发板的烧录、示波器时基测量或 RO 实机校准。需要按上面的烧录步骤在手中的板卡完成。

本机复现故障注入测试（PowerShell，需安装 LLVM；以下为 40 MHz，改宏值即可测试 48 MHz）：

```powershell
& 'C:\Program Files\LLVM\bin\clang.exe' -std=c11 -Wall -Wextra -Werror -I . -DACQ_HFXT_FREQ_HZ=40000000UL tests\board_clock_test.c -o Debug\board_clock_test40.exe
& .\Debug\board_clock_test40.exe
```

资料：[TI LP-MSPM0G3507 用户指南](https://www.ti.com/lit/ug/slau873a/slau873a.pdf)，SLAU873E，§2.7、原理图及 Y2 物料表。
