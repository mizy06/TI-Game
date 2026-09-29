# 2026 TI 杯赛道一：高精度波形识别算法分支

交付内容：新增低计算量算法 3，保留算法 1/2 与 RT 自激 200+96 题集
算法接口版本：2.1（0x00020100）

本工程面向 MSPM0G3507，处理采集侧交付的无符号 ADC 数组，识别正弦波、方波、三角波和下降锯齿波，并输出频率、Vpp；方波额外输出占空比。

第一次接触单片机或信号处理，请先读 [波形识别工程零基础导读](BEGINNER_GUIDE.md)。它从采样点、ADC 码值、频率和峰峰值讲起，再按实际调用顺序解释全部算法步骤、定点数单位、串口协议、测试方法和文件地图；源码中也已按同样思路补充逐函数注释。

## 已完成

- 新增版本化采集-算法接口 waveform_frame_t；
- 用 AMDF 基波搜索与滞回交越回归联合估计频率；
- 修复原 1-2-1 FIR 的原地覆写问题；
- 用原始 ADC 点做 128 相位多周期截尾折叠；
- 对全部循环相位做生成模型拟合；
- 方波模型联合搜索占空比和独立 0–10% 上升/下降边沿；
- 按最佳类别模型做仿射最小二乘 Vpp；
- 频率、参考电压和前端增益均由采集元数据传入；
- 保留旧 waveform_analyze 兼容入口；
- 新增 `waveform_analyze_frame_with_algorithm()`，运行时选择两条算法路径；
- 算法 2 在原 AMDF/穿越路径拒识时增加倍频全局模型消歧；
- 算法 3 对 AMDF 隔 8 点采样；相关模板每组只生成一次、每组只做一次除法，正常帧免去第二次折叠；
- MSPM0G3507 调至额定最高 80 MHz，并同步重算外设时序；
- 串口 RESULT 增加高精度整数结果、算法版本、帧号和质量标志；
- 培训 PDF、算法研读和对接协议已归档在 docs。

## 文档

- [零基础导读：从 ADC 采样到四类波形识别](BEGINNER_GUIDE.md)
- [算法培训研读、替代方案与最终取舍](docs/ALGORITHM_RESEARCH_AND_DECISIONS.md)
- [低计算量算法设计、基线及实测报告](docs/FAST_ALGORITHM_REPORT.md)
- [ADC 采集侧与算法侧对接协议](docs/ACQUISITION_ALGORITHM_INTERFACE.md)
- [算法分支设计说明](docs/DESIGN.md)
- [权威赛题迁移与初赛压力测试设计](docs/COMPETITION_DERIVED_STRESS_TESTS.md)
- [接线、保护和赛前硬件检查](docs/HARDWARE.md)
- [赛事手册](docs/2026TI杯赛道一赛事手册.pdf)
- [TI 培训——算法部分](docs/TI培训-算法部分.pdf)

## 默认采集

| 项目 | 默认值 |
| --- | ---: |
| ADC | ADC0.CH2 / PA25 |
| CPU / MCLK | 80 MHz（SYSPLL，器件额定最高频率） |
| ULPCLK | 40 MHz（MCLK÷2） |
| 采样率 | 20 kS/s |
| 样本数 | 4096 |
| ADC 格式 | 12 bit unsigned，右对齐 |
| 缓冲大小 | 8192 B |
| 串口 | 115200, 8-N-1 |
| 触发 | R=ADC（默认三级级联），RT4=级联，RT1/RT2/RT3=历史路径，RT=原两算法，W=数字注入，T=片上自测，D=导出样点 |
| 板载波形输出 | DAC12 / PA15（自激时用杜邦线短接到 PA25） |

输入必须全程位于 ADC 合法范围。赛事只保证无负电压，并未保证不会超过 VDDA；接线前要用示波器确认，建议至少串联 1 kΩ 限流。

## 自激环路（RT）

没有信号发生器时，板子自己产生测试波形：DAC12 在 **PA15** 上按 20 kHz
回放 4096 点的已知波形，用一根杜邦线短接到 ADC 输入 **PA25**，同一帧再走
完整的采集 + 识别链路。`RT1`/`RT2` 分别让算法 1/2 跑一套
**200 道自编题 + 96 道 challenge，共 296 题**；发送 `RT` 会一键先后跑
两种算法，共 592 次测试。题集覆盖四类波形、
100–400 Hz、幅度/直流位置/噪声/方波占空比和边沿；challenge 额外加入
削顶、交越、谐波、慢调幅和相干干扰。每个用例上报：

```text
RT-START,algorithm=2,name=global_harmonic_model,cases=296,custom=200,challenge=96,fs_hz=20000,n=4096,dac=PA15,adc=PA25,dump=0
RT-CASE,index=1/296,group=custom,profile=boundary,type=sine,freq_millihz=...,vpp_millivolts=...,...
CAPTURING
PROCESSING
RESULT,status=OK,type=sine,freq_hz=200.195,vpp_v=1.997,...
RT-CHECK,verdict=PASS,type_ok=1,freq_ok=1,vpp_ok=1,duty_ok=1,freq_err_millihz=8,...
...
RT-DONE,algorithm=2,name=global_harmonic_model,pass=...,fail=...,custom_pass=...,custom_fail=...,challenge_pass=...,challenge_fail=...
READY
```

设计要点：

- 产生和采集共用 TIMER_0 的同一个 20 kHz 节拍，第 k 个 ADC 点恒等于第 k 个
  DAC 码，帧内不需要任何对齐；
- 普通用例频率取 `f = cycles × 20000 / 4096`；20/82 周期的边界题改由
  DAC 的 32 位分数相位累加器产生 100/400 Hz，始终在正式频率范围内；
