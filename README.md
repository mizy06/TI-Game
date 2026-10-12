# TI 杯决赛工程 3.0.0

MSPM0G3507 只做定时 ADC、DMA 和 UART 传输；电脑完成分类、校准和参数计算。默认 **32 MHz SYSOSC、20 kS/s、8192 点、115200 8-N-1**。新固件使用 DUMP v3，上位机兼容原 DUMP v2 / 4096 点；C 分类 ABI 仍为 2.1.2。R / RO / RT、原 296 项矩阵和 RESULT 五字段格式均保留。

固件初始化在停止定时器、关闭 ADC 转换后将 SCOMP0/1 设为 32，再启用转换，增加输入建立裕量；SysConfig 的时钟和采样触发保持不变。烧录后须重新 RO 标定。

本版消除了测量路径的二次 ADC 取整及结果过早取整，增加连续记录、双精度联合拟合、双边沿占空比、连续周期峰值和多帧 R。**更多小数位不等于真实准确度；当前实机数据没有证明 1 ppm 或 0.1 ppm。** 精度分析、接口、存档、测试和下一步硬件方案已合并到 [工程说明](docs/ENGINEERING.md)。

## 快速使用

1. 解压后，在 CCS 中导入本目录。项目名为 `TI-game-finals`，可与原初赛项目并存。原 MSPM0 SDK 2.11.00.07、SysConfig 1.28.1 和两个 CCS wrapper 保留。
2. Clean / Build / Flash；确认生成的 `CPUCLK_FREQ=32000000`。固件对此有编译检查。缓冲区为 16 KiB，需在实际 TI 链接 map 中确认 SRAM 余量。修改 SysConfig 后必须 Clean；不要烧录原初赛的旧 `.out`。
3. 发生器接 PA25（ADC0.CH2），共地；UART 为 PA10/PA11，经 XDS110。Hi-Z 负载配置，输入全程 0–3.3 V；关闭占用串口的 CCS 终端。
4. 使用 64 位 Python 3.10+：

```powershell
python -m pip install -r requirements.txt
python server.py --port COM6
```

把 COM6 换成实际端口，Linux 可用 `/dev/ttyACM0`。在终端键入命令并回车：

| 命令 | 行为 |
| --- | --- |
| `R` | 默认采集并独立拟合 4 帧，检查稳定性，平均参数，最终只输出一条 RESULT |
| `RO` | 独立参考校准 ADC / 时基和发生器 ARB，再按原 296 项矩阵回放、实采、判定 |
| `RT` | 无硬件生成原 296 项 ADC 输入，运行相同单帧算法，逐项判定并完整存档 |
| `Q` | 退出 |

也支持直接命令：

```powershell
python server.py R --port COM6
python server.py R --port COM6 --r-frames 1
python server.py RO --port COM6
python server.py RT
```

`--r-frames` 支持 1–8，默认 4；`--r-timeout` 默认 15 s，不能超过 15 s，涵盖 R 的采集、拟合和存档。帧之间有 UART 间隙，**分别拟合，绝不拼成连续采样**。改变类别、频率跨度 >0.2%、幅值跨度 >10% 或方波 duty 跨度 >1 个百分点时拒绝这组输出。默认模式有助于随机噪声平均，但循环回放噪声或系统误差不保证按 √帧数下降。

## 输出与校准

stdout 保持以下格式；Hz、V 使用 9 位小数，方波占空比 % 使用 7 位小数，其他类别固定为 `0`。无单位、无科学计数法、无 DC 列。日志写 stderr / `server.log`。

```text
RESULT,sine,237.192998602,1.499988635,0
RESULT,square,237.192998314,1.499680020,63.0000285
```

示例仅说明格式。`algorithm_parameters.json` 保留上传工程 2026-10-09 的成功 RO 参数，匹配原板卡和原前端。它是发生器相对校准，不是 ppm 级可溯源标定；本次没有连接你的板卡或重新标定。**烧录本版后，应先跑一次 RO 重新确认时基与幅值。** 换板、换供电或换前端也应重新校准。无校准诊断可指定 `--algorithm-parameters algorithm_parameters.uncalibrated.json`。

原工程已经配置默认 32 MHz；没有发现切换至 80 MHz 的代码。旧采样率 20244.764 Hz 与标称 20000 Hz 的差异足以解释未校准时约 −1.21% 的频率偏差。32 MHz 本身并不能去除内部振荡器误差，详细证据见工程说明。

RO 默认地址仍为 `10.11.13.220:111`，VXI-11 `inst0` / CH1。比赛 R 不接信号源控制。无串口时 RO 为 encode-only / ENCODED，不能当作实测通过。`--ro-calibrate off` 是本轮诊断，不覆盖已有参数。RO 所有校准参考均独立于矩阵，绝不使用题号或测量真值修正答案。

## 验证与目录

```powershell
python server.py RT
python -m unittest discover -s tests -p "test*.py"
python tools/precision_validation.py
python tools/reanalyze_evidence.py evidence/original_ro.zip
```

Linux/macOS 的完整软件检查为 `bash tests/run_all_checks.sh`；Windows 如需重建 C 库，安装 LLVM/Clang + Windows C 运行库开发环境或 MinGW GCC。包内 Windows DLL 沿用原工程未改动的 C 核心；启动时仍检查 ABI，不符时自动重建。没有附带新的实板固件二进制。

| 路径 | 内容 |
| --- | --- |
| 根目录、`.settings/`、`targetConfigs/` | CCS 工程、采集固件、C 分类源码、校准参数 |
| `tools/` | 上位机、精度拟合、RO/RT、数据与构建工具 |
| `tests/`、`examples/` | C/Python 回归与调用示例 |
| `docs/ENGINEERING.md` | 合并后的当前接口、精度分析和验收说明 |
| `docs/competition_manual.pdf` | 上传赛事手册的原始副本 |
| `validation/` | 本版真实软件验证、原版同输入比较、历史实采复算和 ppm 实验 |
| `evidence/original_ro.zip` | 原完整 296 项实机 RO 的原始数据，保持不变 |

运行数据自动保存在 `data/<会话>/`，可用 `--data-dir` 指向工程外。压缩包不含 `.git`、缓存、旧日志、重复历史说明或临时构建目录。**软件检查通过不替代实际 CCS 编译、烧录、DMA 连续性和端到端 15 s 验收。**
