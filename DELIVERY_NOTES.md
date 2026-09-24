# 本次交付说明

本源码包在原 200 道自编题 + 96 道 challenge 自激题集上完成了双算法与
80 MHz 调频：

- `RT1`：`legacy_amdf_crossing` 跑 296 题；
- `RT2`：`global_harmonic_model` 跑 296 题；
- `RT3`：`fast_phase_model` 跑 296 题；单帧 `R/W/T` 默认用算法 3；
- `RT`：一键依次跑完两套，共 592 次“采集 + 分析 + 判定”；
- MSPM0G3507 MCLK 使用额定最高 80 MHz；ULPCLK、Flash wait-state、UART、
  TIMER、ADC/DAC 相关时钟由 SysConfig 同步配置；
- 固件接口升级到 v2.1（`0x00020100`）。

烧录后将 PA15（DAC）短接到 PA25（ADC），在 115200 8-N-1 串口发送上述
命令。新固件启动横幅必须包含：

```text
FW=algorithm-v2.1,API=0x00020100,CPU_HZ=80000000,DEFAULT_ALGORITHM=3
```

本包不附带旧 `Debug/Release`、`.out`、DLL/EXE、历史 RT 结果或串口日志，
必须在 CCS 中 Clean、Build 并烧录。完整步骤见 [TESTING_RT.md](TESTING_RT.md)。

## 离线验证

- C99 严格编译：`-Wall -Wextra -Werror`；
- 基础随机回归：2400/2400；
- 强噪声回归：2400/2400；
- 算法 1：自编 200/200，challenge 95/96；
- 算法 2：自编 200/200，challenge 96/96；
- 算法 3：自编 200/200，challenge 96/96；详细速度与精度见 docs/FAST_ALGORITHM_REPORT.md；
- 协议、题数、双算法命令与 80 MHz 静态契约单元测试全部通过。

这些是主机数字模型结果，不替代目标板构建、PA15→PA25 模拟环路实测、
真实信号源校准和板上最坏处理时间测量。