- DAC 与 ADC 都以 VDDA 为参考，参考误差在码域两端抵消；能容纳的题目会
  自动留出上下各 0.3 V 的 DAC 线性区余量，避免近电源轨输出压缩；
- 非自激状态下 DAC 输出缓冲置为高阻，自检杜邦线可以一直挂着，不影响对外测量。

296 题默认只输出文本，不向串口终端倾倒二进制样点；需要查看失败帧时可中止
矩阵后发送 `D`，导出最近一帧。主机侧也可用 `tools/rt_self_test.py` 自动保存
逐题汇总。双算法一键测试的完整协议见 [TESTING_RT.md](TESTING_RT.md)。

历史 `RT1`/`RT2`/`RT3` 失败题会在 13 s 诊断窗口内重测；`RT4` 每题只采集分析一次，保留真实失败和单题 `RT-TIMING`。正式 `R/W/T` 输出 `TIMING,elapsed_ms=...,budget_ms=10000` 供上板核验。

生产级联和历史对照路径：

| 编号 | 名称 | 说明 |
| ---: | --- | --- |
| 1 | `legacy_amdf_crossing` | 原 AMDF 粗周期 + 中心穿越回归路径，保留作基线对照 |
| 2 | `global_harmonic_model` | 原默认路径；在基线会拒识时比较倍频候选的全局形状证据 |
| 3 | `fast_phase_model` | 级联 L1，历史 `RT3`；稀疏 AMDF、相位模型 |
| 4 | `cascade_rt3_spectral_yin` | **默认路径**：总运行 L1 和独立频域 L2；任一拒识或答案不同运行 YIN 类时域 L3 |

PA15 是 DAC 模拟输出，PA25 是 ADC 输入；UART0（板载 USB-UART 使用
PA10/TX、PA11/RX）负责收命令并向电脑回传识别结果和采样点，不要把 PA15
接到 UART 引脚。外部实时观察可用示波器探测 PA15。

## 推荐调用

    waveform_frame_t frame;
    waveform_result_t result;

    frame.samples = adc_buffer;
    frame.sample_count = 4096;
    frame.adc_full_scale = 4095;
    frame.sample_rate_millihz = 20000000;
    frame.adc_reference_microvolts = 3300000;
    frame.frontend_gain_ppm = 1000000;
    frame.sequence = next_sequence;
    frame.flags = WAVEFORM_FRAME_FLAG_CONTIGUOUS;

    if (waveform_analyze_frame(&frame, &result)) {
        /* 仅在 status == OK 时使用测量值。 */
    }

完整的字段、所有权、校准和错误约定见接口协议。

## 串口输出

成功示例：

    RESULT,status=OK,type=square,freq_hz=250.000,vpp_v=2.000,duty_pct=30.0,score_pct=99.9,margin_pct=20.1,cycles=51,freq_millihz=250000,vpp_microvolts=2000123,duty_ppm=300042,sequence=7,algorithm_version=131328,quality_flags=3

quality_flags=3 表示默认采样时钟和参考电压仍使用标称值；完成校准并正确置标志后应消失。

## 回归测试

在 Linux/macOS 或兼容 shell 中运行：

    bash tests/run_all_checks_strict.sh

当前确定性结果：

| 数据组 | 分类 | 最坏频率误差 | 最坏 Vpp 误差 | 最坏方波占空比误差 |
| --- | ---: | ---: | ---: | ---: |
| 初赛强度 2400 例 | 2400/2400 | 0.0802% | 4.233% | 1.666 pp |
| 强干扰 2400 例 | 2400/2400 | 0.2317% | 5.217% | 0.524 pp |
| 算法1：自编初赛门禁 200 例 | 200/200 | 0.0310% | 4.196% | 0.522 pp |
| 算法2：自编初赛门禁 200 例 | 200/200 | 0.0310% | 4.196% | 0.522 pp |
| 算法3：自编初赛门禁 200 例 | 200/200 | 0.0310% | 4.196% | 0.522 pp |

三套算法还分别运行 96 个赛事迁移挑战例：算法 1 为 95/96，算法 2 与 3 均为
96/96。算法 1 唯一失败是额外相干窄带干扰下的安全拒识；算法 2/3 的倍频
全局消歧恢复了该例。以上仍是确定性数字模型，不代表真实模拟链路结果。

正式级联 296/296，完整噪音矩阵在三组固定随机种子下各 1296/1296。修复原因、逐级结果和板上验收要求详见 [第二轮修复报告](docs/CASCADE_NOISE_ITERATION_20260925.md)；[首次交付报告](docs/CASCADE_NOISE_VALIDATION_20260925.md)保留历史 9 题失败记录。目标板的 10 s 时限、真实模拟链路和校准仍需实测。

## CCS 构建

1. 安装 Code Composer Studio、MSPM0 SDK 2.11.00.07 和兼容 SysConfig；
2. 导入本目录；
3. Project > Clean；
4. Build Project；
5. 烧录后确认启动横幅包含 `FW=cascade-v2.3-fs-estimate`、
   `API=0x00020100`、`FS_HZ=20240.000` 和 `CPU_HZ=80000000`；
6. 优先发送 `RT4` 测试正式级联；`RT1`、`RT2`、`RT3` 与 `RT` 保留历史对照。

`empty.syscfg` 已配置 80 MHz SYSPLL、ULPCLK÷2、PLL/FCC 校验、20 kS/s
定时器和 115200 UART。目标板构建仍应以 CCS/SysConfig 2.11 工程重新生成的
文件为准；不要复用旧 Debug 目录。

9 月 29 日外部输入的频率换算修正见 [板级校准说明](CALIBRATION_2026-09-29.md)。
`board_calibration.h` 中的 20240 Hz 是依据当日日志的暂定值，须在目标板实测。
