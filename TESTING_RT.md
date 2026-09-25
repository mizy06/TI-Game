# 三级级联 RT 自激 200+96 题测试方法

## 1. 烧录前

1. 使用 CCS 与 MSPM0 SDK 2.11.00.07 打开工程；
2. 确认 CCS 在 **Project → Properties → General → Dependencies** 中识别
   MSPM0 SDK 2.11.00.07 和 SysConfig 1.28.1；
3. 执行 **Project > Clean**、**Build Project**，只在编译成功后烧录新生成的固件；
4. 用一根杜邦线连接 **PA15（DAC 输出）→ PA25（ADC 输入）**；
5. 打开 XDS110 Application/User UART：**115200、8-N-1、无流控**。

自激连接不要串 1 kΩ，也不要在 PA25 对地加电容。上电横幅必须包含：

```text
FW=cascade-v2.2,API=0x00020100,CPU_HZ=80000000,DEFAULT_ALGORITHM=4,...
```

若仍显示 `algorithm-v2`、`0x00020000`、`CPU_HZ=32000000` 或 `cases=12`，
说明烧录的还是旧构建产物，请 Clean 后重新构建和烧录。

## 2. RT 命令

| 命令 | 行为 | 单套题数 |
| --- | --- | ---: |
| `RT1` | 算法 1：`legacy_amdf_crossing` | 200+96 |
| `RT2` | 算法 2：`global_harmonic_model` | 200+96 |
| `RT3` | 历史算法 3：`fast_phase_model`，现为 L1 | 200+96 |
| `RT4` | 正式三级级联 L1→L2→按需 L3 | 200+96 |
| `RT` | 一键先跑算法 1，再跑算法 2 | 296×2 |

让串口工具一次发送整条命令，可带回车换行，不要缓慢逐字键入。单算法正常
启动示例：

```text
RT-START,algorithm=2,name=global_harmonic_model,cases=296,custom=200,challenge=96,fs_hz=20000,n=4096,dac=PA15,adc=PA25,dump=0
```

`RT` 双算法模式还会先输出：

```text
RT-SUITE-START,algorithms=2,cases_per_algorithm=296,total_runs=592
```

随后每题依次输出 `RT-CASE`、`CAPTURING`、`PROCESSING`、`RESULT` 和
`RT-CHECK`。`RT4` 还输出 `CASCADE`，包含三级状态、类别与是否运行 L3。
`RT4` 不自动重测，逐题展示单次真实结果与耗时。历史 `RT1`～`RT3` 失败时会反复重新驱动 DAC、采 ADC 并分析，直到通过或本题从
第一次采样起累计达到 13 s；每次重测前输出 `RT-RETRY,attempt=...,elapsed_ms=...`，
最后只输出一次正式 `RESULT` / `RT-CHECK`，并输出
`RT-TIMING,attempts=...,elapsed_ms=...,budget_ms=13000`。本题超时仍为 FAIL，
之后继续下一题。运行中不要再向串口发送字符，也不要按 PB13；任一操作都会安全
中止矩阵。

本次修正保留 296 题及小幅度、直流位置边界题。过去 20、82 个整周期分别对应
97.656、400.391 Hz，落在赛题 100–400 Hz 范围之外；现在用定时器驱动的
DAC 分数相位步进准确生成 100、400 Hz。MSPM0G3507 的 DAC 线性度只在
0.3 V 到 VDDA-0.3 V 内有规格保证；能容纳的题目因此在回放时自动保留该余量，
而不是把输出缓冲的近电源轨压缩当成识别算法 Vpp 误差。每条 `RT-CASE` 的
`center_code` 和 `linear_guard` 会说明实际中心码及本题是否完整受此保护；
过大的“信号+噪声”包络若无法放入 2.7 V 线性窗口，会回到全量程内居中播放。

13 s 是历史 **RT1～RT3 诊断矩阵**的单题重测窗口，不是正式初赛识别时限。手册规定初赛
10 s、每题允许重测一次；决赛 15 s，较强干扰样例允许重测一次。正式比赛的
`R` 命令不会用已知答案自动判错；其 `TIMING` 含本次采集、三级分析和串口结果输出，需检查所有题都低于 10000 ms。

每套结束时输出：

```text
RT-DONE,algorithm=2,name=global_harmonic_model,pass=...,fail=...,custom_pass=...,custom_fail=...,challenge_pass=...,challenge_fail=...
```

双算法全部结束后还会输出：

```text
RT-SUITE-DONE,completed_algorithms=2,aborted=0
READY
```

`custom_fail=0` 表示 200 道自编门禁题全部通过。challenge 是额外压力题，
应分别比较两套算法的 `challenge_pass/challenge_fail`。

## 3. 自动保存单算法结果（可选）

关闭其他占用串口的软件后运行：

```bash
python -m pip install pyserial
python tools/rt_self_test.py --port COM15 --algorithm 1
python tools/rt_self_test.py --port COM15 --algorithm 2
python tools/rt_self_test.py --port COM15 --algorithm 3
python tools/rt_self_test.py --port COM15 --algorithm 4
```

把 `COM15` 换成实际端口。脚本故意一次只采一套矩阵，分别保存逐题 JSONL、
元数据和汇总，避免把两种算法的记录混在同一文件中。直接在串口发送 `RT`
仍可一键完成两套现场测试。

## 4. 80 MHz 与外设时序

MSPM0G3507 的额定最高 CPU 频率为 80 MHz。本工程通过 SYSPLL 运行 MCLK，
启用 SDK 的 PLL/FCC 有效性检查，并将 ULPCLK 除以 2 保持在 40 MHz。SysConfig
会据此重新生成 Flash wait-state、20 kS/s TIMER 装载值和 UART 115200 分频；
DAC 的 MFPCLK 继续取 32 MHz SYSOSC。不要手改生成目录中的
`ti_msp_dl_config.c/.h`，应从 `empty.syscfg` 重新生成。

## 5. 查看失败帧

296 题默认只回传文本。需要检查最近一题的 4096 个 ADC 样点时，先中止或
等待矩阵结束，再发送：

```text
D
```

固件会输出 `SAMPLES,bytes=8192,fnv1a=...`，随后发送 8192 字节小端样点。
