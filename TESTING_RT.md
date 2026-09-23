# 双算法 RT 自激 200+96 题测试方法

## 1. 烧录前

1. 使用 CCS 与 MSPM0 SDK 2.11.00.07 打开工程；
2. 执行 **Project > Clean**、**Build Project**，再烧录新生成的固件；
3. 用一根杜邦线连接 **PA15（DAC 输出）→ PA25（ADC 输入）**；
4. 打开 XDS110 Application/User UART：**115200、8-N-1、无流控**。

自激连接不要串 1 kΩ，也不要在 PA25 对地加电容。上电横幅必须包含：

```text
FW=algorithm-v2.1,API=0x00020100,CPU_HZ=80000000,DEFAULT_ALGORITHM=2,...
```

若仍显示 `algorithm-v2`、`0x00020000`、`CPU_HZ=32000000` 或 `cases=12`，
说明烧录的还是旧构建产物，请 Clean 后重新构建和烧录。

## 2. 三种 RT 命令

| 命令 | 行为 | 单套题数 |
| --- | --- | ---: |
| `RT1` | 算法 1：`legacy_amdf_crossing` | 200+96 |
| `RT2` | 算法 2：`global_harmonic_model` | 200+96 |
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
`RT-CHECK`。运行中不要再向串口发送字符，也不要按 PB13；任一操作都会安全
中止矩阵。

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
python tools/rt_self_test.py --port COM15 --algorithm 1 --timeout 600
python tools/rt_self_test.py --port COM15 --algorithm 2 --timeout 600
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
